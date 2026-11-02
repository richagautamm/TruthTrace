"""Stage 2 – Cross-Source Comparison: how Indian, international and Hindi sources report the
same event, and whether headlines match the claim and their own article text (document §3, §4)."""
from __future__ import annotations

import asyncio
from collections import Counter

from .. import llm, sources
from ..textutils import content_tokens, coverage, extract_numbers, is_year
from . import evidence as ev
from . import framing, meaning

REPORT_TYPES = {
    "A": "Consistent — reports the claim as the original did",
    "B": "Places the claim inside a different story (e.g. political criticism)",
    "C": "Headline suggests something the original never said",
    "D": "Reverses or disputes the original meaning",
    "—": "Not about the claim",
}


async def hindi_query(query: str) -> str | None:
    if llm.enabled():
        data = await llm.ask_json("translate a news search query to Hindi", f'Query: "{query}". Return JSON {{"hindi": "..."}}', 300)
        if isinstance(data, dict) and data.get("hindi"):
            return data["hindi"]
    return await sources.translate_free(query, "en", "hi")


async def translate_headlines(items: list[dict]) -> None:
    hi = [it for it in items if it["lang"] == "hi"][:12]
    if not hi:
        return
    joined = " ||| ".join(it["title"] for it in hi)
    res = await meaning.to_english(joined)
    parts = [p.strip() for p in res["english"].split("|||")]
    if len(parts) == len(hi):
        for it, p in zip(hi, parts):
            it["title_en"] = p
            it["translation_method"] = res["method"]


def classify_report(claim: str, it: dict) -> dict:
    head = it.get("title_en") or it["title"]
    st = ev.heuristic_stance(claim, {**it, "title": head, "passage": head + ". " + it.get("snippet", "")})
    tone = framing.tone(head)
    changes = meaning.deterministic_changes(claim, head)
    types = {c["type"] for c in changes}
    claim_nums = {n["value"] for n in extract_numbers(claim) if not is_year(n) and not n.get("word")}
    head_nums = {n["value"] for n in extract_numbers(head) if not is_year(n) and not n.get("word")}
    if st["stance"] == "irrelevant":
        cat, why = "—", "Shares few key terms with the claim."
    elif st["stance"] == "contradicts" or "negation flipped" in types:
        cat, why = "D", st["reason"] if st["stance"] == "contradicts" else "Headline contains a negation the claim does not."
    elif (head_nums - claim_nums and claim_nums) or "figurative to literal" in types or "certainty increased" in types or "question" in tone["label"]:
        cat, why = "C", "Headline adds or changes details (" + ", ".join(sorted(types & {"number changed", "number added", "figurative to literal", "certainty increased"})) + ")" if types else "Question-style headline insinuates more than it states."
    elif tone["critical_words"] and not framing.tone(claim)["critical_words"]:
        cat, why = "B", "Critical framing words: " + ", ".join(tone["critical_words"])
    else:
        cat, why = "A", "Same key facts, no added framing detected."
    return {"category": cat, "category_label": REPORT_TYPES[cat], "why": why, "tone": tone, "stance": st["stance"],
            "omitted_terms": framing.omitted_terms(claim, head), "changes": [c["type"] for c in changes if c["severity"] != "low"]}


async def _ai_classify(claim: str, items: list[dict]) -> None:
    if not llm.enabled():
        return
    rel = [it for it in items if it["report"]["category"] != "—"][:30]
    if not rel:
        return
    listing = "\n".join(f"[{it['id']}] {it['source']}: {it.get('title_en') or it['title']}" for it in rel)
    data = await llm.ask_json("headline framing comparison", f"""ORIGINAL CLAIM: {claim}
HEADLINES:
{listing}
Classify each headline relative to the original claim:
A = consistent with the original; B = puts the claim inside a different story (e.g. political criticism) but facts intact;
C = headline suggests something the original never said (added claim, exaggeration, insinuation); D = reverses/disputes the meaning.
Return JSON: {{"items": [{{"id": "E...", "category": "A|B|C|D", "why": "short reason naming the exact words", "added": "new claim added, or empty", "omitted": "context left out, or empty"}}]}}""", 4000)
    if not isinstance(data, dict):
        return
    by_id = {it["id"]: it for it in rel}
    for x in data.get("items", []):
        it = by_id.get(x.get("id"))
        if it and x.get("category") in ("A", "B", "C", "D"):
            it["report"].update({"category": x["category"], "category_label": REPORT_TYPES[x["category"]], "why": x.get("why", ""),
                                 "added": x.get("added", ""), "omitted_ai": x.get("omitted", ""), "method": "ai"})


async def run(query: str, status) -> dict:
    hq = await hindi_query(query)
    tasks = [sources.google_news(query, status, n=25), sources.bing_news(query, status, n=15)]
    if hq:
        tasks.append(sources.google_news(hq, status, lang="hi", n=15))
    res = await asyncio.gather(*tasks, return_exceptions=True)
    items, seen = [], set()
    for r in res:
        if isinstance(r, Exception):
            continue
        for it in r:
            k = it["url"]
            if k not in seen:
                seen.add(k)
                items.append(it)
    await translate_headlines(items)
    for it in items:
        it["report"] = classify_report(query, it)
        it["report"]["method"] = "heuristic"
    await _ai_classify(query, items)
    relevant = [it for it in items if it["report"]["category"] != "—"]

    # headline vs body for articles with a direct URL
    direct = [it for it in relevant if "news.google.com" not in it["url"]][:5]
    pages = await asyncio.gather(*(sources.fetch_article(it["url"]) for it in direct), return_exceptions=True)
    for it, page in zip(direct, pages):
        if isinstance(page, dict) and page.get("ok") and len(page["text"]) > 200:
            hb = framing.headline_vs_body(it["title"], page["text"])
            it["headline_body"] = await framing.headline_vs_body_ai(it["title"], page["text"], hb)

    by_region = {}
    for it in relevant:
        key = it["region"] if it["lang"] != "hi" else "Hindi-language"
        g = by_region.setdefault(key, {"count": 0, "categories": Counter(), "tones": Counter()})
        g["count"] += 1
        g["categories"][it["report"]["category"]] += 1
        g["tones"][it["report"]["tone"]["label"].split(" (")[0]] += 1
    heads = [it.get("title_en") or it["title"] for it in relevant]
    term_freq = Counter(w for h in heads for w in set(content_tokens(h)))
    consensus = [w for w, n in term_freq.most_common(15) if heads and n / len(heads) >= 0.3]
    for it in relevant:
        h = it.get("title_en") or it["title"]
        it["report"]["consensus_overlap"] = round(coverage(" ".join(consensus), h), 2) if consensus else None
    relevant.sort(key=lambda x: x["published"] or "", reverse=False)
    return {
        "query": query,
        "hindi_query": hq,
        "items": relevant,
        "excluded_irrelevant": len(items) - len(relevant),
        "by_region": {k: {"count": v["count"], "categories": dict(v["categories"]), "tones": dict(v["tones"])} for k, v in by_region.items()},
        "consensus_terms": consensus,
        "report_types": REPORT_TYPES,
        "notes": [
            "Negative framing is not fake news: two outlets can report the same event from different angles and both be factual.",
            "Region is inferred from the outlet's domain; Hindi-language items are grouped separately.",
            "Headline–body checks run only on articles whose full text could be downloaded.",
        ],
    }
