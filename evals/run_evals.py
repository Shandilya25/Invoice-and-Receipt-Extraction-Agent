"""Run the eval set end-to-end through the real MCP server and score it.

    python -m evals.run_evals --label v2               # run, save evals/results/v2.json
    python -m evals.run_evals --compare v1 v2          # before/after table (markdown)

Metric (per run):
  field_accuracy   = correct fields / graded fields
  routing_accuracy = cases with the right status (posted / needs_review) and expected flags
  unsafe_posts     = cases that should have gone to review but were posted (must be 0)
  score            = 0.6 * field_accuracy + 0.4 * routing_accuracy

Exits with code 1 if score < --min-score or unsafe_posts > 0 (this is the CI gate).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import time
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path

from dotenv import load_dotenv

CASES = Path("evals/cases.json")
DATA = Path("evals/data")
RESULTS = Path("evals/results")
WORK = Path("evals/.work")
AMOUNT_FIELDS = {"subtotal", "tax", "total", "discount", "service_charge", "rounding"}


# --------------------------------------------------------------------------- scoring


def field_matches(field: str, expected, got) -> bool:
    if expected is None:  # the document doesn't show it: the agent must not invent it
        return got in (None, "")
    if got in (None, ""):
        return False
    if field == "vendor_name":
        from core.normalize import vendor_key

        return SequenceMatcher(None, vendor_key(expected), vendor_key(str(got))).ratio() >= 0.8
    if field in AMOUNT_FIELDS:
        try:
            return Decimal(str(got)) == Decimal(str(expected))
        except InvalidOperation:
            return False
    return str(got).strip().lower() == str(expected).strip().lower()


def score_case(case: dict, doc: dict, seconds: float) -> dict:
    record = doc.get("record") or {}
    codes = {i["code"] for i in doc.get("issues") or []}
    fields = {
        f: {"expected": exp, "got": record.get(f), "ok": field_matches(f, exp, record.get(f))}
        for f, exp in case.get("expected", {}).items()
    }
    status = doc.get("status", "error")
    routing_ok = status == case["expected_status"] and set(case.get("expected_issues", [])) <= codes
    return {
        "id": case["id"],
        "category": case["category"],
        "status": status,
        "expected_status": case["expected_status"],
        "routing_ok": routing_ok,
        "unsafe_post": case["expected_status"] == "needs_review" and status == "posted",
        "issues": sorted(codes),
        "fields": fields,
        "error": doc.get("error"),
        "seconds": round(seconds, 2),
    }


def summarize(results: list[dict], trace_file: Path) -> dict:
    graded = [f["ok"] for r in results for f in r["fields"].values()]
    field_acc = sum(graded) / len(graded)
    routing_acc = sum(r["routing_ok"] for r in results) / len(results)
    llm_calls = [
        json.loads(line)
        for line in trace_file.read_text(encoding="utf-8").splitlines()
        if '"step": "llm_' in line
    ]
    ok_calls = [c for c in llm_calls if "tokens_in" in c]
    seconds = sorted(r["seconds"] for r in results)
    return {
        "cases": len(results),
        "score": round(0.6 * field_acc + 0.4 * routing_acc, 3),
        "field_accuracy": round(field_acc, 3),
        "routing_accuracy": round(routing_acc, 3),
        "unsafe_posts": sum(r["unsafe_post"] for r in results),
        "latency_p50_s": round(statistics.median(seconds), 2),
        "latency_p95_s": round(seconds[int(0.95 * (len(seconds) - 1))], 2),
        "llm_calls": len(ok_calls),
        "llm_errors": len(llm_calls) - len(ok_calls),
        "providers": {p: sum(c["provider"] == p for c in ok_calls) for p in {"groq", "gemini"}},
        "tokens_in": sum(c["tokens_in"] for c in ok_calls),
        "tokens_out": sum(c["tokens_out"] for c in ok_calls),
        "cost_usd_list_price": round(sum(c["cost_usd"] for c in ok_calls), 5),
    }


# --------------------------------------------------------------------------- running


def run_case(case: dict, tools) -> dict:
    session = case["id"]
    if "chat" in case:  # multi-turn: drive the real agent
        from agent.agent import Agent
        from core.store import Store

        agent = Agent(tools, Store(os.environ["DB_PATH"]))
        for turn in case["chat"]:
            print(f"    user: {turn}\n    agent: {agent.reply(session, turn)[:300]}")
        listed = json.loads(tools.call_tool("list_documents", {"session_id": session}))
        docs = listed.get("result", listed) if isinstance(listed, dict) else listed
        if not docs:
            return {"error": "agent did not process the document"}
        return json.loads(tools.call_tool("get_document", {"doc_id": docs[0]["doc_id"]}))

    args = {"file_name": case["file"], "session_id": session}
    if case.get("home_currency"):
        args["home_currency"] = case["home_currency"]
    return json.loads(tools.call_tool("process_document", args))


def run(label: str, min_score: float) -> int:
    load_dotenv()
    work = WORK / label
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(DATA, work / "uploads")
    os.environ.update(
        DB_PATH=str(work / "eval.db"),
        UPLOAD_DIR=str(work / "uploads"),
        TRACE_FILE=str(work / "trace.jsonl"),
        MAX_RECEIPT_AGE_DAYS="0",  # dataset receipts are from 2017-2019
        DEFAULT_CURRENCY="MYR",  # same as the deployed app (Dockerfile / .env.example)
        LLM_MAX_RETRIES="6",  # free-tier rate limits: wait rather than switch model mid-run
    )
    from agent.mcp_client import MCPToolClient

    cases = json.loads(CASES.read_text(encoding="utf-8"))
    results = []
    tools = MCPToolClient()
    try:
        for case in cases:
            start = time.perf_counter()
            result = score_case(case, run_case(case, tools), time.perf_counter() - start)
            results.append(result)
            wrong = [f for f, v in result["fields"].items() if not v["ok"]]
            print(
                f"{'OK  ' if result['routing_ok'] and not wrong else 'FAIL'} {case['id']:<17}"
                f" status={result['status']:<13} issues={result['issues']} wrong={wrong}"
                f" {result['seconds']}s"
            )
    finally:
        tools.close()

    summary = summarize(results, work / "trace.jsonl")
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{label}.json"
    out.write_text(
        json.dumps({"label": label, "summary": summary, "cases": results}, indent=2, default=str),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))
    print(f"saved {out}")

    passed = summary["score"] >= min_score and summary["unsafe_posts"] == 0
    print(
        f"GATE: score {summary['score']} (min {min_score}), unsafe posts "
        f"{summary['unsafe_posts']} -> {'PASS' if passed else 'FAIL'}"
    )
    return 0 if passed else 1


def compare(before: str, after: str) -> str:
    a, b = (
        json.loads((RESULTS / f"{x}.json").read_text(encoding="utf-8")) for x in (before, after)
    )
    rows = ["| Metric | " + before + " | " + after + " |", "|---|---|---|"]
    for key in [
        "score",
        "field_accuracy",
        "routing_accuracy",
        "unsafe_posts",
        "latency_p50_s",
        "latency_p95_s",
        "llm_calls",
        "tokens_in",
        "tokens_out",
        "cost_usd_list_price",
    ]:
        rows.append(f"| {key} | {a['summary'][key]} | {b['summary'][key]} |")
    rows += ["", "| Case | " + before + " | " + after + " |", "|---|---|---|"]
    after_cases = {c["id"]: c for c in b["cases"]}
    for c in a["cases"]:
        d = after_cases[c["id"]]
        rows.append(f"| {c['id']} | {_cell(c)} | {_cell(d)} |")
    return "\n".join(rows)


def _cell(case: dict) -> str:
    wrong = [f for f, v in case["fields"].items() if not v["ok"]]
    mark = "✅" if case["routing_ok"] and not wrong else "❌"
    detail = f"{case['status']}" + (f", wrong: {', '.join(wrong)}" if wrong else "")
    return f"{mark} {detail}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--label", default="latest")
    parser.add_argument("--min-score", type=float, default=0.80)
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    args = parser.parse_args()
    if args.compare:
        print(compare(*args.compare))
        return
    sys.exit(run(args.label, args.min_score))


if __name__ == "__main__":
    main()
