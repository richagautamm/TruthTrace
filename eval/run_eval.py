"""Measure TruthTrace's own accuracy (document §15).

    python -m eval.run_eval            # run the claim test set through the full pipeline (needs internet)
    python -m eval.run_eval --meaning  # meaning-drift test set only

Results are written to data/eval_results.json and shown on the Accuracy page.
The claim test set is small and hand-labelled from the references in the file; treat the numbers as a
demonstration of the method, not as a published accuracy figure.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import config, db, llm  # noqa: E402
from backend.analysis import meaning, verify  # noqa: E402

FAKE = {"Contradicted", "Meaning Altered", "Misleading Context or Framing"}


def _load(name):
    return [json.loads(l) for l in (ROOT / "eval" / name).read_text(encoding="utf-8").splitlines() if l.strip()]


def binary_metrics(pairs):
    """pairs: list of (truth_is_positive, predicted_positive)."""
    tp = sum(1 for t, p in pairs if t and p)
    fp = sum(1 for t, p in pairs if not t and p)
    fn = sum(1 for t, p in pairs if t and not p)
    tn = sum(1 for t, p in pairs if not t and not p)
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else None
    fpr = fp / (fp + tn) if fp + tn else None
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": prec, "recall": rec, "f1": f1, "false_positive_rate": fpr}


async def meaning_eval() -> dict:
    """Deterministic checks only (no network, no AI) so the result is reproducible."""
    rows = []
    for case in _load("meaning_testset.jsonl"):
        o_en = case["original"] if case["pair"].startswith("en") else None
        v_en = case["variant"] if case["pair"].endswith("en") else None
        # translations are needed for Hindi sides; when offline the raw text is compared and the case is marked
        o = {"english": o_en} if o_en else await meaning.to_english(case["original"])
        v = {"english": v_en} if v_en else await meaning.to_english(case["variant"])
        changes = meaning.deterministic_changes(o["english"], v["english"], case["original"], case["variant"])
        pred = any(c["severity"] == "high" for c in changes)
        rows.append({"id": case["id"], "pair": case["pair"], "expected_altered": case["altered"], "predicted_altered": pred,
                     "correct": pred == case["altered"], "changes": [c["type"] for c in changes]})
    by_pair = defaultdict(list)
    for r in rows:
        by_pair[r["pair"]].append(r["correct"])
    return {
        "cases": rows,
        "accuracy": sum(r["correct"] for r in rows) / len(rows),
        "metrics": binary_metrics([(r["expected_altered"], r["predicted_altered"]) for r in rows]),
        "by_language_pair": {k: {"n": len(v), "accuracy": sum(v) / len(v)} for k, v in by_pair.items()},
        "note": "Synthetic sentence pairs written for this project to test the drift detector (not real news).",
    }


async def claims_eval() -> dict:
    db.init()
    rows = []
    for case in _load("claims_testset.jsonl"):
        t = time.time()
        r = await verify.run(text=case["text"], original_text=case.get("original_text", ""))
        rows.append({"id": case["id"], "lang": case["lang"], "text": case["text"], "expected": case["label"],
                     "predicted": r["verdict"]["verdict"], "confidence": r["verdict"]["confidence"],
                     "correct": r["verdict"]["verdict"] == case["label"], "seconds": round(time.time() - t, 1),
                     "support_chains": r["verdict"]["counts"]["support_chains"], "contradict_chains": r["verdict"]["counts"]["contradict_chains"]})
        print(f"{case['id']}: expected {case['label']:<22} got {r['verdict']['verdict']:<30} {'OK' if rows[-1]['correct'] else 'MISS'}")
    answered = [r for r in rows if r["predicted"] != "Insufficient Evidence"]
    calib = defaultdict(list)
    for r in answered:
        calib[r["confidence"]].append(r["correct"])
    by_lang = defaultdict(list)
    for r in rows:
        by_lang[r["lang"]].append(r["correct"])
    labels = sorted({r["expected"] for r in rows} | {r["predicted"] for r in rows})
    confusion = {e: dict(Counter(r["predicted"] for r in rows if r["expected"] == e)) for e in labels}
    # "fake" = any non-supported problem verdict; genuine = Supported
    scored = [r for r in rows if r["expected"] != "Insufficient Evidence"]
    return {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": llm.provider_info(),
        "n": len(rows),
        "accuracy": sum(r["correct"] for r in rows) / len(rows),
        "fake_detection": binary_metrics([(r["expected"] in FAKE, r["predicted"] in FAKE) for r in scored]),
        "abstention_rate": sum(1 for r in rows if r["predicted"] == "Insufficient Evidence") / len(rows),
        "accuracy_when_answered": (sum(r["correct"] for r in answered) / len(answered)) if answered else None,
        "calibration": {k: {"n": len(v), "accuracy": sum(v) / len(v)} for k, v in calib.items()},
        "by_language": {k: {"n": len(v), "accuracy": sum(v) / len(v)} for k, v in by_lang.items()},
        "confusion": confusion,
        "cases": rows,
        "note": "Small hand-labelled set (see eval/claims_testset.jsonl for references). Live web evidence changes over time, so results vary between runs.",
    }


async def main():
    if "--meaning" in sys.argv:
        print(json.dumps(await meaning_eval(), indent=2, ensure_ascii=False))
        return
    res = await claims_eval()
    (config.DATA_DIR / "eval_results.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nAccuracy {res['accuracy']:.0%}, abstention {res['abstention_rate']:.0%}. Saved to data/eval_results.json")


if __name__ == "__main__":
    asyncio.run(main())
