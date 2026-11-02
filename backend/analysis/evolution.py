"""Stage 4 – Claim Evolution and Narrative Tracking: connect the original statement to every
later version, in time order, and mark which version added a new claim and which is disputed."""
from __future__ import annotations

import asyncio
from collections import Counter

from .. import sources
from ..textutils import coverage
from . import compare, evidence as ev
from . import meaning


async def run(query: str, status, original: str | None = None) -> dict:
    hq = await compare.hindi_query(query)
    tasks = [sources.google_news(query, status, n=40), sources.bing_news(query, status, n=15),
             sources.web_search(query + " fact check", status, n=8)]
    if hq:
        tasks.append(sources.google_news(hq, status, lang="hi", n=20))
    res = await asyncio.gather(*tasks, return_exceptions=True)
    items, seen = [], set()
    for r in res:
        if isinstance(r, Exception):
            continue
        for it in r:
            if it["url"] not in seen:
                seen.add(it["url"])
                items.append(it)
    await compare.translate_headlines(items)
    versions = []
    for it in items:
        head = it.get("title_en") or it["title"]
        rel = max(coverage(query, head + " " + it.get("snippet", "")), coverage(hq, it["title"]) if hq else 0)
        if rel < 0.35:
            continue
        it["relevance"] = round(rel, 2)
        versions.append(it)
    dated = sorted([v for v in versions if v["published"]], key=lambda v: v["published"])
    undated = [v for v in versions if not v["published"]]
    reference = original or query

    first_added, first_disputed = None, None
    for v in dated + undated:
        head = v.get("title_en") or v["title"]
        changes = meaning.deterministic_changes(reference, head)
        st = ev.heuristic_stance(query, {**v, "title": head, "passage": head + ". " + v.get("snippet", "")})
        added = [c for c in changes if c["type"] in ("number added", "number changed", "name added", "figurative to literal", "certainty increased", "interpretation added")]
        v["evolution"] = {
            "label": meaning.stage_label(changes, 0.5),
            "changes": [{"type": c["type"], "severity": c["severity"], "explanation": c["explanation"]} for c in changes if c["severity"] != "low"],
            "adds_new_claim": bool(added),
            "disputed": st["stance"] == "contradicts" or v["source_type"] == "fact-check",
            "stance": st["stance"],
        }
        if added and first_added is None and v["published"]:
            first_added = v["id"]
        if v["evolution"]["disputed"] and first_disputed is None and v["published"]:
            first_disputed = v["id"]

    per_week = Counter()
    for v in dated:
        per_week[v["published"][:10]] += 1
    first = dated[0] if dated else None
    return {
        "query": query,
        "reference": reference,
        "reference_kind": "user-supplied original" if original else "the claim being tracked",
        "first_known_appearance": first,
        "first_appearance_note": "Earliest item found in the searched indexes. The earliest source found is not always the true original."
                                 if first else "Source not traced — authenticity unverified.",
        "versions": dated + undated,
        "first_added_claim_id": first_added,
        "first_disputed_id": first_disputed,
        "timeline": [{"date": d, "count": n} for d, n in sorted(per_week.items())],
        "span_days": _span(dated),
    }


def _span(dated):
    if len(dated) < 2:
        return 0
    from datetime import datetime

    a = datetime.fromisoformat(dated[0]["published"])
    b = datetime.fromisoformat(dated[-1]["published"])
    return (b - a).days
