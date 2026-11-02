"""Headline and framing distortion (dimension 7) and headline tone used by the cross-source view."""
from __future__ import annotations

import re

from .. import llm
from ..textutils import (
    content_tokens, coverage, entities, extract_numbers, hedge_words, is_year, normalize,
    quotes_in, sensational_words, split_sentences, tokens,
)

CAUSAL = ["because", "due to", "caused", "causes", "leads to", "led to", "results in", "blamed", "behind", "after", "thanks to", "की वजह से", "के कारण"]
PRAISE = {"praise", "praises", "praised", "hails", "hailed", "lauds", "lauded", "applauds", "salutes", "hero", "heroes", "heroic", "thanks", "saves", "saved", "commends", "celebrates", "welcomes", "welcomed", "boost", "success", "historic", "proud"}
CRITICAL = {"slams", "slammed", "criticises", "criticizes", "criticised", "criticized", "attacks", "attacked", "blames", "blamed", "questions", "questioned", "fails", "failure", "failed", "flak", "row", "controversy", "accuses", "accused", "jibe", "dig", "targets", "targeted", "mocks", "opposes", "opposition", "protest", "protests", "concern", "concerns", "backlash", "lapse", "negligence"}


def tone(headline: str) -> dict:
    toks = set(tokens(headline))
    p, c = sorted(toks & PRAISE), sorted(toks & CRITICAL)
    sens = sensational_words(headline)
    if p and not c:
        label = "praise / positive framing"
    elif c and not p:
        label = "critical framing"
    elif p and c:
        label = "mixed framing"
    else:
        label = "neutral / descriptive"
    if headline.strip().endswith("?"):
        label += " (question headline — may insinuate)"
    return {"label": label, "praise_words": p, "critical_words": c, "sensational_words": sens}


def headline_vs_body(headline: str, body: str) -> dict:
    """Does the headline claim more than the article? Is a caveat hidden?"""
    headline, body = normalize(headline), normalize(body)
    findings = []
    if not headline or not body or len(body) < 80:
        return {"status": "not assessed", "findings": [], "match": None,
                "note": "Needs both a headline and the article text."}
    match = coverage(headline, body)
    if match < 0.5:
        findings.append({"severity": "high", "issue": "Headline terms largely absent from the article",
                         "detail": f"Only {match:.0%} of the headline's key words appear in the body."})
    hn = [n for n in extract_numbers(headline) if not is_year(n) and not n.get("word")]
    bn = {round(n["value"], 4) for n in extract_numbers(body) if not n.get("word")}
    for n in hn:
        if round(n["value"], 4) not in bn:
            findings.append({"severity": "high", "issue": "Number in headline not supported by the article",
                             "detail": f"The headline says {n['raw']}; the article never gives this figure."})
    body_low = body.lower()
    for e in entities(headline):
        if e.lower() not in body_low and len(e) > 3:
            findings.append({"severity": "medium", "issue": "Name in headline not found in the article", "detail": f"'{e}' appears only in the headline."})
    hh, bh = set(hedge_words(headline)), set(hedge_words(body))
    if bh and not hh:
        caveat = next((s for s in split_sentences(body) if set(hedge_words(s))), "")
        findings.append({"severity": "medium", "issue": "Caveat hidden",
                         "detail": f"The article is tentative ({', '.join(sorted(bh)[:4])}) but the headline states it as certain.",
                         "quote": caveat[:300]})
    for c in CAUSAL:
        if re.search(rf"(?<!\w){re.escape(c)}(?!\w)", headline.lower()) and not re.search(rf"(?<!\w){re.escape(c)}(?!\w)", body_low):
            findings.append({"severity": "medium", "issue": "Cause-and-effect claim not made in the article",
                             "detail": f"The headline uses '{c}' but the body does not make this causal link."})
            break
    for q in quotes_in(headline):
        if q.lower() not in body_low:
            findings.append({"severity": "high", "issue": "Quoted words not found in the article", "detail": f"“{q}” does not appear in the body."})
    sens = sensational_words(headline)
    if sens:
        findings.append({"severity": "low", "issue": "Sensational wording", "detail": "Loaded words: " + ", ".join(sens)})
    if headline.endswith("?"):
        findings.append({"severity": "low", "issue": "Question headline", "detail": "Question headlines can suggest a claim without stating it."})
    worst = max((3 if f["severity"] == "high" else 2 if f["severity"] == "medium" else 1 for f in findings), default=0)
    status = "misleading headline" if worst == 3 else "possible framing issues" if worst == 2 else "consistent"
    return {"status": status, "match": round(match, 2), "findings": findings}


async def headline_vs_body_ai(headline: str, body: str, base: dict) -> dict:
    if not llm.enabled() or base.get("status") == "not assessed":
        return base
    data = await llm.ask_json("headline versus article body framing check", f"""HEADLINE: {headline}
ARTICLE: {body[:7000]}
Does the headline claim more than the article supports, hide a caveat, imply a cause the article does not state, or leave out context that changes the meaning?
Return JSON: {{"status": "consistent|possible framing issues|misleading headline", "findings": [{{"severity": "high|medium|low", "issue": "...", "detail": "...", "quote": "exact sentence from the ARTICLE or empty"}}]}}""", 2000)
    if not isinstance(data, dict):
        return base
    extra = []
    for f in data.get("findings", []):
        q = f.get("quote") or ""
        if q and q.lower() not in body.lower():
            f["quote"] = ""
        if f.get("severity") in ("high", "medium", "low"):
            f["source"] = "ai"
            extra.append(f)
    out = dict(base)
    out["findings"] = base["findings"] + extra
    order = ["consistent", "possible framing issues", "misleading headline"]
    if data.get("status") in order and order.index(data["status"]) > order.index(base["status"]):
        out["status"] = data["status"]
    return out


def omitted_terms(claim: str, headline: str) -> list[str]:
    ht = set(content_tokens(headline))
    return [w for w in dict.fromkeys(content_tokens(claim)) if w not in ht][:8]
