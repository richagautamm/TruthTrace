"""Stage 1 – claim extraction: break the input into small, separately checkable claims
and decide which tests fit this kind of item (document §7, "module rule")."""
from __future__ import annotations

import re

from .. import llm
from ..textutils import (
    content_tokens, entities, extract_numbers, hedge_words, is_year, normalize,
    quotes_in, split_sentences, TIME_NEW_CUES,
)

MISINFO_TYPES = [
    "Fake factual claim",
    "Real fact with misleading headline",
    "Hindi-English meaning distortion",
    "Unknown source but viral",
    "AI-generated image",
    "Deepfake video",
    "Real old video shown as new",
    "Fake voice or made-up quote",
    "Political narrative or POV",
    "Fake statistics or graphs",
    "Fake government order or document",
    "Fake scientific or medical claim",
    "Fake screenshot or social media post",
    "Made-up quote from a leader",
    "Breaking news with conflicting reports",
]

# Recommended tests per type, straight from the project document (§7).
TESTS_FOR_TYPE = {
    "Fake factual claim": ["Break the claim into parts", "Find the primary source", "Check independent confirmation", "Search for contradictions"],
    "Real fact with misleading headline": ["Compare headline and article", "Find omitted context", "Check if the claim is stronger than the evidence"],
    "Hindi-English meaning distortion": ["Translate both ways", "Compare with the original quote", "Check names, numbers, negation and certainty"],
    "Unknown source but viral": ["Find the earliest post", "Map reposts", "Reverse search", "Note gaps in origin", "Use trend data"],
    "AI-generated image": ["Check content credentials", "Reverse image search", "Image forensic checks", "Find the original source"],
    "Deepfake video": ["Frame-level analysis", "Audio-video match", "Source history", "Original footage", "Independent check of the event"],
    "Real old video shown as new": ["Reverse video and frame search", "Upload history", "Landmarks, weather and lighting", "Date and location evidence"],
    "Fake voice or made-up quote": ["Original recording", "Transcript match", "Audio origin", "Independent recording"],
    "Political narrative or POV": ["Separate factual claims from opinions", "Compare arguments", "Check each factual claim on its own"],
    "Fake statistics or graphs": ["Original dataset", "Base number used", "Time period and units", "How data was collected", "Arithmetic and chart axis check"],
    "Fake government order or document": ["Official website", "Document number and date", "Digital signature if available", "Confirmation from issuing authority"],
    "Fake scientific or medical claim": ["Original study", "Methods and sample size", "Systematic reviews", "Expert consensus"],
    "Fake screenshot or social media post": ["Original account and post", "Archived copy", "URL and account identity", "Repost timeline", "Image editing checks"],
    "Made-up quote from a leader": ["Full speech, video or transcript", "Exact wording", "Date", "Surrounding context"],
    "Breaking news with conflicting reports": ["Time-stamped evidence", "Primary sources", "Separate confirmed from unconfirmed facts", "Re-check regularly"],
}

_TOPIC_KEYWORDS = {
    "political/policy": ["government", "minister", "policy", "parliament", "bill", "law", "party", "bjp", "congress", "election", "president", "prime minister", "visa", "tariff", "budget", "scheme", "modi", "opposition", "court", "सरकार", "मंत्री", "चुनाव", "नीति"],
    "health/medical": ["vaccine", "covid", "virus", "cancer", "cure", "disease", "health", "medicine", "doctor", "hospital", "who", "infection", "कोरोना", "टीका", "इलाज"],
    "science/space": ["isro", "nasa", "moon", "space", "satellite", "planet", "scientist", "study", "research", "chandrayaan"],
    "disaster/public safety": ["flood", "earthquake", "fire", "crash", "accident", "killed", "dead", "attack", "blast", "cyclone", "rescue", "pilot", "plane", "train"],
    "communal/social": ["hindu", "muslim", "temple", "mosque", "riot", "caste", "religion", "communal", "church"],
    "business/economy": ["gdp", "inflation", "rupee", "market", "stock", "company", "rbi", "bank", "economy", "price"],
}
_HIGH_RISK = ["health/medical", "communal/social", "disaster/public safety"]
_OPINION_CUES = ["should", "must", "best", "worst", "beautiful", "ugly", "great", "terrible", "i think", "i believe", "in my opinion", "deserve", "shameful", "comes first", "चाहिए", "सबसे अच्छा"]


def _topic(text: str) -> str:
    low = text.lower()
    scores = {t: sum(1 for k in kws if re.search(rf"(?<!\w){re.escape(k)}(?!\w)", low)) for t, kws in _TOPIC_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] else "general"


def _types_heuristic(text: str, headline: str, lang: str, media_kind: str | None, has_original: bool) -> list[str]:
    low = (headline + " " + text).lower()
    types = []
    if media_kind == "image":
        types.append("AI-generated image")
    if media_kind == "video":
        types += ["Deepfake video", "Real old video shown as new"]
    if media_kind == "audio":
        types.append("Fake voice or made-up quote")
    if lang in ("hi", "mixed") or has_original:
        types.append("Hindi-English meaning distortion")
    if headline and text and headline.strip() != text.strip():
        types.append("Real fact with misleading headline")
    if quotes_in(text) or re.search(r"\b(said|says|stated|told|remarked|ने कहा)\b", low):
        types.append("Made-up quote from a leader")
    if re.search(r"\b(order|notification|circular|gazette|notice|ordinance)\b", low):
        types.append("Fake government order or document")
    if any(k in low for k in ("study", "cure", "vaccine", "doctor", "medical", "cancer", "virus", "research")):
        types.append("Fake scientific or medical claim")
    if any(not is_year(n) for n in extract_numbers(text)) and any(k in low for k in ("%", "percent", "data", "survey", "statistics", "rate", "crore", "lakh", "million")):
        types.append("Fake statistics or graphs")
    if any(k in low for k in ("viral", "forwarded", "whatsapp", "going around", "social media")):
        types.append("Unknown source but viral")
    if any(k in low for k in ("screenshot", "tweet", "post on x", "instagram post", "facebook post")):
        types.append("Fake screenshot or social media post")
    if any(k in low for k in ("breaking", "just in", "developing")):
        types.append("Breaking news with conflicting reports")
    if _topic(low) == "political/policy":
        types.append("Political narrative or POV")
    types.append("Fake factual claim")
    seen = []
    for t in types:
        if t not in seen:
            seen.append(t)
    return seen


def _is_opinion(sentence: str) -> bool:
    # case-sensitive on purpose: "Great Wall" is a name, "great" is an opinion
    return any(re.search(rf"(?<!\w){re.escape(c)}(?!\w)", sentence) for c in _OPINION_CUES)


def _query_from(text: str, limit: int = 9) -> str:
    words, seen = [], set()
    for w in content_tokens(text):
        if w not in seen:
            seen.add(w)
            words.append(w)
    return " ".join(words[:limit])


def heuristic_extract(text: str, headline: str, lang: str, media_kind, has_original) -> dict:
    body = normalize(text)
    sentences = split_sentences(body) or [body]
    subclaims = []
    for s in sentences[:6]:
        kind = "opinion" if _is_opinion(s) else ("prediction" if re.search(r"\b(will|going to|expected to)\b", s.lower()) else "fact")
        subclaims.append({"text": s, "type": kind, "checkable": kind == "fact"})
    main = headline.strip() or sentences[0]
    nums = [n["raw"] for n in extract_numbers(body)]
    dates = re.findall(r"\b(?:\d{1,2}\s+)?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}?,?\s*\d{4}\b|\b(?:19|20)\d{2}\b", body)
    topic = _topic(headline + " " + body)
    return {
        "main_claim": main,
        "subclaims": subclaims,
        "entities": {"names": entities(headline + ". " + body), "numbers": nums[:12], "dates": dates[:8]},
        "quotes": [{"speaker": None, "quote": q} for q in quotes_in(body)][:5],
        "misinformation_types": _types_heuristic(body, headline, lang, media_kind, has_original),
        "topic": topic,
        "risk": "high" if topic in _HIGH_RISK else ("medium" if topic == "political/policy" else "low"),
        "search_query": _query_from(main if len(content_tokens(main)) >= 3 else body),
        "claims_recency": any(c in body.lower() for c in TIME_NEW_CUES),
        "hedged": bool(hedge_words(body)),
        "method": "heuristic",
    }


async def extract(text: str, headline: str = "", lang: str = "en", media_kind: str | None = None,
                  has_original: bool = False, english_text: str | None = None) -> dict:
    base = heuristic_extract(english_text or text, headline, lang, media_kind, has_original)
    if not llm.enabled():
        return base
    prompt = f"""Analyse this news item.
HEADLINE: {headline or '(none)'}
TEXT ({lang}): {text[:6000]}
{'ENGLISH TRANSLATION: ' + english_text[:6000] if english_text and english_text != text else ''}

Return JSON:
{{"main_claim": "the central checkable claim in one English sentence",
 "subclaims": [{{"text": "small separately checkable statement (English)", "type": "fact|opinion|prediction|value", "checkable": true}}],
 "entities": {{"names": ["people, organisations, places"], "numbers": ["numbers as written"], "dates": ["dates as written"]}},
 "quotes": [{{"speaker": "name or null", "quote": "exact quoted words from the text"}}],
 "misinformation_types": [up to 4 from this list, most relevant first: {MISINFO_TYPES}],
 "topic": "political/policy|health/medical|science/space|disaster/public safety|communal/social|business/economy|general",
 "risk": "high|medium|low (high = health advice, elections, public safety, communal violence)",
 "search_query": "6-10 keyword English web search query to find evidence about the main claim",
 "claims_recency": true if the item presents the event as new/current}}
Split compound statements. Values and opinions are not facts. Only extract what is in the text."""
    data = await llm.ask_json("claim extraction", prompt, 3000)
    if not isinstance(data, dict) or not data.get("subclaims"):
        return base
    out = dict(base)
    for key in ("main_claim", "subclaims", "entities", "quotes", "topic", "risk", "search_query", "claims_recency"):
        if data.get(key) not in (None, "", []):
            out[key] = data[key]
    types = [t for t in data.get("misinformation_types", []) if t in MISINFO_TYPES]
    # Media types depend on the uploaded file, not on the model's reading of the text.
    for t in base["misinformation_types"]:
        if t in ("AI-generated image", "Deepfake video", "Real old video shown as new", "Fake voice or made-up quote", "Hindi-English meaning distortion") and t not in types:
            types.insert(0, t)
    if types:
        out["misinformation_types"] = types
    out["subclaims"] = [s for s in out["subclaims"] if isinstance(s, dict) and s.get("text")][:8]
    out["method"] = "ai"
    return out


def select_tests(types: list[str], media_kind: str | None) -> dict:
    """Module rule: only run tests that fit the item. Text-only claims never get a video check."""
    selected = []
    for t in types:
        selected.append({"type": t, "tests": TESTS_FOR_TYPE.get(t, [])})
    skipped = []
    if media_kind is None:
        skipped.append("Image/video/audio authenticity checks — not applicable: no media file was submitted (module rule).")
    elif media_kind == "image":
        skipped.append("Video frame and audio checks — not applicable to a still image.")
    return {"selected": selected, "skipped": skipped}
