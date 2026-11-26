# AI usage

## Tools used

| Tool | Used for |
|---|---|
| **Claude Code** (Claude Opus 5.5, in VS Code) | Planning, writing most of the code, tests and docs, running the evals, analysing failures, and preparing the deployment |
| **GPT-OSS-120B on Groq** | The product's own LLM: extraction and the chat agent (not a coding aid) |
| **Gemini 3.1 Flash-Lite** | The product's fallback LLM |

The choices of models (GPT-OSS-120B primary, Gemini 3.1 Flash-Lite secondary, PP-OCRv5 mobile
for OCR), the scope and the free-tier constraint were made by me, not by the AI.

How AI output was checked:

- **Unit tests:** every module has them, 155 tests, no network.
- **End-to-end eval:** 20 cases with the real OCR and LLM.
- **Root cause before any change:** every eval failure was traced in the stored record and OCR
  text before anything was changed.
- **Images inspected by eye:** dataset labels and the rendered synthetic documents.
- **Real use:** the app was used locally, and that's how two of the issues below were found.

## Three things the AI got wrong, and how they were fixed

### 1. A number-parsing rule that was wrong for Malaysian receipts

**What it did:** Claude wrote a "3 digits after the separator means thousands" rule so that
Indonesian `60.000` reads as 60000, and applied it to every currency. SROIE (Malaysian)
receipts print unit prices like `9.000`, which became RM 9000, so correct receipts were flagged
with a false `LINE_MATH` error. A second bug in the same function turned `.01` into `1.00` and
ignored Unicode minus signs, so an OCR-mangled rounding of `-0.01` became `-100.00`.

**How it was caught:** the v1 eval. Four correct receipts went to review. The stored records
showed `unit_price: 9000.00` and `rounding: -100.00`, and reproducing the inputs in a small
script confirmed both bugs.

**Fix:** the currency now decides the number format; leading dots and Unicode minus signs are
handled; and a rounding > 1% of the total is treated as an OCR misread. Each case has a
regression test in [`tests/test_normalize.py`](tests/test_normalize.py).

### 2. A safety metric that missed the worst kind of failure

**What it did:** Claude defined "unsafe post" as *posting a record that should have gone to
review*. In the v1 run, a sideways photo produced garbled OCR whose numbers happened to add
up, and the record was **posted with the wrong vendor and total**. That is exactly what a
finance team fears most, and the metric scored it as safe.

**How it was caught:** reading the per-case results, which showed `rotated: posted, wrong:
vendor_name, total` while the summary said 0 unsafe posts.

**Fix:** an unsafe post is now *any posted record that needed review **or** has a wrong
value*. The saved v1 run was re-scored (`--rescore v1`, no new LLM calls), and v1 now correctly
**fails** the CI gate. v2 fixes the cause (sideways pages are detected and re-read).

### 3. Wrong assumptions about where the app would run

**What it did:**
- **Unusable start-up link.** The UI bound to `0.0.0.0` and printed `http://0.0.0.0:7860`.
  That address opens nothing in a browser on Windows, and plain `python` doesn't exist on this
  machine either, so the documented start command failed. I reported "the app is not working".
- **Deployment target.** Claude planned the deployment on Hugging Face Spaces from older
  knowledge. Creating the Docker Space returned `402 Payment Required`, because free Docker and
  Gradio Spaces now need a PRO plan.

**How it was caught:** by actually running the app, and by the deploy call itself.

**Fix:**
- The app now binds to `127.0.0.1` locally and prints a working link, and the README gives the
  venv commands for Windows.
- Deployment moved to **Streamlit Community Cloud** (free, and on the brief's list). Render's
  free 512 MB was rejected as too small for the OCR model.
- Before deploying, the pinned requirements were checked on PyPI for Python 3.12 Linux wheels,
  and the whole demo flow was tested headlessly with Streamlit's `AppTest`.

## Other mistakes caught along the way

- **Eval cases that tested two things at once.** The "mild blur" and "rotated" cases used
  altered copies of receipts that were also cases themselves, so once the agent read them
  correctly it rightly flagged them as `DUPLICATE`. Each now has its own receipt. Both
  versions were re-run on the identical corrected set.
- **An improvement that didn't improve.** Claude proposed a "self-check" re-read for
  arithmetic failures. The v2 eval showed it ran 4 times and never helped, while adding
  latency. It's reported as such in [EVAL_REPORT.md](EVAL_REPORT.md) instead of being credited.
- **A fallback that didn't actually work for chat.** The Gemini fallback was only ever tested
  on single extraction calls. Running the whole eval on Gemini showed that the chat agent's
  second request failed with `400`: Gemini 3 attaches a `thought_signature` to each tool call
  and requires it to be sent back, and the agent was rebuilding tool calls without it. Fixed,
  with a regression test (`test_gemini_thought_signature_is_sent_back`). The eval runner also
  now records a crashing case as a failure instead of aborting the whole run.
- **Guessed threshold.** The image-sharpness threshold started as a guess (50); a readable CORD
  photo scored 22. It was calibrated on 24 real images to 10. The same measurement showed that
  mean OCR confidence is useless as a quality signal: heavily blurred images still averaged 0.9
  or more.
- **Grounding vs. corrections.** The first validation design would have rejected a user's chat
  correction, because the OCR text contains the *misread* value. Fixed by letting
  human-verified fields skip grounding.
- **Date regex.** It required a word boundary after the year, but OCR often glues the time on
  (`02/11/202614:05`).
- **Outdated library knowledge.** The installed MCP SDK is 2.x (`FastMCP` renamed, exception
  messages hidden from the model by default). The code was written against the installed
  package, read from source, rather than from memory.
- **Blocked dependency.** `fpdf2` couldn't load on this machine (a Windows Application Control
  policy blocked a DLL), so the test invoice PDF is now written directly in PDF syntax.
