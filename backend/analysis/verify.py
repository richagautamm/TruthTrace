"""The full verification pipeline (document §16): input → claim extraction → evidence → source and
context → language and framing → media → evidence combination → reader dashboard.

Each of the 12 dimensions is computed on its own first (§14). Only then is the overall verdict
built, by explicit rules that are returned with the result so the reader can see exactly why.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timezone

from .. import db, llm, sources
from ..textutils import (
    classify_source, content_tokens, detect_language, domain_of, extract_numbers, is_year, normalize,
    region_of, sensational_words,
)
from . import claims as claims_mod
from . import evidence as ev
from . import framing, influencers, media as media_mod, meaning, opinion, perspectives

VERDICTS = ["Supported", "Contradicted", "Misleading Context or Framing", "Meaning Altered", "Insufficient Evidence"]
STRONG = ev.STRONG_TYPES


def _now():
    return datetime.now(timezone.utc)


def _days_ago(iso: str | None):
    if not iso:
        return None
    try:
        return (_now() - datetime.fromisoformat(iso)).days
    except ValueError:
        return None


def _dim(num, name, question, status, rating, summary, findings=None, method="", evidence_ids=None, data=None):
    return {"id": num, "name": name, "question": question, "status": status, "rating": rating, "summary": summary,
            "findings": findings or [], "method": method, "evidence_ids": evidence_ids or [], "data": data or {}}


# --------------------------------------------------------------------------- dimensions

def dim_claim_accuracy(info, sub_status):
    checkable = [s for s in sub_status if s["checkable"]]
    ver = sum(1 for s in checkable if s["status"] == "supported")
    con = sum(1 for s in checkable if s["status"] == "contradicted")
    unk = len(checkable) - ver - con
    not_checkable = [s for s in sub_status if not s["checkable"]]
    rating = f"{ver}/{len(checkable)} verified" if checkable else "No checkable sub-claims"
    findings = [f"[{s['status'].upper()}] {s['text']}" for s in sub_status]
    if not_checkable:
        findings.append(f"{len(not_checkable)} statement(s) are opinions/values/predictions and are not scored as facts.")
    return _dim(1, "Claim accuracy", "What exactly is claimed? Is each small statement supported?", "assessed", rating,
                f"{ver} verified, {con} contradicted, {unk} unknown out of {len(checkable)} checkable sub-claims.",
                findings, "Verified sub-claims ÷ total checkable sub-claims, with counts of contradicted and unknown.",
                data={"verified": ver, "contradicted": con, "unknown": unk, "total": len(checkable), "subclaims": sub_status})


def dim_source_reliability(input_source, items, stance_by_id, track):
    rows = []
    for it in items:
        st = stance_by_id.get(it["id"], {}).get("stance")
        if st in ("supports", "contradicts", "mentions"):
            p = it.get("page") or {}
            rows.append({"source": it["source"], "domain": it["domain"], "type": it["source_type"], "region": it["region"], "stance": st,
                         "signals": {"byline": bool(p.get("author")), "dated": bool(it["published"]), "links_to_primary": len(p.get("primary_links") or []),
                                     "correction_note": bool(p.get("has_correction_note"))} if p else None,
                         "track_record": track.get(it["domain"])})
    types = Counter(r["type"] for r in rows)
    findings = [f"{n} × {t}" for t, n in types.most_common()]
    summary = "Evidence comes from " + (", ".join(f"{n} {t}" for t, n in types.most_common()) if rows else "no relevant sources") + "."
    rating = "Primary/official sources present" if types.get("official") or types.get("academic") else \
             "Independent fact-checkers present" if types.get("fact-check") else \
             "Secondary news/web only" if rows else "No relevant sources"
    if input_source:
        s = input_source
        summary = f"Submitted source {s['domain'] or '(none)'} — type: {s['type']}. " + summary
        if s.get("signals"):
            sig = s["signals"]
            findings.insert(0, f"Submitted page: byline {'yes' if sig['byline'] else 'no'}, publication date {'yes' if sig['dated'] else 'no'}, "
                               f"links to primary sources {sig['links_to_primary']}, correction note {'yes' if sig['correction_note'] else 'no'}.")
        if s.get("track_record"):
            t = s["track_record"]
            findings.insert(1, f"TruthTrace history for {s['domain']}: seen in {t['n']} earlier checks; backed claims later rated Contradicted {t['backed_false'] or 0} time(s).")
    return _dim(2, "Source reliability", "Who is the source? Original evidence, corrections, transparency?", "assessed", rating, summary, findings,
                "Source type from domain rules (official, fact-check, academic, wire, reference, news/web, social), page transparency signals "
                "(byline, date, links to primary sources, correction notes) and TruthTrace's own history per domain. A famous brand name alone proves nothing.",
                data={"sources": rows[:20], "input_source": input_source})


def dim_traceability(items, stance_by_id, input_source):
    rel = [it for it in items if stance_by_id.get(it["id"], {}).get("stance") in ("supports", "contradicts", "mentions")]
    dated = sorted([it for it in rel if it["published"]], key=lambda x: x["published"])
    primary = [it for it in rel if it["source_type"] in ("official", "academic")]
    linked_primary = sorted({d for it in rel for d in ((it.get("page") or {}).get("primary_links") or [])})
    outlets = {it["domain"] for it in rel}
    findings = []
    if dated:
        e = dated[0]
        findings.append(f"Earliest known appearance in searched indexes: {e['source']} on {e['published'][:10]} — “{e['title'][:120]}”.")
    if primary:
        findings.append("Primary/official source(s) found: " + ", ".join(sorted({p['domain'] for p in primary})))
    if linked_primary:
        findings.append("Articles link to primary domains: " + ", ".join(linked_primary[:6]))
    undated = len(rel) - len(dated)
    if undated:
        findings.append(f"{undated} relevant item(s) have no publication date — gaps in the history.")
    if primary or linked_primary:
        rating = "High — primary source located"
    elif dated:
        rating = "Medium — earliest report located, original item not found"
    elif rel:
        rating = "Low — only undated or secondary reports"
    else:
        rating = "Source not traced — authenticity unverified"
    return _dim(3, "Original-source traceability", "Where did it first appear? Is the original speech/document/post/footage available?", "assessed",
                rating, f"{len(outlets)} distinct outlet(s) in the chain. The earliest source found is not always the true original.", findings,
                "Earliest dated appearance, availability of an official/primary item, links to primary sources, and undated gaps.",
                [e["id"] for e in dated[:1]] + [p["id"] for p in primary[:3]],
                data={"earliest": dated[0] if dated else None, "primary": [p["url"] for p in primary], "outlets": len(outlets)})


def dim_corroboration(items, stance_by_id, indep):
    chains_sup, chains_con = set(), set()
    for it in items:
        st = stance_by_id.get(it["id"], {}).get("stance")
        if st == "supports":
            chains_sup.add(indep["chain_of"][it["id"]])
        elif st == "contradicts":
            chains_con.add(indep["chain_of"][it["id"]])
    n_sup_items = sum(1 for it in items if stance_by_id.get(it["id"], {}).get("stance") == "supports")
    wires = Counter(indep["wire"].values())
    findings = [f"{n_sup_items} supporting item(s) collapse into {len(chains_sup)} independent chain(s)."]
    if wires:
        findings.append("Wire/press-release copies merged: " + ", ".join(f"{k} ×{v}" for k, v in wires.items()))
    findings.append(f"{len(chains_con)} independent chain(s) dispute the claim.")
    rating = "Strong" if len(chains_sup) >= 3 else "Some" if len(chains_sup) >= 2 else "Single chain" if chains_sup else "None"
    return _dim(4, "Independent corroboration", "Do several truly independent sources support the same fact?", "assessed", rating,
                f"{len(chains_sup)} independent supporting chain(s); one press release copied by 50 websites is still one source.", findings,
                "Same outlet, near-duplicate text (shingle overlap ≥ 45%) or the same wire copy are merged into one chain.",
                data={"support_chains": len(chains_sup), "contradict_chains": len(chains_con), "chains": indep["chains"]})


def _quality(it, s):
    score, parts = 0, []
    if s["stance"] in ("supports", "contradicts") and s.get("quote_verified"):
        score += 2; parts.append("direct, quote verified")
    if it["source_type"] in STRONG:
        score += 2; parts.append(it["source_type"])
    if it.get("text"):
        score += 1; parts.append("full text read")
    d = _days_ago(it["published"])
    if d is not None and d < 730:
        score += 1; parts.append("recent")
    return score, parts


def dim_contradiction(items, stance_by_id):
    sup, con = [], []
    for it in items:
        s = stance_by_id.get(it["id"])
        if not s or s["stance"] not in ("supports", "contradicts"):
            continue
        q, parts = _quality(it, s)
        row = {"id": it["id"], "source": it["source"], "quality": q, "quality_factors": parts, "quote": s["quote"], "reason": s["reason"]}
        (sup if s["stance"] == "supports" else con).append(row)
    ws, wc = sum(r["quality"] for r in sup), sum(r["quality"] for r in con)
    if not sup and not con:
        rating, summary = "No direct evidence", "No retrieved item directly supports or contradicts the claim."
    elif sup and con:
        rating = "Conflicting — stronger evidence supports" if ws > wc * 1.5 else "Conflicting — stronger evidence contradicts" if wc > ws * 1.5 else "Unresolved contradiction"
        summary = f"Supporting evidence weight {ws} vs contradicting {wc}."
    else:
        rating = "Only supporting evidence" if sup else "Only contradicting evidence"
        summary = f"{len(sup) or len(con)} direct item(s), total quality weight {ws or wc}."
    return _dim(5, "Contradiction and evidence quality", "Which evidence supports, which opposes, and which is stronger?", "assessed", rating, summary,
                [f"+ {r['source']}: {r['quote'][:160]}" for r in sup[:4]] + [f"− {r['source']}: {r['quote'][:160]}" for r in con[:4]],
                "Quality = directness (verified quote) + source type + full text read + recency. Shown, not averaged into a truth score.",
                [r["id"] for r in sup + con], data={"supporting": sup, "contradicting": con, "weight_support": ws, "weight_contradict": wc})


def dim_translation(lang, translation, meaning_result, claim_text, items, stance_by_id):
    findings, status = [], "assessed"
    if translation and translation.get("method") not in (None, "none (already English)"):
        findings.append(f"Input language: {lang}. Translated to English by {translation['method']}: “{translation['english'][:200]}”")
    conflicts = []
    for it in items:
        s = stance_by_id.get(it["id"], {})
        if s.get("number_conflict"):
            conflicts.append(f"{it['source']} reports {', '.join(s['number_conflict'])}")
    nums = [n["raw"] for n in extract_numbers(claim_text) if not is_year(n) and not n.get("word")]
    if meaning_result:
        rating = meaning_result["verdict"]
        summary = f"Original vs circulating version: {meaning_result['label']}."
        findings += [f"[{c['severity']}] {c['type']}: {c['explanation']}" for c in meaning_result["changes"][:6]]
    elif conflicts:
        rating, summary = "Potential mismatch", f"The claim's figure(s) {', '.join(nums)} differ from figures in some sources."
        findings += conflicts[:5]
    elif lang not in ("en", "unknown") or nums:
        rating = "No mismatch detected"
        summary = "Numbers, negation and certainty in the claim were checked against source wording" + (f" (figures: {', '.join(nums)})." if nums else ".")
    else:
        status, rating, summary = "limited", "No original/translated version supplied", \
            "Add the original statement (e.g. the Hindi original) in the 'Original version' field to check translation drift."
    return _dim(6, "Translation and meaning accuracy", "Did translation or rewriting change meaning, certainty, subject or numbers?", status, rating, summary,
                findings, "Deterministic comparison of numbers, negation, 'may' vs 'did', named people/places, figurative vs literal wording (+ AI check when configured).",
                data={"meaning": meaning_result, "translation": translation, "number_conflicts": conflicts})


def dim_headline(headline_check, items, stance_by_id):
    rel = [it for it in items if stance_by_id.get(it["id"], {}).get("stance") in ("supports", "contradicts", "mentions") and it["via"] != "Wikipedia"]
    sens = [it for it in rel if sensational_words(it["title"])]
    findings = []
    if headline_check and headline_check.get("status") != "not assessed":
        findings += [f"[{f['severity']}] {f['issue']}: {f['detail']}" for f in headline_check["findings"]]
        rating = headline_check["status"].capitalize()
        summary = f"Submitted headline vs article body: {headline_check['status']} (headline–body term match {headline_check['match']:.0%})."
        status = "assessed"
    else:
        rating, status = "Coverage headlines only", "limited"
        summary = "No headline + article pair submitted, so only coverage headlines were screened."
    if rel:
        findings.append(f"{len(sens)} of {len(rel)} related coverage headlines use sensational wording.")
    return _dim(7, "Headline and framing distortion", "Does the headline claim more than the article? Is a caveat hidden?", status, rating, summary, findings,
                "Headline–body term match, numbers/names/quotes only in the headline, hidden hedges, unsupported cause-and-effect, loaded words.",
                data={"headline_check": headline_check, "sensational_headlines": [{"source": i["source"], "title": i["title"], "words": sensational_words(i["title"])} for i in sens[:8]]})


def dim_time(info, items, stance_by_id, input_source, media_result):
    rel = sorted([it for it in items if it["published"] and stance_by_id.get(it["id"], {}).get("stance") in ("supports", "contradicts", "mentions")],
                 key=lambda x: x["published"])
    findings, flags = [], []
    if rel:
        findings.append(f"Related coverage spans {rel[0]['published'][:10]} → {rel[-1]['published'][:10]}.")
        age, latest_age = _days_ago(rel[0]["published"]), _days_ago(rel[-1]["published"])
        if info.get("claims_recency") and age is not None and age > 60 and latest_age is not None and latest_age > 30:
            flags.append(f"The item presents the event as new, but all related coverage found is older: {rel[0]['published'][:10]} to {rel[-1]['published'][:10]} "
                         f"(newest {latest_age} days ago). Possible old story shared as new.")
    if input_source and input_source.get("published"):
        findings.append(f"Submitted article published {input_source['published'][:10]}" + (f", updated {input_source['modified'][:10]}" if input_source.get("modified") else "") + ".")
    corrected = [it for it in items if (it.get("page") or {}).get("has_correction_note")]
    if corrected:
        findings.append("Correction/update notes found in: " + ", ".join(it["source"] for it in corrected[:4]))
    years = sorted({int(n["value"]) for n in extract_numbers(info.get("main_claim", "")) if is_year(n)})
    if years:
        findings.append("Years named in the claim: " + ", ".join(map(str, years)))
    if media_result and media_result.get("label", "").startswith("Possible authentic media, false context"):
        flags.append(media_result["reasons"][0])
    findings = flags + findings
    rating = "Context mismatch suspected" if flags else ("No timeline conflict found" if rel else "Not enough dated evidence")
    return _dim(8, "Time and context accuracy", "Is an old event shown as new? Are the place, date, or order of events wrong?", "assessed" if rel or flags else "limited",
                rating, flags[0] if flags else "Publication dates of the claim and the evidence were compared.", findings,
                "Publication time vs earliest coverage, 'new/today' wording vs evidence age, corrections/updates, media creation date.",
                data={"flags": flags})


def dim_media(media_result):
    if not media_result or media_result.get("kind") not in ("image", "video"):
        return _dim(9, "Image and video authenticity", "AI-generated, edited, spliced, old, from another place, or real footage with a false caption?",
                    "not applicable", "Not applicable", "No image or video submitted — under the module rule, a text verdict never stands in for a media check.",
                    method="Five checks (§8): credentials, reverse search, manipulation signs, scene verification, audio/transcript.")
    return _dim(9, "Image and video authenticity", "AI-generated, edited, spliced, old, from another place, or real footage with a false caption?", "assessed",
                media_result["label"], media_result["reasons"][0], [f"{c['title']}: {c['result'] if isinstance(c['result'], str) else 'see details'}"
                                                                    for c in media_result["checks"].values()],
                "Content credentials & generator metadata, reverse-search keyframes, error-level/cut analysis (indicators only), metadata dates.", data={"media": media_result})


def dim_audio(media_result, info, items):
    qs = [q.get("quote") for q in info.get("quotes", []) if q.get("quote")]
    if media_result and media_result.get("kind") == "audio":
        return _dim(10, "Audio and speaker authenticity", "Cloned voice, spliced speech, dubbing, altered transcript, or made-up quote?", "assessed",
                    media_result["label"], media_result["reasons"][0], [f"{c['title']}: {c['result'] if isinstance(c['result'], str) else 'see details'}" for c in media_result["checks"].values()],
                    "Audio origin, file metadata, transcript comparison against an original recording.", data={"media": media_result})
    if not qs:
        return _dim(10, "Audio and speaker authenticity", "Cloned voice, spliced speech, dubbing, altered transcript, or made-up quote?", "not applicable",
                    "Not applicable", "No audio and no direct quote in the item.", method="Quote tracing and transcript match.")
    findings, found_any = [], 0
    for q in qs[:4]:
        hits = [it for it in items if ev.quote_found(q, ev.evidence_text(it))]
        official = [h for h in hits if h["source_type"] == "official"]
        if hits:
            found_any += 1
            findings.append(f"“{q[:100]}” found in {len(hits)} source(s)" + (f", including official {official[0]['domain']}" if official else " (secondary reports only)") + ".")
        else:
            findings.append(f"“{q[:100]}” — not found in any retrieved source. Quote unverified.")
    rating = "Quote traced" if found_any == len(qs[:4]) else "Quote partly traced" if found_any else "Quote not traced"
    return _dim(10, "Audio and speaker authenticity", "Cloned voice, spliced speech, dubbing, altered transcript, or made-up quote?", "assessed", rating,
                "Exact quotes were searched for in the retrieved sources; a quote seen only on third-party sites still needs the original recording/transcript.",
                findings, "Exact-wording match of each quote against retrieved texts.")


def dim_spread(items, stance_by_id, social, gdelt):
    rel = [it for it in items if stance_by_id.get(it["id"], {}).get("stance") in ("supports", "contradicts", "mentions")]
    by_day = Counter(it["published"][:10] for it in rel if it["published"])
    langs = Counter(it["lang"] for it in rel)
    findings = [f"{len(rel)} related news/web items from {len({i['domain'] for i in rel})} outlets; languages: " + ", ".join(f"{k} {v}" for k, v in langs.items())]
    if social:
        s = social["sample"]
        findings.append(f"Sampled online discussion: {s['on_topic']} on-topic posts, {s['unique_accounts']} unique accounts after removing "
                        f"{s['exact_duplicates_removed']} duplicate(s) and {s['near_duplicates_removed']} copied post(s); "
                        f"{len(s['bot_like_accounts'])} bot-like and {len(s['high_volume_accounts'])} high-volume account(s) flagged.")
    if gdelt:
        findings.append(f"GDELT global news volume series available ({len(gdelt)} points, last 3 months).")
    else:
        findings.append("Global news volume (GDELT) unavailable for this query — trend status not claimed.")
    trend = "Trend data available" if gdelt and sum(p["count"] for p in gdelt) > 0 else "Trend not established"
    return _dim(11, "Public sentiment and spread", "How many people support or oppose? Is it really trending? Is content repeated?", "assessed", trend,
                "Counts unique accounts, not posts. This is not a truth score — popularity never makes a claim true.", findings,
                "Unique accounts, mentions over time, duplicate/bot signs, sample size and sources. Ten search results do not mean something is viral.",
                data={"news_by_day": [{"date": d, "count": n} for d, n in sorted(by_day.items())], "gdelt": gdelt, "social": social})


def dim_statements(infl):
    people = infl.get("people", [])
    if not people:
        return _dim(12, "Expert, official and influential statements", "What exactly did the leader, department, scientist or expert say?", "limited",
                    "No attributed statements found", "No named person's statement about this claim was found in the retrieved text.",
                    method="Attributed quotes extracted from sources, speaker role from Wikipedia, primary-source check.")
    findings = []
    for p in people[:5]:
        findings.append(f"{p['name']} ({p['role']}): {len(p['statements'])} statement(s). {p['primary_source']}")
    return _dim(12, "Expert, official and influential statements", "What exactly did the leader, department, scientist or expert say?", "assessed",
                f"{len(people)} speaker(s) identified", infl["rule"], findings,
                "Exact quote, source, role (Wikipedia), official authority, decision power, reach in sample; influence never mixed with correctness.",
                data={"people": people})


# --------------------------------------------------------------------------- verdict

def build_verdict(*, items, stance_by_id, indep, meaning_result, headline_check, time_dim, media_result, ai_mode, has_claim):
    trace = []
    sup_chains, con_chains, strong_sup, strong_con = set(), set(), set(), set()
    for it in items:
        s = stance_by_id.get(it["id"], {})
        ch = indep["chain_of"].get(it["id"])
        if s.get("stance") == "supports":
            sup_chains.add(ch)
            if it["source_type"] in STRONG:
                strong_sup.add(ch)
        elif s.get("stance") == "contradicts":
            con_chains.add(ch)
            if it["source_type"] in STRONG:
                strong_con.add(ch)
    trace.append(f"Supporting: {len(sup_chains)} independent chain(s) ({len(strong_sup)} from official/fact-check/academic/reference/wire sources). "
                 f"Contradicting: {len(con_chains)} chain(s) ({len(strong_con)} strong).")

    misleading_reasons = []
    if headline_check and headline_check.get("status") == "misleading headline":
        misleading_reasons.append("headline claims more than its article")
    if time_dim["data"].get("flags"):
        misleading_reasons.append("possible old event presented as new")
    if media_result and media_result.get("label", "").startswith(("Synthetic media", "Possible authentic media, false context")):
        misleading_reasons.append("media: " + media_result["label"])
    # A timing mismatch with *related* coverage is only a warning on its own (recurring events like floods are common);
    # a misleading headline or the media file's own metadata is direct evidence about this item.
    direct_misleading = [r for r in misleading_reasons if not r.startswith("possible old event")]

    verdict = None
    if meaning_result and meaning_result["verdict"] == "Meaning Altered":
        verdict = "Meaning Altered"
        trace.append("Rule 1 → the circulating version changes the meaning of the supplied original (" + meaning_result["label"] + ").")
    elif not has_claim:
        verdict = "Insufficient Evidence"
        trace.append("Rule 0 → no claim text to check against evidence.")
    else:
        # weight = independent chains + 2 × chains backed by a strong source type
        sup_w, con_w = len(sup_chains) + 2 * len(strong_sup), len(con_chains) + 2 * len(strong_con)
        trace.append(f"Evidence weight (chains + 2 × strong chains): support {sup_w}, contradict {con_w}. A side must have at least twice the other's weight to win.")
        contra_ok = len(strong_con) >= 1 or len(con_chains) >= 2
        support_ok = len(sup_chains) >= 2 or len(strong_sup) >= 1
        contra = contra_ok and con_w >= 2 * sup_w
        support = support_ok and sup_w >= 2 * con_w and not contra
        if contra:
            verdict = "Contradicted"
            trace.append("Rule 2 → reliable evidence disputes the claim and clearly outweighs support"
                         + (f" ({len(sup_chains)} weaker chain(s) still support it — see Section A)." if sup_chains else "."))
        elif (contra_ok or support_ok) and not support:
            verdict = "Insufficient Evidence"
            trace.append("Rule 2b → sources disagree and neither side clearly outweighs the other; the contradiction is unresolved, so no side is declared.")
        elif support and misleading_reasons:
            verdict = "Misleading Context or Framing"
            trace.append("Rule 3 → facts are supported, but " + "; ".join(misleading_reasons) + ".")
        elif support:
            verdict = "Supported"
            trace.append("Rule 4 → at least two independent chains (or one strong source) support the claim and no strong source disputes it.")
        elif direct_misleading:
            verdict = "Misleading Context or Framing"
            trace.append("Rule 5 → " + "; ".join(direct_misleading) + " — the item itself shows the false impression, even though its core facts are not established.")
        else:
            verdict = "Insufficient Evidence"
            trace.append("Rule 6 → evidence is not enough to support or contradict the claim. Source not found is not proof of falsehood.")
            if misleading_reasons:
                trace.append("Warning (not used for the verdict): " + "; ".join(misleading_reasons) + " — see dimension 8.")

    pro = verdict in ("Supported", "Misleading Context or Framing")
    winning = sup_chains if pro else con_chains if verdict == "Contradicted" else set()
    strong_win = strong_sup if pro else strong_con if verdict == "Contradicted" else set()
    win_w = len(winning) + 2 * len(strong_win)
    opp_w = (len(con_chains) + 2 * len(strong_con)) if pro else (len(sup_chains) + 2 * len(strong_sup))
    ratio = win_w / opp_w if opp_w else float("inf")
    if verdict == "Insufficient Evidence":
        confidence = "Not applicable"
    elif verdict == "Meaning Altered":
        confidence = "High" if any(c["severity"] == "high" and c.get("source") != "ai" for c in meaning_result["changes"]) else "Medium"
    elif len(winning) >= 3 and len(strong_win) >= 2 and ratio >= 4:
        confidence = "High"
    elif (len(winning) >= 2 or strong_win) and ratio >= 2:
        confidence = "Medium"
    else:
        confidence = "Low"
    if confidence == "High" and not ai_mode and verdict in ("Supported", "Contradicted"):
        confidence = "Medium"
        trace.append("Confidence capped at Medium: stance was detected by keyword heuristics (no AI model configured).")
    return {"verdict": verdict, "confidence": confidence, "rule_trace": trace,
            "counts": {"support_chains": len(sup_chains), "contradict_chains": len(con_chains), "strong_support": len(strong_sup), "strong_contradict": len(strong_con)},
            "confidence_definition": "How much trust the available evidence deserves: High = ≥3 independent chains incl. ≥2 strong sources and ≥4× the opposing weight; "
                                     "Medium = ≥2 chains (or a strong source) and ≥2× the opposing weight; otherwise Low. It is not how confident the AI feels."}


# --------------------------------------------------------------------------- sub-claims

def subclaim_status(info, items, stances, ai_mode):
    subs = info.get("subclaims") or [{"text": info["main_claim"], "type": "fact", "checkable": True}]
    out = []
    for i, s in enumerate(subs):
        checkable = bool(s.get("checkable", s.get("type") == "fact")) and s.get("type", "fact") == "fact"
        if ai_mode:
            rel = [x for x in stances if x.get("subclaim") == i]
        else:
            rel = [ev.heuristic_stance(s["text"], it) for it in items] if checkable else []
        sup = [x for x in rel if x["stance"] == "supports"]
        con = [x for x in rel if x["stance"] == "contradicts"]
        status = "contradicted" if con and len(con) >= len(sup) else "supported" if sup else "unknown"
        if not checkable:
            status = s.get("type", "opinion")
        out.append({"text": s["text"], "type": s.get("type", "fact"), "checkable": checkable, "status": status,
                    "evidence_ids": [x["evidence_id"] for x in (sup + con)][:6]})
    return out


# --------------------------------------------------------------------------- main

async def run(text: str = "", headline: str = "", url: str = "", original_text: str = "", media: dict | None = None) -> dict:
    status = sources.SourceStatus()
    text, headline, original_text = normalize(text), normalize(headline), normalize(original_text)
    input_source, page_body = None, ""
    if url:
        page = await sources.fetch_article(url)
        d = domain_of(url)
        input_source = {"url": url, "domain": d, "type": classify_source(d, url), "region": region_of(d), "fetched": page.get("ok", False)}
        if page.get("ok"):
            headline = headline or page["title"]
            page_body = page["text"]
            text = text or normalize(page["text"][:2500])
            input_source.update({"published": page.get("published"), "modified": page.get("modified"), "title": page["title"],
                                 "signals": {"byline": bool(page.get("author")), "dated": bool(page.get("published")),
                                             "links_to_primary": len(page.get("primary_links") or []), "correction_note": page.get("has_correction_note")}})
        else:
            input_source["error"] = page.get("reason")
    claim_text = text or headline
    mkind = media_mod.media_kind(media["filename"], media["content_type"]) if media else None
    lang = detect_language(claim_text) if claim_text else "unknown"
    translation = await meaning.to_english(claim_text) if lang not in ("en", "unknown") else None
    english = translation["english"] if translation else claim_text

    info = await claims_mod.extract(claim_text or "(no text)", headline if text else "", lang, mkind, bool(original_text), english)
    tests = claims_mod.select_tests(info["misinformation_types"], mkind)

    media_result = None
    if media:
        media_result = await asyncio.to_thread(media_mod.analyse, media["data"], media["filename"], media["content_type"], bool(info.get("claims_recency")))

    has_claim = bool(claim_text)
    items, social, gdelt = [], None, None
    if has_claim:
        query = info.get("search_query") or " ".join(content_tokens(english)[:9])
        hq = " ".join(content_tokens(claim_text)[:8]) if lang in ("hi", "mixed") else None
        gathered, social_res = await asyncio.gather(
            ev.gather(query, status, hindi_query=hq), opinion.run(query, status), return_exceptions=True)
        items = gathered if isinstance(gathered, list) else []
        social = social_res if isinstance(social_res, dict) else None
        gdelt = social.get("news_volume") if social else None
        await ev.enrich(items, info["main_claim"])
    stances = await ev.assess(info["main_claim"], info.get("subclaims", []), items)
    main_stance = {}
    for s in stances:
        main_stance[s["evidence_id"]] = s
    indep = ev.independence(items)

    meaning_result = await meaning.compare(original_text, claim_text) if original_text and claim_text else None
    headline_check = None
    body = page_body or (text if headline and text and len(text) > len(headline) + 40 else "")
    if headline and body:
        headline_check = await framing.headline_vs_body_ai(headline, body, framing.headline_vs_body(headline, body))

    relevant_items = [it for it in items if main_stance.get(it["id"], {}).get("stance") != "irrelevant"]
    infl_task = influencers.analyse(info["main_claim"], relevant_items[:14])
    persp_task = perspectives.analyse(info["main_claim"], relevant_items) if info.get("topic") == "political/policy" and relevant_items else None
    infl = await infl_task
    persp = await persp_task if persp_task else None

    track ={it["domain"]: db.domain_track_record(it["domain"]) for it in relevant_items if it["domain"]}
    if input_source and input_source.get("domain"):
        input_source["track_record"] = db.domain_track_record(input_source["domain"])
    ai_mode = llm.enabled()
    sub_status = subclaim_status(info, items, stances, ai_mode)

    time_dimension = dim_time(info, items, main_stance, input_source, media_result)
    dimensions = [
        dim_claim_accuracy(info, sub_status),
        dim_source_reliability(input_source, items, main_stance, track),
        dim_traceability(items, main_stance, input_source),
        dim_corroboration(items, main_stance, indep),
        dim_contradiction(items, main_stance),
        dim_translation(lang, translation, meaning_result, english, items, main_stance),
        dim_headline(headline_check, items, main_stance),
        time_dimension,
        dim_media(media_result),
        dim_audio(media_result, info, items),
        dim_spread(items, main_stance, social, gdelt),
        dim_statements(infl),
    ]
    verdict = build_verdict(items=items, stance_by_id=main_stance, indep=indep, meaning_result=meaning_result, headline_check=headline_check,
                            time_dim=time_dimension, media_result=media_result, ai_mode=ai_mode, has_claim=has_claim)

    supporting = [dict(it, stance=main_stance[it["id"]]) for it in items if main_stance.get(it["id"], {}).get("stance") == "supports"]
    contradicting = [dict(it, stance=main_stance[it["id"]]) for it in items if main_stance.get(it["id"], {}).get("stance") == "contradicts"]
    open_q = [f"Unverified: {s['text']}" for s in sub_status if s["checkable"] and s["status"] == "unknown"]
    if dimensions[2]["rating"].startswith(("Low", "Source not traced", "Medium")):
        open_q.append("The original statement, document, post or recording has not been located.")
    if verdict["counts"]["support_chains"] and verdict["counts"]["contradict_chains"]:
        open_q.append("Sources disagree — check the contradicting items below.")
    if media_result and media_result.get("label") == "Media authenticity inconclusive":
        open_q.append("Media origin not established — run the reverse searches listed in the media panel.")

    result = {
        "input": {"text": text, "headline": headline, "url": url, "original_text": original_text, "language": lang,
                  "english": english if translation else None, "media": media["filename"] if media else None},
        "input_source": input_source,
        "ai": llm.provider_info(),
        "claim": info,
        "tests": tests,
        "verdict": verdict,
        "result_parts": [
            {"part": "Factual claim status", "value": dimensions[0]["rating"] + " — " + dimensions[4]["rating"]},
            {"part": "Source traceability", "value": dimensions[2]["rating"]},
            {"part": "Media authenticity", "value": dimensions[8]["rating"]},
            {"part": "Context and translation", "value": dimensions[7]["rating"] + "; " + dimensions[5]["rating"]},
            {"part": "Overall conclusion", "value": verdict["verdict"] + (f" (confidence: {verdict['confidence']})" if verdict["confidence"] != "Not applicable" else "")},
        ],
        "dimensions": dimensions,
        "evidence": items,
        "stances": main_stance,
        "independence": {"chains": indep["chains"], "wire": indep["wire"]},
        "sections": {
            "A": {"supporting": supporting, "contradicting": contradicting, "subclaims": sub_status, "open_questions": open_q},
            "B": {"first_source": dimensions[2]["data"].get("earliest"), "coverage": [
                    {"id": it["id"], "source": it["source"], "title": it["title"], "url": it["url"], "published": it["published"], "region": it["region"],
                     "lang": it["lang"], "tone": framing.tone(it["title"]), "omitted": framing.omitted_terms(info["main_claim"], it["title"])}
                    for it in relevant_items if it["via"] != "Wikipedia"][:20],
                  "headline_check": headline_check, "meaning": meaning_result, "translation": translation},
            "C": persp,
            "D": social,
            "E": infl,
        },
        "media": media_result,
        "viral_card": _viral_card(info, dimensions, supporting, contradicting, verdict),
        "review": _review_flag(info, verdict),
        "data_sources": status.as_list(),
        "summary": _summary(verdict, info, dimensions, supporting, contradicting),
        "checked_at": _now().isoformat(timespec="seconds"),
    }
    if lang in ("hi", "mixed"):
        result["summary_local"] = await _local_summary(result["summary"])
    result["ai"] = llm.provider_info()
    return result


def _viral_card(info, dims, sup, con, verdict) -> dict:
    """Document §9: careful format for viral claims, used whenever the original source is not established."""
    trace = dims[2]
    earliest = trace["data"].get("earliest")
    traced = trace["rating"].startswith("High")
    found = f"{len(sup)} supporting and {len(con)} contradicting item(s) found."
    missing = [] if traced else ["the original post, document or recording"]
    if not sup and not con:
        missing.append("any direct confirmation or denial")
    return {
        "show": not traced or "Unknown source but viral" in info.get("misinformation_types", []),
        "claim": info["main_claim"],
        "trend_status": dims[10]["rating"] + " — only shown as trending when real trend data exists; ten search results do not mean something is viral.",
        "first_known_appearance": (f"{earliest['source']}, {earliest['published'][:10]}: {earliest['title']}" if earliest else "None found in the searched indexes."),
        "verification": found + (" Still missing: " + ", ".join(missing) + "." if missing else ""),
        "current_conclusion": ("Original source not traced. The truth of the claim has not been established." if not traced and verdict["verdict"] == "Insufficient Evidence"
                               else f"{verdict['verdict']} — see evidence." if traced else f"Source not traced — authenticity unverified. Evidence-based status: {verdict['verdict']}."),
    }


def _review_flag(info, verdict) -> dict:
    reasons = []
    if info.get("risk") == "high":
        reasons.append(f"high-risk topic ({info.get('topic')})")
    if verdict["confidence"] == "Low":
        reasons.append("low confidence")
    if verdict["counts"]["support_chains"] and verdict["counts"]["contradict_chains"] and verdict["verdict"] == "Insufficient Evidence":
        reasons.append("conflicting evidence")
    return {"recommended": bool(reasons), "reasons": reasons,
            "note": "Human expert review recommended before this result is treated as final." if reasons else ""}


def _summary(verdict, info, dims, sup, con) -> str:
    v = verdict["verdict"]
    parts = [f"Verdict: {v}" + (f" (confidence {verdict['confidence'].lower()})" if verdict["confidence"] != "Not applicable" else "") + "."]
    parts.append(f"Claim checked: “{info['main_claim'][:200]}”.")
    c = verdict["counts"]
    parts.append(f"{c['support_chains']} independent source chain(s) support it and {c['contradict_chains']} dispute it.")
    if con:
        parts.append(f"For example, {con[0]['source']} says: “{con[0]['stance']['quote'][:180]}”.")
    elif sup:
        parts.append(f"For example, {sup[0]['source']} says: “{sup[0]['stance']['quote'][:180]}”.")
    parts.append(f"Source traceability: {dims[2]['rating']}.")
    if v == "Insufficient Evidence":
        parts.append("Not finding a source is a reason for caution, not proof that the claim is false.")
    return " ".join(parts)


async def _local_summary(summary: str) -> dict | None:
    if llm.enabled():
        data = await llm.ask_json("plain-language Hindi translation", f"Translate into simple Hindi. Keep numbers and names.\n{summary}\nReturn JSON {{\"hindi\": \"...\"}}", 1500)
        if isinstance(data, dict) and data.get("hindi"):
            return {"lang": "hi", "text": data["hindi"], "method": "AI model"}
    tr = await sources.translate_free(summary, "en", "hi")
    return {"lang": "hi", "text": tr, "method": "MyMemory machine translation"} if tr else None
