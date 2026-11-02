# TruthTrace — A News Verification and Narrative Tracking System

*Trace the claim. Check the evidence. Show the full picture.*

TruthTrace is not a "fake or real" detector. It takes a news claim, headline, article link, image, video or audio file and
produces an evidence-based, explainable report: what exactly is claimed, which sources support or contradict it, how
independent those sources are, how different outlets and languages report it, whether translation or rewording changed
its meaning, and how the claim evolved over time. When the evidence is not enough, it says so.

This implementation follows the project document *TruthTrace_Project_Document.docx* (concept by Sahil Soni "GK").

### Project status and scope

TruthTrace is a **full-stack local web application**, not a custom-trained AI model. Its FastAPI backend retrieves live public evidence and combines deterministic claim/evidence rules with an optional hosted language model for selected analysis tasks. If the model provider is unavailable or rate-limited, the app falls back to heuristics and labels that mode. Model weights are not included, and this folder does not contain a public repository or hosted deployment URL; the app runs locally at `http://127.0.0.1:8000`.

The saved evaluation report is a small, preliminary, 14-claim hand-labelled run in heuristic mode (30 October 2026). It is useful for development checks, not a claim of general fact-checking accuracy. The test cases and references are in `eval/claims_testset.jsonl`.

---

## Quick start (Windows)

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python run.py
```

Open **http://127.0.0.1:8000**. Requires Python 3.10+ and an internet connection (evidence is collected live).

On macOS/Linux use `.venv/bin/pip` and `.venv/bin/python`.

### Optional: enable an AI model

TruthTrace works without any API key in **heuristic mode** (rule-based claim extraction, polarity-aware stance
detection, deterministic meaning comparison). An AI model improves stance detection, sub-claim extraction, viewpoint
analysis, the reasoned conclusion and support/oppose classification of posts. Copy `.env.example` to `.env` and set one of:

| Option | Settings |
|---|---|
| Claude (Anthropic) | `ANTHROPIC_API_KEY=...` (default model `claude-opus-5-5`) |
| Google Gemini (free tier) | `LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai` `LLM_API_KEY=...` `LLM_MODEL=gemini-3.6-flash` |
| Groq | `LLM_BASE_URL=https://api.groq.com/openai/v1` `LLM_API_KEY=...` `LLM_MODEL=openai/gpt-oss-20b` |
| Ollama (local, offline model) | `LLM_BASE_URL=http://localhost:11434/v1` `LLM_MODEL=llama3.1` |

The sidebar shows which mode is active. Restart the server after editing `.env`.
If the AI provider returns a rate limit (429) or server error (5xx), TruthTrace pauses further AI calls for 90 seconds and continues with its rule-based fallback. The report labels that fallback.

---

Or simply double-click **`run.bat`** (sets everything up on first run and opens the browser).

## Suggested demo (≈10 minutes)

1. **News Intelligence Dashboard** → example *Great Wall visible from space* → verdict **Contradicted**; open the 12 dimension cards, “Why this verdict”, Section A evidence with quotes.
2. Example *Chandrayaan-3 landing* → **Supported**, official sources (ISRO/PIB/NASA) in dimension 3.
3. Example *5G spreads corona (Hindi)* → Hindi input translated, Hindi summary shown.
4. Example *Altered quote* → **Meaning Altered** (“may collapse if repairs are delayed” → “confirms … will collapse”).
5. **Meaning & Translation → Track a chain** → the document's stomach → pregnant example; stage 2 highlighted as the break.
6. **Cross-Source Comparison** → *Chandrayaan-3 landing*: Indian vs international vs Hindi coverage, report types A–D.
7. **Claim Evolution** → *2000 rupee note GPS chip*: first appearance (Nov 2016), first disputed version.
8. **Public Opinion** → *H-1B visa fee*: unique accounts, duplicates removed, “sampled online discussion”.
9. **Perspectives** and **Influential People** → *Great Nicobar Island development project*.
10. **Media Check** → upload any photo; result stays “inconclusive” with reverse-search links (no fake certainty).
11. **System Accuracy** → test-set metrics; **History** → claim memory when a claim is checked again.

## What is implemented

### The four verification stages (document §4)

| Stage | Where | What it does |
|---|---|---|
| 1. Claim Verification | **News Intelligence Dashboard** | Breaks input into sub-claims, retrieves evidence, classifies stance with a verified quote, groups sources into independent chains, applies explicit verdict rules |
| 2. Cross-Source Comparison | **Cross-Source Comparison** | Indian vs international vs Hindi-language coverage; each headline classified as report type A–D (§3); headline-vs-body checks on downloaded articles |
| 3. Meaning & Translation | **Meaning & Translation** | Compares an original and a later version (any mix of Hindi/English): numbers, negation, "may" vs "did", names, figurative → literal; finds the exact stage in a chain where meaning broke |
| 4. Claim Evolution | **Claim Evolution** | Orders all versions by date, first known appearance, first version that added a claim, first disputed version, timeline chart |

### The five verdicts (§5)
Supported · Contradicted · Misleading Context or Framing · Meaning Altered · Insufficient Evidence.
The verdict is built by explicit rules that are shown with every result ("Why this verdict"):

1. A supplied original whose meaning was changed → **Meaning Altered**
2. Evidence weight (independent chains + 2 × chains from official / fact-check / academic / reference / wire sources) disputing the claim ≥ 2 × the supporting weight → **Contradicted**
3. Supported facts but a misleading headline, old-as-new timing or synthetic/false-context media → **Misleading Context or Framing**
4. Supporting weight ≥ 2 × disputing weight (≥ 2 chains or 1 strong source) → **Supported**
5. Otherwise → **Insufficient Evidence** ("Source not traced — authenticity unverified", never "maybe fake")

Confidence is defined as *how much trust the evidence deserves* (chains, strong sources, ratio over the opposing side), not how
confident the AI feels; it is capped at Medium in heuristic mode.

### The 12 assessment dimensions (§6)
Every verification computes each dimension separately before the overall result: claim accuracy, source reliability,
original-source traceability, independent corroboration, contradiction & evidence quality, translation & meaning accuracy,
headline & framing distortion, time & context accuracy, image & video authenticity, audio & speaker authenticity,
public sentiment & spread, and expert/official/influential statements. Each card shows its rating, findings, evidence ids and
measurement method. Dimensions that do not apply (e.g. video checks for a text claim — the §7 module rule) are marked
"Not applicable" instead of being guessed.

### Dashboards
* **News Intelligence Dashboard** (§13) — sections A Claim & Evidence, B Source & Narrative Tracking, C Perspectives, D Public Sentiment, E Influential Statements, plus the §14 result table.
* **Perspectives** (§10, §17) — verifiable facts, viewpoints found from real arguments, evidence check of each side's factual claims, values vs facts, reasoned conclusion with priority scenarios and "what could change this conclusion" (AI mode).
* **Public Opinion** (§11) — sentiment, reasoning and evidence status shown separately; duplicates/copies removed, each account counted once, bot-like and high-volume accounts flagged, sample size/period/platforms shown, labelled "sampled online discussion".
* **Influential People** (§12) — exact attributed quotes, role (Wikipedia), official authority, decision power, reach in sample, policy impact, primary-source check; never merged into public opinion.
* **Media Check** (§8) — content credentials (C2PA) and AI-generator metadata, error-level analysis, video keyframes for reverse search, cut detection, metadata creation dates vs "new" claims; outcomes use the four §8 labels and default to "inconclusive".

### Extra ideas from §19
Evidence trail for every result · claim memory (same claim or same media file → earlier result shown) · update tracking
(verdict changed since last check) · source reliability over time · risk-based labelling (health, communal, safety) ·
reader feedback and appeals · plain-language summary, translated to Hindi for Hindi input · public accuracy page.

### Measuring the system's own accuracy (§15)
* `python -m eval.run_eval` — runs the hand-labelled claim test set (`eval/claims_testset.jsonl`, with references) through the
  full live pipeline; reports accuracy, precision, recall, F1, false-positive rate, abstention rate, calibration and
  language-wise accuracy. Results appear on the **System Accuracy** page.
* `python -m eval.run_eval --meaning` — the meaning-drift test set (synthetic sentence pairs, English/Hindi).
* `python -m pytest tests` — offline unit tests of the deterministic core.

---

## Never fabricating: safeguards

* Evidence comes only from live HTTP responses: Google News RSS (English + Hindi), Bing News RSS, DuckDuckGo web search,
  Wikipedia, Reddit, Bluesky, Hacker News, Lemmy and GDELT. Every item links to its source.
* Any quote an AI model gives as justification is matched against the downloaded source text; unmatched quotes are
  discarded and the stance is not counted. Evidence ids the model cites that do not exist are removed.
* Sources that fail or are rate-limited are listed as **unavailable** in "Data sources used" — nothing is filled in.
* Without an AI model, no reasoned conclusion and no support/oppose stance of people is produced (tone is shown instead, and labelled so).
* Media detectors are reported as indicators only.

## Architecture

```
frontend/            single-page app (HTML + vanilla JS + Chart.js), served by FastAPI
backend/
  app.py             REST API
  config.py          settings (.env)
  llm.py             optional AI model (Claude or OpenAI-compatible), JSON-only, failure → heuristic fallback
  sources.py         connectors to public data sources
  textutils.py       tokenisation, numbers (lakh/crore, Devanagari digits), negation, source classification
  db.py              SQLite: history, claim memory, source record, feedback
  analysis/
    claims.py        claim extraction, misinformation type → recommended tests (§7)
    evidence.py      retrieval, stance (quote-verified), independence chains
    verify.py        the 12 dimensions, verdict rules, dashboard sections
    meaning.py       translation and meaning-drift analysis
    framing.py       headline vs body, headline tone
    compare.py       cross-source comparison (report types A–D)
    evolution.py     claim evolution timeline
    perspectives.py  POV system and reasoned conclusion
    opinion.py       public opinion sampling and de-duplication
    influencers.py   influential statements
    media.py         image / video / audio checks
eval/                test sets and evaluation script
tests/               unit tests
data/                SQLite database and evaluation results (created at runtime)
```

## Honest limitations (§20)
* Heuristic stance detection can misread sarcasm and complex sentences; configure an AI model for better accuracy.
* Only free, key-less sources are searched; paywalled articles and closed platforms (WhatsApp, X, Facebook, Instagram) are not covered. Search providers may rate-limit heavy use.
* There is no free reverse-image API and no validated deepfake detector, so media results are usually "inconclusive" with links for manual reverse search.
* Online opinion samples are not representative of the public.
* Hindi analysis depends on machine translation; quality differs by language.
* Live evidence changes over time, so the same claim can get a different result on a different day — that is what update tracking records.
