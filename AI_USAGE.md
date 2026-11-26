# AI usage

## Tools used

| Tool | Used for |
|---|---|
| **Claude Code** (Claude Opus 5.5, in VS Code) | Planning, writing most of the code, tests and docs, running the evals, and analysing the failures |
| **GPT-OSS-120B on Groq** | The product's own LLM: extraction and the chat agent (not a coding aid) |
| **Gemini 3.1 Flash-Lite** | The product's fallback LLM |

The choices of models (GPT-OSS-120B primary, Gemini 3.1 Flash-Lite secondary, PP-OCRv5 mobile
for OCR), the scope and the free-tier constraint were made by me, not by the AI.

How AI output was checked:

- Every module has unit tests: 152 tests, no network.
- The 20-case eval runs the real OCR and LLM end to end.
- Any eval failure was traced to a root cause in the stored records and OCR text before
  anything was changed.
- Dataset labels and the rendered synthetic documents were checked by looking at the images.

## Three things the AI got wrong, and how they were fixed

### 1. A number-parsing rule that was wrong for Malaysian receipts

**What it did:** Claude wrote a "3 digits after the separator means thousands" rule so that
Indonesian `60.000` reads as 60000. It applied the rule to every currency. SROIE (Malaysian)
receipts print unit prices like `9.000`, which became RM 9000, so correct receipts were flagged
with a false `LINE_MATH` error.

**How it was caught:** the v1 eval run. `sroie_0` and its duplicate went to review. Checking
the stored record showed `unit_price: 9000.00`.

**Fix:** the currency now decides the format. MYR, SGD, USD, GBP and INR use `.` as the decimal
point; IDR has no minor unit; the 3-digit rule is only a fallback when the currency is unknown.
A regression test pins `("9.000", "MYR") -> 9.00`
(see [`tests/test_normalize.py`](tests/test_normalize.py)).

### 2. Leading-dot and Unicode-minus bugs that turned "-0.01" into "-100.00"

**What it did:** `parse_amount` stripped separators from *both* ends of a string, so `.01`
became `01`, which is 1.00. It also ignored Unicode minus signs (`−`, `–`). The OCR misread a
"-0.01" rounding line as `-001` and `10'0-`, and those became rounding adjustments of
-1.00 and -100.00, which broke the total check on two correct receipts (`sroie_1`,
`sroie_3`).

**How it was caught:** the v1 eval run. The failing records showed `rounding: -100.00`, and
reproducing the inputs in a small script showed `parse_amount("-.01") == -1.00`.

**Fix:** only trailing separators are stripped, a leading dot gets a `0` in front, and
Unicode minus signs are normalised. Separately, any rounding larger than 1% of the total is
now treated as an OCR misread: it's ignored, with a non-blocking `IMPLAUSIBLE_VALUE` note.
That's safe because the total check already tolerates rounding-sized differences.

### 3. An eval case that contradicted the system's own (correct) duplicate detection

**What it did:** Claude made the "mild blur" robustness case by blurring `sroie_5`, which was
also an eval case of its own, and expected the blurred copy to be **posted**. The system flagged
it as `DUPLICATE`, because it is the same receipt (same vendor, date and total), and that is
the right behaviour.

**How it was caught:** the first baseline run. The case failed for the "wrong" reason, so the
expectation was wrong, not the agent.

**Fix:** the blur case now uses a receipt (SROIE row 6) that appears nowhere else in the set,
and the v1 baseline was **re-run on the corrected set** before any code changes, so the
before/after comparison uses identical cases.

## Smaller mistakes also caught

- **Guessed threshold.** The image-sharpness threshold started as a guess (50). Measuring 24
  real images showed a perfectly readable CORD photo scoring 22, so it was calibrated to 10.
  The same measurement showed that mean OCR confidence is a poor quality signal: heavily
  blurred images still averaged 0.9 or more.
- **Grounding vs. corrections.** The first validation design would have rejected the user's
  chat correction: the OCR text contains the *misread* value, so a corrected value can't be
  "found" in it. This was caught while writing the test for the chat scenario, and fixed by
  letting human-verified fields skip grounding.
- **A limitation instead of a fix.** A test first documented `0.535` being read as 535 as a
  "known limitation". It was fixed properly instead: a leading `0` can't be a thousands group.
- **Date regex.** The date regex needed a word boundary after the year, but OCR often glues the
  time on (`02/11/202614:05`), so the grounding check missed real dates. Caught by OCR-ing the
  synthetic receipts.
- **Outdated API knowledge.** The installed MCP SDK is 2.x, where `FastMCP` was renamed and
  exception messages are hidden from the model by default. The code was written against the
  installed package (read from source) rather than from memory, and anticipated errors use
  `ToolError` so the agent can read them.
- **Trivial bugs, each caught by the first run:** `lru_cache` on an unhashable dataclass, and a
  keyword clash in the tracing helper.
