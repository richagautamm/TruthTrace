"""Stage 3 – evidence retrieval, stance assessment and independence.

Anti-fabrication guard: every stance that the AI model reports must come with a quote that
can be found in the retrieved source text. Quotes that cannot be matched are discarded and the
item is downgraded to "mentions". In heuristic mode the quote is always a real sentence copied
from the source.
"""
from __future__ import annotations

import asyncio
import re

from .. import config, llm, sources
from ..textutils import (
    NEGATIONS, content_tokens, coverage, debunk_cues, extract_numbers, is_year, jaccard, negation_count,
    normalize, shingles, split_sentences, tokens,
)

STRONG_TYPES = {"official", "fact-check", "academic", "reference", "wire"}
TRUE_CUES = ("is true", "is correct", "is genuine", "is authentic", "is accurate", "did happen", "confirmed", "rated true", "verdict: true", "सच है", "सही है")


# ------------------------------------------------------------------ retrieval

async def gather(query: str, status, *, hindi_query: str | None = None, extra_queries=(), news_n: int = 12) -> list[dict]:
    tasks = [
        sources.bing_news(query, status, n=news_n),
        sources.google_news(query, status, n=news_n),
        sources.web_search(query, status),
        sources.web_search(query + " fact check", status, n=8),
        sources.wikipedia(query, status),
    ]
    if hindi_query:
        tasks.append(sources.google_news(hindi_query, status, lang="hi", n=10))
    for q in extra_queries:
        tasks.append(sources.web_search(q, status, n=6))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    items, seen_urls, seen_titles = [], set(), set()
    for res in results:
        if isinstance(res, Exception):
            continue
        for it in res:
            key_t = " ".join(content_tokens(it["title"]))[:80]
            if it["url"] in seen_urls or (key_t and key_t in seen_titles):
                continue
            seen_urls.add(it["url"])
            seen_titles.add(key_t)
            items.append(it)
    for it in items:
        it["relevance"] = round(max(coverage(query, it["title"] + " " + it["snippet"]),
                                    coverage(hindi_query, it["title"]) if hindi_query else 0), 3)
    items.sort(key=lambda x: (x["relevance"], x["source_type"] in STRONG_TYPES), reverse=True)
    return items[: config.MAX_EVIDENCE]


async def enrich(items: list[dict], claim: str, limit: int | None = None) -> None:
    """Fetch article bodies for the most relevant items and pick the passage closest to the claim."""
    limit = config.FETCH_ARTICLE_BODIES if limit is None else limit
    targets = [it for it in items if not it["text"] and "news.google.com" not in it["url"]][:limit]
    pages = await asyncio.gather(*(sources.fetch_article(it["url"]) for it in targets), return_exceptions=True)
    for it, page in zip(targets, pages):
        if isinstance(page, dict) and page.get("ok") and len(page["text"]) > 200:
            it["text"] = page["text"]
            it["page"] = {k: page.get(k) for k in ("published", "modified", "author", "primary_links", "has_correction_note", "outbound_domains")}
            if not it["published"] and page.get("published"):
                it["published"] = page["published"]
    for it in items:
        it["passage"] = best_passage(claim, it)


def best_passage(claim: str, item: dict, n: int = 3) -> str:
    full = item.get("text") or ""
    pool = split_sentences(full)[:400] if full else []
    if not pool:
        return normalize(item["title"] + ". " + item["snippet"])
    scored = sorted(range(len(pool)), key=lambda i: coverage(claim, pool[i]), reverse=True)[:n]
    return " ".join(pool[i] for i in sorted(scored))


def evidence_text(item: dict) -> str:
    return normalize(" ".join([item["title"], item.get("snippet", ""), item.get("passage", ""), item.get("text", "")]))


# ------------------------------------------------------------------ quote verification

def quote_found(quote: str, text: str) -> bool:
    if not quote:
        return False
    q, t = normalize(quote).lower(), normalize(text).lower()
    if len(q) < 8:
        return False
    if q in t:
        return True
    # tolerate small differences (punctuation, curly quotes): most of the quote's words in one sentence
    qt = content_tokens(q)
    if len(qt) < 3:
        return False
    for s in split_sentences(text):
        st = set(content_tokens(s))
        if sum(1 for w in qt if w in st) / len(qt) >= 0.85:
            return True
    return False


# ------------------------------------------------------------------ stance

def _best_sentence(claim: str, text: str) -> str:
    sents = split_sentences(text) or [text]
    return max(sents, key=lambda s: coverage(claim, s))[:400]


def _negative(sentence: str, claim: str | None = None) -> bool:
    """Polarity of a sentence: debunking language, or a negation that governs one of the claim's key words
    ("is not visible") — a stray "no country had done this before" does not flip the meaning."""
    if debunk_cues(sentence):
        return True
    if claim is None:
        return negation_count(sentence) % 2 == 1
    toks = tokens(sentence)
    key = set(content_tokens(claim))
    flips = 0
    for i, t in enumerate(toks):
        if t in NEGATIONS or t.endswith("n't"):
            window = toks[i + 1:i + 6]
            if (i == 0 and t == "no") or any(w in key for w in window):
                flips += 1
    return flips % 2 == 1


def heuristic_stance(claim: str, item: dict) -> dict:
    title = item["title"].strip()
    title_snip = title + ("" if title.endswith(("?", ".", "!")) else ".") + " " + item.get("snippet", "")
    passage = item.get("passage") or ""
    body = normalize(title_snip + " " + passage)
    rel = max(coverage(claim, title_snip), coverage(claim, passage) if passage else 0)
    # Questions ("Is X true?", "Fact or myth?") assert nothing, so they are never used as evidence.
    sentences = split_sentences(title) + split_sentences(item.get("snippet", "")) + split_sentences(passage)
    asserts = [s for s in sentences if not s.rstrip().endswith("?") and len(s) > 15]
    ranked = sorted(asserts, key=lambda s: coverage(claim, s), reverse=True)
    quote = (ranked[0] if ranked else _best_sentence(claim, body))[:400]
    out = {"evidence_id": item["id"], "subclaim": 0, "quote": quote, "quote_verified": True, "method": "heuristic", "relevance": round(rel, 2)}
    if rel < 0.3:
        return {**out, "stance": "irrelevant", "reason": f"Shares only {rel:.0%} of the claim's key terms."}
    top = [s for s in ranked[:3] if coverage(claim, s) >= 0.4]
    if not top:
        return {**out, "stance": "mentions", "reason": "On topic, but no sentence directly states or disputes the claim (questions are not counted)."}
    claim_negative = _negative(claim)
    best = top[0]
    neg_votes = sum(1 for s in top[:2] if _negative(s, claim))
    is_factcheck = item.get("source_type") == "fact-check"
    evidence_negative = _negative(best, claim) or (neg_votes >= 1 and is_factcheck)
    if evidence_negative != claim_negative:
        cues = debunk_cues(best) or ["negation"]
        neg_s = next((s for s in top if _negative(s, claim)), best)
        return {**out, "stance": "contradicts", "quote": neg_s[:400],
                "reason": f"States the opposite of the claim ({', '.join(cues[:3])})."}
    claim_nums = [n for n in extract_numbers(claim) if not is_year(n) and not n.get("word")]
    if claim_nums:
        ev_nums = [n for s in top for n in extract_numbers(s) if not is_year(n) and not n.get("word")]
        matched = [c for c in claim_nums if any(abs(c["value"] - e["value"]) < 1e-6 and c["unit"] == e["unit"] for e in ev_nums)]
        if matched:
            return {**out, "stance": "supports", "quote": best[:400], "reason": f"States the same fact with the same figure ({matched[0]['raw']})."}
        if ev_nums:
            return {**out, "stance": "mentions", "number_conflict": [e["raw"] for e in ev_nums[:3]],
                    "reason": f"On topic but gives different figures ({', '.join(e['raw'] for e in ev_nums[:3])}) — needs review."}
    n_terms = len(set(content_tokens(claim)))
    # short claims must match (almost) every key term — "minister bridge collapse" is not enough to confirm a specific claim
    specific = round(coverage(claim, best) * n_terms) >= min(4, n_terms)
    if coverage(claim, best) >= 0.65 and specific:
        if claim_negative and is_factcheck:
            return {**out, "stance": "supports", "quote": best[:400], "reason": "Fact-check states the same negative conclusion as the claim."}
        if is_factcheck and not any(c in best.lower() for c in TRUE_CUES):
            return {**out, "stance": "mentions", "quote": best[:400],
                    "reason": "Fact-check article on a related claim; it does not rate this claim as true."}
        return {**out, "stance": "supports", "quote": best[:400],
                "reason": f"A sentence states the same thing ({coverage(claim, best):.0%} of the claim's key terms) with the same polarity."}
    return {**out, "stance": "mentions", "reason": f"Related coverage ({rel:.0%} term overlap) that neither confirms nor disputes the claim."}


async def assess(claim: str, subclaims: list[dict], items: list[dict]) -> list[dict]:
    heur = {it["id"]: heuristic_stance(claim, it) for it in items}
    if not llm.enabled() or not items:
        return list(heur.values())
    sub_list = "\n".join(f"[{i}] {s['text']}" for i, s in enumerate(subclaims)) or f"[0] {claim}"
    blocks = []
    for it in items:
        blocks.append(f"<evidence id=\"{it['id']}\" source=\"{it['source']}\" type=\"{it['source_type']}\" date=\"{it['published'] or 'unknown'}\">\n"
                      f"{normalize(it['title'] + '. ' + it.get('snippet', '') + ' ' + it.get('passage', ''))[:1100]}\n</evidence>")
    prompt = f"""MAIN CLAIM: {claim}
SUB-CLAIMS:
{sub_list}

EVIDENCE:
{chr(10).join(blocks)}

For EVERY evidence item decide its stance towards the sub-claim it is most relevant to.
stance: "supports" (directly confirms), "contradicts" (directly disputes/debunks), "mentions" (on topic, neither), "irrelevant".
A fact-check article that calls the claim false CONTRADICTS it. Reporting that someone made the claim is "mentions", not "supports".
quote: copy the exact sentence from the evidence text that justifies the stance (verbatim, max 300 chars). Empty for irrelevant.
Return JSON: {{"assessments": [{{"evidence_id": "...", "subclaim": 0, "stance": "...", "quote": "...", "reason": "one short sentence"}}]}}"""
    data = await llm.ask_json("evidence stance classification", prompt, 6000)
    if not isinstance(data, dict):
        return list(heur.values())
    by_id = {it["id"]: it for it in items}
    results = dict(heur)
    for a in data.get("assessments", []):
        eid = a.get("evidence_id")
        if eid not in by_id or a.get("stance") not in ("supports", "contradicts", "mentions", "irrelevant"):
            continue
        it = by_id[eid]
        quote = a.get("quote") or ""
        verified = quote_found(quote, evidence_text(it))
        stance = a["stance"]
        reason = a.get("reason", "")
        if stance in ("supports", "contradicts") and not verified:
            reason = "AI-reported quote could not be found in the source text, so this stance was not counted. " + reason
            stance = "mentions"
        try:
            sub = int(a.get("subclaim", 0))
        except (TypeError, ValueError):
            sub = 0
        results[eid] = {"evidence_id": eid, "subclaim": sub, "stance": stance, "quote": quote if verified else heur[eid]["quote"],
                        "quote_verified": verified or not quote, "reason": reason, "method": "ai", "relevance": heur[eid]["relevance"]}
    return list(results.values())


# ------------------------------------------------------------------ independence

_WIRE_MARKERS = {"pti": "PTI", "ians": "IANS", "ani": "ANI", "reuters": "Reuters", "afp": "AFP", "associated press": "AP", "(ap)": "AP", "press release": "Press release", "pib": "PIB"}


def independence(items: list[dict]) -> dict:
    """Group evidence into independent chains: same outlet, copied text or the same wire copy = one chain.
    'One press release copied by 50 websites is still one source.'"""
    parent = {it["id"]: it["id"] for it in items}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    sh = {it["id"]: shingles(it["title"] + " " + it.get("snippet", "") + " " + it.get("passage", "")[:600]) for it in items}
    wire = {}
    for it in items:
        low = (it.get("snippet", "") + " " + it.get("passage", "")[:600]).lower()
        for marker, name in _WIRE_MARKERS.items():
            if re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", low):
                wire[it["id"]] = name
                break
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if a["domain"] and a["domain"] == b["domain"]:
                union(a["id"], b["id"])
            elif jaccard(sh[a["id"]], sh[b["id"]]) >= 0.45:
                union(a["id"], b["id"])
            elif wire.get(a["id"]) and wire.get(a["id"]) == wire.get(b["id"]):
                union(a["id"], b["id"])
    chains = {}
    for it in items:
        chains.setdefault(find(it["id"]), []).append(it["id"])
    chain_of = {eid: root for root, members in chains.items() for eid in members}
    return {"chains": list(chains.values()), "chain_of": chain_of, "wire": wire}
