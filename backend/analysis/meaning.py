"""Stage 3 of the document – Meaning and Translation Analysis.

Compares an original statement with a later version (translation or rewrite) and points to the
exact phrase where meaning changed: numbers, negation, certainty ("may" vs "did"), named people
and places, and figurative descriptions that became literal claims.
"""
from __future__ import annotations

import difflib
import re

from .. import llm, sources
from ..textutils import (
    assertive_words, content_tokens, detect_language, entities, extract_numbers, hedge_words,
    jaccard, negation_count, normalize,
)

COMPARISON_MARKERS = {"like", "as if", "as though", "looks", "look", "looked", "seems", "seem", "seemed", "appears",
                      "appear", "resembles", "resemble", "similar", "जैसा", "जैसी", "जैसे", "लगता", "लगती", "लगते", "दिखता", "दिखती", "दिखते"}
OPINION_MARKERS = {"i think", "i feel", "in my view", "seems", "apparently", "joked", "joke", "sarcastic", "satire", "figuratively"}
SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1}
ALIASES = {"pm": "prime minister", "cm": "chief minister", "fm": "finance minister", "hm": "home minister",
           "sc": "supreme court", "hc": "high court", "govt": "government", "us": "united states", "uk": "united kingdom",
           "un": "united nations", "who": "world health organization", "rbi": "reserve bank of india"}


def _expand(text: str) -> str:
    """Expand common abbreviations so 'PM' and 'Prime Minister' compare as the same name."""
    return re.sub(r"\b(PM|CM|FM|HM|SC|HC|Govt|govt|US|UK|UN|WHO|RBI)\b", lambda m: ALIASES[m.group(1).lower()].title(), text)


def _same_entity(e: str, other_text_low: str, other_entities_low: set) -> bool:
    el = e.lower()
    if el in other_text_low:
        return True
    return any(el in x or x in el for x in other_entities_low)


async def to_english(text: str) -> dict:
    lang = detect_language(text)
    if lang in ("en", "unknown") or not text.strip():
        return {"lang": lang, "english": text, "method": "none (already English)"}
    if llm.enabled():
        data = await llm.ask_json("faithful literal translation",
                                  f"Translate into English as literally as possible. Keep numbers, names, negation and certainty words exactly.\n"
                                  f"TEXT: {text}\nReturn JSON: {{\"english\": \"...\"}}", 2000)
        if isinstance(data, dict) and data.get("english"):
            return {"lang": lang, "english": data["english"], "method": "AI model"}
    src = "hi" if lang in ("hi", "mixed") else lang
    tr = await sources.translate_free(text, src, "en")
    if tr:
        return {"lang": lang, "english": tr, "method": "MyMemory machine translation"}
    return {"lang": lang, "english": text, "method": "translation unavailable", "warning": "Translation service unavailable; comparison is done on the raw text."}


def _markers(text: str, markers) -> set:
    low = " " + normalize(text).lower() + " "
    return {m for m in markers if re.search(rf"(?<![\wऀ-ॿ]){re.escape(m)}(?![\wऀ-ॿ])", low)}


def _diff_tokens(a: str, b: str) -> list[dict]:
    ta, tb = re.findall(r"\S+", normalize(a)), re.findall(r"\S+", normalize(b))
    sm = difflib.SequenceMatcher(a=[t.lower().strip(".,!?\"'") for t in ta], b=[t.lower().strip(".,!?\"'") for t in tb])
    ops = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        ops.append({"op": tag, "a": " ".join(ta[i1:i2]), "b": " ".join(tb[j1:j2])})
    return ops


def deterministic_changes(orig_en: str, var_en: str, orig_raw: str = "", var_raw: str = "") -> list[dict]:
    orig_en, var_en = _expand(orig_en), _expand(var_en)
    changes = []
    # 1. numbers (compare on both raw and English text so a translation can't hide a changed figure)
    on = {round(n["value"], 4): n["raw"] for n in extract_numbers(orig_raw + " " + orig_en)}
    vn = {round(n["value"], 4): n["raw"] for n in extract_numbers(var_raw + " " + var_en)}
    missing = [on[v] for v in on if v not in vn]
    added = [vn[v] for v in vn if v not in on]
    if missing and added:
        changes.append({"type": "number changed", "severity": "high", "original_phrase": ", ".join(missing), "variant_phrase": ", ".join(added),
                        "explanation": f"The figure {', '.join(missing)} in the original appears as {', '.join(added)} in the later version."})
    elif added:
        changes.append({"type": "number added", "severity": "medium", "original_phrase": "", "variant_phrase": ", ".join(added),
                        "explanation": f"The later version adds a figure ({', '.join(added)}) not present in the original."})
    elif missing:
        changes.append({"type": "number removed", "severity": "low", "original_phrase": ", ".join(missing), "variant_phrase": "",
                        "explanation": f"The figure {', '.join(missing)} from the original is missing in the later version."})
    # 2. negation flip
    neg_o, neg_v = negation_count(orig_en), negation_count(var_en)
    if (neg_o % 2) != (neg_v % 2):
        changes.append({"type": "negation flipped", "severity": "high", "original_phrase": orig_en, "variant_phrase": var_en,
                        "explanation": "One version contains a negation (not/no/never/denied) that the other lacks — the meaning may be reversed."})
    # 3. certainty shift ("may" versus "did")
    ho, hv = set(hedge_words(orig_en)), set(hedge_words(var_en))
    ao, av = set(assertive_words(orig_en)), set(assertive_words(var_en))
    if ho - hv:
        sev = "high" if (av - ao) or not hv else "medium"
        changes.append({"type": "certainty increased", "severity": sev, "original_phrase": ", ".join(sorted(ho - hv)), "variant_phrase": ", ".join(sorted(av - ao)) or "(hedge removed)",
                        "explanation": f"The original was tentative ({', '.join(sorted(ho - hv))}); the later version drops that and states it as settled."})
    elif hv - ho:
        changes.append({"type": "certainty reduced", "severity": "low", "original_phrase": "", "variant_phrase": ", ".join(sorted(hv - ho)),
                        "explanation": "The later version adds hedging words; it is less certain than the original."})
    # 4. comparison / description turned into a literal claim
    co, cv = _markers(orig_en + " " + orig_raw, COMPARISON_MARKERS), _markers(var_en + " " + var_raw, COMPARISON_MARKERS)
    if co and not cv:
        changes.append({"type": "figurative to literal", "severity": "high", "original_phrase": ", ".join(sorted(co)), "variant_phrase": "(comparison removed)",
                        "explanation": "The original is a description or comparison ('" + "', '".join(sorted(co)) + "'); the later version states it as a literal fact."})
    elif cv - co:
        changes.append({"type": "interpretation added", "severity": "medium", "original_phrase": "", "variant_phrase": ", ".join(sorted(cv - co)),
                        "explanation": "The later version adds a comparison or interpretation that is an opinion, not something the original said."})
    oo, ov = _markers(orig_en, OPINION_MARKERS), _markers(var_en, OPINION_MARKERS)
    if oo and not ov:
        changes.append({"type": "opinion presented as fact", "severity": "high", "original_phrase": ", ".join(sorted(oo)), "variant_phrase": "",
                        "explanation": "Opinion/joke markers in the original are gone in the later version."})
    # 5. named people / places
    eo, ev = set(entities(orig_en)), set(entities(var_en))
    lo, lv = {e.lower() for e in eo}, {e.lower() for e in ev}
    ent_added = [e for e in ev if not _same_entity(e, orig_en.lower(), lo)]
    ent_removed = [e for e in eo if not _same_entity(e, var_en.lower(), lv)]
    if ent_added and ent_removed:
        changes.append({"type": "subject/name changed", "severity": "high", "original_phrase": ", ".join(ent_removed[:3]), "variant_phrase": ", ".join(ent_added[:3]),
                        "explanation": "Different people, organisations or places are named in the two versions."})
    elif ent_added:
        changes.append({"type": "name added", "severity": "medium", "original_phrase": "", "variant_phrase": ", ".join(ent_added[:3]),
                        "explanation": "The later version names someone/something not in the original."})
    # 6. new content words (added claims) and dropped content (missing context)
    wo, wv = set(content_tokens(orig_en)), set(content_tokens(var_en))
    new_words = [w for w in content_tokens(var_en) if w not in wo]
    dropped = [w for w in content_tokens(orig_en) if w not in wv]
    if len(dropped) >= 3 and len(dropped) / max(len(wo), 1) > 0.4:
        changes.append({"type": "context removed", "severity": "medium", "original_phrase": " ".join(dict.fromkeys(dropped))[:200], "variant_phrase": "",
                        "explanation": "A large part of the original wording is missing from the later version."})
    if len(new_words) >= 3 and len(new_words) / max(len(wv), 1) > 0.4:
        changes.append({"type": "content added", "severity": "low", "original_phrase": "", "variant_phrase": " ".join(dict.fromkeys(new_words))[:200],
                        "explanation": "The later version contains substantial wording that the original does not."})
    return changes


def stage_label(changes: list[dict], similarity: float) -> str:
    types = {c["type"] for c in changes}
    if "negation flipped" in types:
        return "Reversed meaning"
    if "figurative to literal" in types or "opinion presented as fact" in types:
        return "Distorted literal claim"
    if "number changed" in types or "certainty increased" in types:
        return "Exaggeration / altered detail"
    if "subject/name changed" in types:
        return "Altered subject"
    if "interpretation added" in types:
        return "Interpretation (opinion)"
    if any(c["severity"] in ("medium", "high") for c in changes):
        return "Reworded with changes"
    return "Faithful" if similarity >= 0.5 else "Reworded"


def _verdict(changes: list[dict]) -> str:
    worst = max((SEVERITY_ORDER[c["severity"]] for c in changes), default=0)
    if worst >= 3:
        return "Meaning Altered"
    if worst == 2:
        return "Possible drift — review"
    return "Meaning preserved"


async def compare(original: str, variant: str) -> dict:
    o, v = await to_english(original), await to_english(variant)
    changes = deterministic_changes(o["english"], v["english"], original, variant)
    sim = jaccard(content_tokens(o["english"]), content_tokens(v["english"]))
    ai = None
    if llm.enabled():
        data = await llm.ask_json("meaning comparison between an original statement and a later version", f"""ORIGINAL ({o['lang']}): {original}
ORIGINAL IN ENGLISH: {o['english']}
LATER VERSION ({v['lang']}): {variant}
LATER VERSION IN ENGLISH: {v['english']}

Did the later version change the meaning? Check subject, numbers, negation, certainty ("may" vs "did"), opinion/joke vs literal fact, added claims, removed context.
Return JSON: {{"verdict": "Meaning preserved|Possible drift — review|Meaning Altered",
"changes": [{{"type": "...", "severity": "high|medium|low", "original_phrase": "exact words from ORIGINAL or ORIGINAL IN ENGLISH", "variant_phrase": "exact words from LATER VERSION or its English", "explanation": "..."}}],
"summary": "one or two plain sentences"}}""", 2500)
        if isinstance(data, dict):
            ai_changes = []
            pool_o = (original + " " + o["english"]).lower()
            pool_v = (variant + " " + v["english"]).lower()
            for c in data.get("changes", []):
                if c.get("severity") not in SEVERITY_ORDER:
                    continue
                op, vp = (c.get("original_phrase") or "").lower(), (c.get("variant_phrase") or "").lower()
                # keep only changes whose phrases really occur in the texts (traceability)
                if (op and op not in pool_o) or (vp and vp not in pool_v):
                    continue
                c["source"] = "ai"
                ai_changes.append(c)
            ai = {"summary": data.get("summary", ""), "verdict": data.get("verdict")}
            known = {c["type"] for c in changes}
            changes += [c for c in ai_changes if c.get("type") not in known]
    changes.sort(key=lambda c: SEVERITY_ORDER[c["severity"]], reverse=True)
    return {
        "original": {"text": original, **o},
        "variant": {"text": variant, **v},
        "similarity": round(sim, 3),
        "changes": changes,
        "label": stage_label(changes, sim),
        "verdict": _verdict(changes),
        "diff": _diff_tokens(o["english"], v["english"]),
        "ai": ai,
    }


async def chain(versions: list[str]) -> dict:
    """Follow a statement through several versions and find the exact stage where meaning broke."""
    versions = [normalize(v) for v in versions if v and v.strip()]
    stages = [{"index": 0, "text": versions[0], "label": "Original", "changes": [], "verdict": "—"}] if versions else []
    first_break = None
    for i in range(1, len(versions)):
        step = await compare(versions[i - 1], versions[i])
        vs_orig = await compare(versions[0], versions[i])
        stage = {"index": i, "text": versions[i], "english": step["variant"]["english"], "label": step["label"],
                 "changes": step["changes"], "verdict": step["verdict"], "diff": step["diff"],
                 "verdict_vs_original": vs_orig["verdict"], "similarity_to_original": vs_orig["similarity"]}
        if first_break is None and step["verdict"] == "Meaning Altered":
            first_break = i
        stages.append(stage)
    return {
        "stages": stages,
        "first_altered_stage": first_break,
        "summary": (f"Meaning first changed at stage {first_break} ({stages[first_break]['label']})." if first_break is not None
                    else "No stage altered the meaning according to the checks run."),
    }
