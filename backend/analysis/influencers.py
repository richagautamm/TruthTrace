"""Influential People dashboard and dimension 12 (document §12).

Statements by leaders, officials and experts are tracked separately from public opinion and
never counted as votes. Influence is shown on several axes; factual correctness is judged
separately by checking the claims inside a statement against evidence.
"""
from __future__ import annotations

import asyncio
import re

from .. import llm, sources
from ..textutils import normalize
from . import evidence as ev

OFFICE_WORDS = ["prime minister", "president", "minister", "chief minister", "governor", "secretary", "chairman", "chairperson",
                "chief justice", "judge", "commissioner", "director", "spokesperson", "ambassador", "mayor", "senator", "chief of",
                "head of", "ceo", "general", "mp", "mla", "member of parliament", "king", "chancellor"]
EXPERT_WORDS = ["scientist", "professor", "researcher", "economist", "doctor", "physician", "epidemiologist", "expert", "analyst", "astronaut", "engineer"]

_SAID = r"(?:said|says|stated|told|added|asserted|claimed|remarked|tweeted|posted|wrote|announced)"
_NAME = r"([A-Z][a-zA-Z.\-]+(?:\s+[A-Z][a-zA-Z.\-]+){0,3})"
PATTERNS = [
    re.compile(r"[\"“]([^\"”]{15,400})[,.]?[\"”],?\s+" + _SAID + r"\s+" + _NAME),
    re.compile(r"[\"“]([^\"”]{15,400})[,.]?[\"”],?\s+" + _NAME + r"\s+" + _SAID),
    re.compile(_NAME + r"\s+" + _SAID + r"(?:\s+that)?,?\s+[\"“]([^\"”]{15,400})[\"”]"),
]
NOT_PEOPLE = {"The", "He", "She", "They", "It", "This", "That", "We", "Officials", "Police", "Sources", "Reports", "News"}


def extract_statements(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        text = normalize(it.get("text") or it.get("passage") or it.get("snippet", ""))
        for pat_i, pat in enumerate(PATTERNS):
            for m in pat.finditer(text):
                if pat_i == 2:
                    speaker, quote = m.group(1), m.group(2)
                else:
                    quote, speaker = m.group(1), m.group(2)
                speaker = speaker.strip()
                if speaker.split()[0] in NOT_PEOPLE or len(speaker) < 4:
                    continue
                if " " not in speaker:
                    # news style names a person in full once, then by surname: recover the full name from the same text
                    full = re.findall(r"\b((?:[A-Z][a-z]+\s+){1,3}" + re.escape(speaker) + r")\b", text)
                    if full:
                        speaker = max(full, key=len).strip()
                out.append({"speaker": speaker, "quote": quote.strip(), "evidence_id": it["id"], "source": it["source"], "url": it["url"],
                            "date": it["published"], "source_type": it["source_type"], "method": "pattern"})
    uniq = {}
    for s in out:
        uniq.setdefault((s["speaker"].lower(), s["quote"][:60].lower()), s)
    return list(uniq.values())


def _authority(description: str) -> dict:
    d = (description or "").lower()
    office = next((w for w in OFFICE_WORDS if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", d)), None)
    expert = next((w for w in EXPERT_WORDS if w in d), None)
    if office:
        return {"official_authority": f"Holds/held public office ({office}) — authority on this specific issue must be checked against the role.",
                "decision_power": "Potentially high (office holder)" if any(k in d for k in ("prime minister", "president", "minister", "chief minister", "governor")) else "Moderate / role-dependent"}
    if expert:
        return {"official_authority": f"Subject expert ({expert}); no decision-making office indicated.", "decision_power": "Advisory only"}
    return {"official_authority": "No public office or expert role found in the reference description.", "decision_power": "Not established"}


async def profile_people(statements: list[dict], max_people: int = 6) -> list[dict]:
    names = []
    for s in statements:
        if s["speaker"] not in names:
            names.append(s["speaker"])
    names = names[:max_people]
    summaries = await asyncio.gather(*(sources.wikipedia_summary(n) for n in names), return_exceptions=True)
    people = []
    for name, summ in zip(names, summaries):
        summ = summ if isinstance(summ, dict) else None
        own = [s for s in statements if s["speaker"] == name]
        desc = (summ or {}).get("description", "")
        primary = [s for s in own if s["source_type"] == "official"]
        people.append({
            "name": name,
            "role": desc or "Role not found in reference sources",
            "reference": (summ or {}).get("url"),
            "statements": own,
            "influence": {
                **_authority(desc + " " + (summ or {}).get("extract", "")[:300]),
                "public_reach": f"Quoted in {len({s['source'] for s in own})} retrieved outlet(s) in this sample (sample count, not audience size).",
                "policy_impact": "Not assessed — requires tracking what changed after the statement.",
            },
            "primary_source": ("Official source found: " + primary[0]["url"]) if primary else
                              "Original recording/official transcript not located — statement is known only through third-party reports.",
        })
    return people


async def ai_statements(items: list[dict], topic: str) -> list[dict] | None:
    if not llm.enabled() or not items:
        return None
    blocks = "\n".join(f"[{it['id']}] ({it['source']}, {it['published'] or 'undated'}) {normalize(it.get('passage') or it.get('snippet', ''))[:900]}" for it in items)
    data = await llm.ask_json("extract attributed statements by leaders, officials, institutions and experts", f"""TOPIC: {topic}
EVIDENCE:
{blocks}
Extract statements made by named leaders, officials, institutions or experts about the topic.
"quote" must be copied exactly from the evidence text (direct or reported speech). Do not paraphrase into quotes.
For each, list factual claims inside the statement and whether OTHER evidence in this set supports or contradicts them (else "unverified").
Return JSON: {{"statements": [{{"speaker": "...", "role_in_text": "...", "quote": "...", "evidence_id": "E...", "context": "...",
  "factual_claims": [{{"claim": "...", "status": "supported|contradicted|unverified", "evidence_ids": ["E..."]}}]}}]}}""", 5000)
    if not isinstance(data, dict):
        return None
    by_id = {it["id"]: it for it in items}
    out = []
    for s in data.get("statements", []):
        it = by_id.get(s.get("evidence_id"))
        if not it or not ev.quote_found(s.get("quote", ""), ev.evidence_text(it)):
            continue  # untraceable quote: dropped
        claims = []
        for c in s.get("factual_claims", []):
            ids = [i for i in c.get("evidence_ids", []) if i in by_id]
            st = c.get("status") if c.get("status") in ("supported", "contradicted", "unverified") else "unverified"
            claims.append({"claim": c.get("claim", ""), "status": st if ids or st == "unverified" else "unverified", "evidence_ids": ids})
        out.append({"speaker": s.get("speaker", "").strip(), "role_in_text": s.get("role_in_text"), "quote": s["quote"], "context": s.get("context"),
                    "evidence_id": it["id"], "source": it["source"], "url": it["url"], "date": it["published"],
                    "source_type": it["source_type"], "factual_claims": claims, "method": "ai"})
    return out


async def analyse(topic: str, items: list[dict]) -> dict:
    statements = await ai_statements(items, topic)
    if statements is None:
        statements = extract_statements(items)
    people = await profile_people(statements)
    return {
        "people": people,
        "statement_count": len(statements),
        "rule": "Influential statements are a separate scoreboard. They are never added to public opinion as votes, and influence is never treated as accuracy.",
        "method": statements[0]["method"] if statements else ("ai" if llm.enabled() else "pattern"),
    }


async def run(topic: str, status) -> dict:
    items = await ev.gather(topic, status, extra_queries=[topic + " said statement minister official"])
    await ev.enrich(items, topic, limit=10)
    result = await analyse(topic, items)
    return {"topic": topic, "evidence": items, **result}
