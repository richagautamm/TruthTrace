"""Perspectives (POV) system and reasoned final conclusion (document §10 and §17).

Facts and viewpoints are kept in separate sections. Viewpoints are found from the actual
arguments in retrieved coverage, not forced into left/right labels. Every fact and every
factual claim inside an argument must cite evidence ids that exist in the retrieved set.
"""
from __future__ import annotations

import re

from .. import llm
from ..textutils import coverage, extract_numbers, normalize, split_sentences
from . import evidence as ev

SUPPORT_CUES = ["support", "supports", "welcome", "welcomed", "benefit", "benefits", "boost", "will help", "necessary", "needed", "praised", "lauded", "in favour", "in favor", "positive", "growth", "security", "strengthen", "समर्थन", "फायदा"]
OPPOSE_CUES = ["oppose", "opposed", "opposes", "criticise", "criticize", "criticised", "criticized", "concern", "concerns", "risk", "risks", "protest", "against", "slammed", "damage", "harm", "threat", "challenge", "petition", "irreversible", "displace", "विरोध", "नुकसान", "चिंता"]


def _has(text: str, cues) -> list[str]:
    low = text.lower()
    return [c for c in cues if re.search(rf"(?<!\w){re.escape(c)}(?!\w)", low)]


def heuristic(topic: str, items: list[dict]) -> dict:
    facts, pro, con = [], [], []
    for it in items:
        txt = it.get("passage") or (it["title"] + ". " + it.get("snippet", ""))
        for s in split_sentences(txt)[:12]:
            if coverage(topic, s) < 0.25 or len(s) < 40:
                continue
            entry = {"text": s[:350], "evidence_ids": [it["id"]], "source": it["source"]}
            p, c = _has(s, SUPPORT_CUES), _has(s, OPPOSE_CUES)
            if it["source_type"] in ("official", "reference", "academic") and extract_numbers(s) and not (p or c):
                facts.append(entry)
            elif p and not c:
                pro.append({**entry, "cues": p})
            elif c and not p:
                con.append({**entry, "cues": c})
    dedupe = lambda arr: list({normalize(x["text"]).lower(): x for x in arr}.values())
    facts, pro, con = dedupe(facts)[:6], dedupe(pro)[:6], dedupe(con)[:6]
    viewpoints = []
    if pro:
        viewpoints.append({"label": "Arguments in favour (keyword-grouped)", "summary": "Sentences from coverage that use supportive language.",
                           "arguments": [{"argument": x["text"], "evidence_ids": x["evidence_ids"], "factual_claims": []} for x in pro]})
    if con:
        viewpoints.append({"label": "Concerns / opposing arguments (keyword-grouped)", "summary": "Sentences from coverage that raise risks or objections.",
                           "arguments": [{"argument": x["text"], "evidence_ids": x["evidence_ids"], "factual_claims": []} for x in con]})
    return {
        "method": "heuristic",
        "issue_dimensions": [],
        "verifiable_facts": [{"fact": f["text"], "evidence_ids": f["evidence_ids"]} for f in facts],
        "viewpoints": viewpoints,
        "values_vs_facts": [],
        "reasoned_conclusion": None,
        "note": "Keyword grouping only. Configure an AI model (see README) for argument-level viewpoints, per-claim evidence checks and a reasoned conclusion. "
                "Without it, TruthTrace does not produce a conclusion rather than guess.",
    }


async def analyse(topic: str, items: list[dict], status=None) -> dict:
    base = heuristic(topic, items)
    if not llm.enabled() or not items:
        return base
    ids = {it["id"] for it in items}
    blocks = "\n".join(f"[{it['id']}] ({it['source']}, {it['source_type']}, {it['published'] or 'undated'}) {it['title']}. "
                       f"{normalize(it.get('passage') or it.get('snippet', ''))[:700]}" for it in items)
    prompt = f"""ISSUE: {topic}
RETRIEVED EVIDENCE (cite by id):
{blocks}

Build a perspectives report. Rules: facts and viewpoints separate; viewpoints come from the real arguments in the evidence
(not left/right labels); the issue may be economic, legal, regional, cultural, humanitarian, environmental, security...
Test every factual claim inside each argument against the evidence; if one side makes a factual claim the evidence contradicts,
say so — neutrality does not mean equal credit. Separate VALUES (priorities, not evidence) from FACTUAL claims.
The conclusion must follow from the evidence, not from a pre-set rule like "country first" or "environment first".
If evidence is insufficient, conclusion must say so.

Return JSON:
{{"issue_dimensions": ["economic|legal|security|environmental|humanitarian|regional|cultural|..."],
 "verifiable_facts": [{{"fact": "...", "evidence_ids": ["E..."]}}],
 "viewpoints": [{{"label": "who holds it, described by their argument", "summary": "...",
    "arguments": [{{"argument": "...", "evidence_ids": ["E..."],
       "factual_claims": [{{"claim": "...", "status": "supported|contradicted|unverified", "evidence_ids": ["E..."]}}]}}]}}],
 "values_vs_facts": [{{"statement": "...", "kind": "value|factual claim", "note": "..."}}],
 "open_questions": ["questions that must be answered before deciding"],
 "reasoned_conclusion": {{"conclusion": "Based on the available evidence ... (or: evidence is insufficient to conclude ...)",
    "evidence_for": ["..."], "evidence_against": ["..."], "alternatives_compared": ["..."],
    "priority_scenarios": [{{"priority": "If X is the top priority", "outcome": "..."}}],
    "could_change_conclusion": ["specific unanswered questions"], "confidence": "low|medium|high (trust the evidence deserves)"}}}}"""
    data = await llm.ask_json("perspectives and reasoned conclusion", prompt, 6000)
    if not isinstance(data, dict):
        return base

    def keep_ids(lst):
        return [i for i in (lst or []) if i in ids]

    facts = [{"fact": f.get("fact", ""), "evidence_ids": keep_ids(f.get("evidence_ids"))} for f in data.get("verifiable_facts", [])]
    facts = [f for f in facts if f["fact"] and f["evidence_ids"]]  # a "fact" with no evidence is not shown as a fact
    vps = []
    for vp in data.get("viewpoints", []):
        args = []
        for a in vp.get("arguments", []):
            claims = []
            for c in a.get("factual_claims", []):
                cids = keep_ids(c.get("evidence_ids"))
                status_ = c.get("status") if c.get("status") in ("supported", "contradicted", "unverified") else "unverified"
                if status_ != "unverified" and not cids:
                    status_ = "unverified"
                claims.append({"claim": c.get("claim", ""), "status": status_, "evidence_ids": cids})
            args.append({"argument": a.get("argument", ""), "evidence_ids": keep_ids(a.get("evidence_ids")), "factual_claims": claims})
        vps.append({"label": vp.get("label", "Viewpoint"), "summary": vp.get("summary", ""), "arguments": args})
    return {
        "method": "ai",
        "issue_dimensions": data.get("issue_dimensions", []),
        "verifiable_facts": facts,
        "viewpoints": vps,
        "values_vs_facts": data.get("values_vs_facts", []),
        "open_questions": data.get("open_questions", []),
        "reasoned_conclusion": data.get("reasoned_conclusion"),
        "note": "Viewpoints and conclusion drafted by the AI model from the retrieved evidence only; evidence ids that did not exist were removed.",
    }


async def run(topic: str, status) -> dict:
    items = await ev.gather(topic, status, extra_queries=[topic + " criticism concerns", topic + " benefits government says"])
    await ev.enrich(items, topic, limit=8)
    result = await analyse(topic, items, status)
    return {"topic": topic, "evidence": items, **result}
