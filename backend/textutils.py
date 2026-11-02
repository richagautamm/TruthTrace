"""Deterministic text helpers used by every module (no AI needed)."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

STOPWORDS = set(
    """a an the and or but if then of to in on at by for with from as is are was were be been being
    this that these those it its he she they them his her their we our you your i me my not no
    has have had do does did will would shall should can could may might must about into over
    after before than so such very just also only said says say new news report reports
    who whom which what when where why how all any each more most other some up out
    के का की है हैं में से को और पर ने कि यह वह था थी थे भी तो एक इस उस लिए नहीं""".split()
)

NEGATIONS = {
    "not", "no", "never", "none", "nobody", "nothing", "neither", "nor", "without",
    "denied", "denies", "deny", "refused", "refutes", "rejects", "isn't", "wasn't",
    "aren't", "weren't", "didn't", "doesn't", "don't", "won't", "cannot", "can't",
    "नहीं", "न", "मत", "कभी",
}

# Words that weaken certainty vs words that assert it ("may" versus "did").
HEDGES = {
    "may", "might", "could", "possibly", "reportedly", "allegedly", "likely", "unlikely",
    "probably", "perhaps", "suggests", "suggested", "claims", "claimed", "rumoured",
    "rumored", "expected", "plans", "proposed", "considering", "unconfirmed", "सकता", "सकती", "संभवतः",
}
ASSERTIVES = {
    "confirmed", "proved", "proven", "definitely", "certainly", "officially", "did",
    "has", "will", "always", "never", "all", "every", "guaranteed", "100%",
}

SENSATIONAL = {
    "shocking", "shock", "slams", "slammed", "destroys", "destroyed", "exposed", "exposes",
    "bombshell", "outrage", "outraged", "furious", "fury", "blasts", "blasted", "explosive",
    "stunning", "stuns", "unbelievable", "viral", "massive", "huge", "big", "breaking",
    "humiliates", "humiliated", "brutal", "brutally", "panic", "chaos", "disaster", "epic",
    "mocks", "mocked", "rips", "lashes", "jolt", "secret", "truth", "finally",
}

DEBUNK_CUES = [
    "fact check", "fact-check", "factcheck", "false", "fake", "hoax", "myth", "debunk",
    "debunked", "misleading", "no evidence", "not true", "untrue", "incorrect", "baseless",
    "fabricated", "doctored", "morphed", "misattributed", "satire", "rumour", "rumor",
    "not visible", "no truth", "busted", "falsely", "wrongly claimed", "viral claim",
    "फर्जी", "झूठा", "भ्रामक", "फेक", "गलत दावा", "अफवाह",
]
SUPPORT_CUES = ["confirmed", "confirms", "announced", "official", "according to", "data shows", "records show", "verified"]

TIME_NEW_CUES = ["today", "just now", "breaking", "right now", "this morning", "tonight", "live", "latest", "आज", "अभी"]


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str) -> list[str]:
    text = normalize(text).lower()
    return re.findall(r"[\wऀ-ॿ%]+(?:[-'][\wऀ-ॿ]+)*", text)


def content_tokens(text: str) -> list[str]:
    return [t for t in tokens(text) if t not in STOPWORDS and len(t) > 1]


def jaccard(a, b) -> float:
    a, b = set(a), set(b)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def coverage(query: str, text: str) -> float:
    """Share of the query's content words that appear in text (0..1)."""
    q = set(content_tokens(query))
    if not q:
        return 0.0
    t = set(content_tokens(text))
    hits = sum(1 for w in q if w in t or any(len(w) > 4 and x.startswith(w[:5]) for x in t))
    return hits / len(q)


def shingles(text: str, k: int = 4) -> set:
    toks = content_tokens(text)
    if len(toks) < k:
        return {" ".join(toks)} if toks else set()
    return {" ".join(toks[i : i + k]) for i in range(len(toks) - k + 1)}


def split_sentences(text: str) -> list[str]:
    text = normalize(text)
    parts = re.split(r"(?<=[.!?।])\s+(?=[\"'“A-Z0-9ऀ-ॿ])", text)
    return [p.strip() for p in parts if len(p.strip()) > 2]


_DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_MULT = {
    "thousand": 1e3, "hazar": 1e3, "हजार": 1e3, "हज़ार": 1e3, "lakh": 1e5, "lakhs": 1e5, "लाख": 1e5,
    "million": 1e6, "crore": 1e7, "crores": 1e7, "करोड़": 1e7, "billion": 1e9, "trillion": 1e12, "अरब": 1e9,
}
_WORD_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "twenty": 20, "fifty": 50, "hundred": 100,
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5, "दस": 10, "सौ": 100,
}


def extract_numbers(text: str) -> list[dict]:
    """Numbers with their raw form and normalized value (handles lakh/crore and Devanagari digits)."""
    t = normalize(text).translate(_DEV_DIGITS)
    out = []
    # (?<!...) skips digits that are part of a name such as "Chandrayaan-3", "COVID-19" or "G20"
    for m in re.finditer(r"(?<![A-Za-zऀ-ॿ\-\d])(\d[\d,]*(?:\.\d+)?)\s*(%|percent|per cent|thousand|lakhs?|million|crores?|billion|trillion|हजार|हज़ार|लाख|करोड़|अरब)?", t, re.I):
        raw = m.group(0).strip()
        try:
            val = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        unit = (m.group(2) or "").lower()
        if unit in ("%", "percent", "per cent"):
            out.append({"raw": raw, "value": val, "unit": "%"})
        else:
            out.append({"raw": raw, "value": val * _MULT.get(unit, 1), "unit": ""})
    for w, v in _WORD_NUM.items():
        if re.search(rf"(?<![\wऀ-ॿ]){re.escape(w)}(?![\wऀ-ॿ])", t, re.I):
            out.append({"raw": w, "value": float(v), "unit": "", "word": True})
    return out


def is_year(n: dict) -> bool:
    return n["unit"] == "" and 1800 <= n["value"] <= 2100 and float(n["value"]).is_integer() and "," not in n["raw"]


def negation_count(text: str) -> int:
    toks = tokens(text)
    return sum(1 for t in toks if t in NEGATIONS or t.endswith("n't"))


def hedge_words(text: str) -> list[str]:
    return [t for t in tokens(text) if t in HEDGES]


def assertive_words(text: str) -> list[str]:
    return [t for t in tokens(text) if t in ASSERTIVES]


def sensational_words(text: str) -> list[str]:
    return sorted({t for t in tokens(text) if t in SENSATIONAL})


def debunk_cues(text: str) -> list[str]:
    low = normalize(text).lower()
    return [c for c in DEBUNK_CUES if c in low]


def quotes_in(text: str) -> list[str]:
    found = re.findall(r"[\"“”']([^\"“”]{12,300})[\"“”']", normalize(text))
    return [q.strip() for q in found if len(q.split()) >= 3]


def entities(text: str) -> list[str]:
    """Capitalised multi-word names (rough, used only when no AI model is configured)."""
    text = normalize(text)
    cands = re.findall(r"\b(?:[A-Z][a-zA-Z.\-]+(?:\s+(?:of|the|de|and)?\s*[A-Z][a-zA-Z.\-]+)*)", text)
    out = []
    for c in cands:
        c = c.strip()
        first = c.split()[0].lower()
        if first in STOPWORDS and len(c.split()) == 1:
            continue
        if len(c) > 2 and c not in out:
            out.append(c)
    return out[:15]


def detect_language(text: str) -> str:
    if not text or not text.strip():
        return "unknown"
    dev = len(re.findall(r"[ऀ-ॿ]", text))
    lat = len(re.findall(r"[A-Za-z]", text))
    if dev and lat and min(dev, lat) / max(dev, lat) > 0.25:
        return "mixed"
    if dev > lat:
        return "hi"
    try:
        from langdetect import DetectorFactory, detect

        DetectorFactory.seed = 0
        return detect(text)
    except Exception:
        return "en" if lat else "unknown"


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# ---- Source classification (transparent, rule-based; a brand name alone proves nothing) ----

FACTCHECK_DOMAINS = {
    "altnews.in", "boomlive.in", "factly.in", "factchecker.in", "vishvasnews.com", "newschecker.in",
    "snopes.com", "politifact.com", "factcheck.org", "fullfact.org", "factcheck.afp.com",
    "leadstories.com", "newsmeter.in", "digiteye.in", "youturn.in", "factcrescendo.com",
    "checkyourfact.com", "healthfeedback.org", "sciencefeedback.co", "logically.ai",
    "newsmobile.in", "thip.media", "dfrac.org", "factchecking.pib.gov.in",
}
OFFICIAL_SUFFIXES = (".gov", ".gov.in", ".nic.in", ".mil", ".gov.uk", ".gouv.fr", ".gc.ca", ".gov.au", ".int", ".europa.eu", ".un.org")
OFFICIAL_DOMAINS = {"pib.gov.in", "who.int", "un.org", "worldbank.org", "imf.org", "rbi.org.in", "isro.gov.in", "nasa.gov", "eci.gov.in", "europa.eu", "mygov.in", "prsindia.org", "sansad.in", "indiacode.nic.in", "egazette.gov.in", "sci.gov.in"}
ACADEMIC_HINTS = ("pmc.ncbi", "nih.gov/pubmed", ".edu", ".ac.in", ".ac.uk", "nature.com", "sciencedirect.com", "ncbi.nlm.nih.gov", "pubmed", "arxiv.org", "thelancet.com", "nejm.org", "springer.com", "wiley.com", "jstor.org", "bmj.com", "science.org", "cochranelibrary.com", "plos.org")
REFERENCE_DOMAINS = {"wikipedia.org", "britannica.com", "wikidata.org"}
SOCIAL_DOMAINS = {"twitter.com", "x.com", "facebook.com", "instagram.com", "youtube.com", "youtu.be", "reddit.com", "t.me", "tiktok.com", "bsky.app", "quora.com", "threads.net", "linkedin.com", "sharechat.com", "kooapp.com", "news.ycombinator.com", "lemmy.world", "mastodon.social"}
INDIAN_OUTLETS = {
    "timesofindia.indiatimes.com", "indiatimes.com", "hindustantimes.com", "ndtv.com", "indianexpress.com",
    "thehindu.com", "news18.com", "indiatoday.in", "livemint.com", "economictimes.indiatimes.com",
    "deccanherald.com", "scroll.in", "thewire.in", "theprint.in", "firstpost.com", "zeenews.india.com",
    "abplive.com", "aajtak.in", "bhaskar.com", "jagran.com", "amarujala.com", "livehindustan.com",
    "navbharattimes.indiatimes.com", "republicworld.com", "moneycontrol.com", "business-standard.com",
    "telegraphindia.com", "tribuneindia.com", "outlookindia.com", "freepressjournal.in", "dnaindia.com",
    "wionews.com", "newindianexpress.com", "thequint.com", "india.com", "oneindia.com", "timesnownews.com",
    "financialexpress.com", "mid-day.com", "deccanchronicle.com", "ddnews.gov.in", "newsonair.gov.in",
    "lokmat.com", "manoramaonline.com", "mathrubhumi.com", "siasat.com", "thenewsminute.com",
    "newslaundry.com", "businesstoday.in", "zeebiz.com", "etvbharat.com", "patrika.com", "ptinews.com", "ians.in",
}
WIRE_SERVICES = {"reuters.com", "apnews.com", "afp.com", "ptinews.com", "ians.in", "ani.in", "aninews.in"}


def _match(domain: str, names) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in names)


def classify_source(domain: str, url: str = "", title: str = "") -> str:
    d = domain.lower()
    low = (url + " " + title).lower()
    if _match(d, FACTCHECK_DOMAINS) or "fact-check" in low or "factcheck" in low or "fact check" in low or "webqoof" in low:
        return "fact-check"
    if any(h in d for h in ACADEMIC_HINTS):
        return "academic"
    if _match(d, OFFICIAL_DOMAINS) or d.endswith(OFFICIAL_SUFFIXES) or "." + d in OFFICIAL_SUFFIXES:
        return "official"
    if _match(d, REFERENCE_DOMAINS):
        return "reference"
    if _match(d, SOCIAL_DOMAINS):
        return "social"
    if _match(d, WIRE_SERVICES):
        return "wire"
    return "news/web"


def region_of(domain: str, lang: str = "") -> str:
    d = domain.lower()
    if d.endswith(".in") or _match(d, INDIAN_OUTLETS):
        return "Indian"
    if not d:
        return "unknown"
    return "International"
