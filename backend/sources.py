"""Connectors to free, public, key-less data sources.

Every item returned here comes from a real HTTP response; nothing is generated.
When a source is down or rate-limited the function returns an empty list and the
failure is recorded in `status` so the dashboard can say "unavailable" honestly.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import json
import urllib.parse
import urllib.request

import feedparser
import httpx
from bs4 import BeautifulSoup

from . import config
from .textutils import classify_source, domain_of, normalize, region_of

log = logging.getLogger("truthtrace.sources")
HEADERS = {"User-Agent": config.USER_AGENT, "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8"}


class SourceStatus:
    """Collects which connectors worked, for the 'data availability' panel."""

    def __init__(self):
        self.items: dict[str, dict] = {}

    def ok(self, name: str, count: int):
        prev = self.items.get(name, {"count": 0})
        self.items[name] = {"status": "ok", "count": prev.get("count", 0) + count}

    def fail(self, name: str, reason: str):
        if self.items.get(name, {}).get("status") != "ok":
            self.items[name] = {"status": "unavailable", "reason": reason[:160], "count": 0}

    def as_list(self):
        return [{"source": k, **v} for k, v in self.items.items()]


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=HEADERS, timeout=config.HTTP_TIMEOUT, follow_redirects=True)


def _iso(value) -> str | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value) if isinstance(value, str) else value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def _clean(text: str) -> str:
    if not text:
        return ""
    text = BeautifulSoup(html.unescape(text), "lxml").get_text(" ")
    return normalize(text)


def make_item(*, title, url, snippet="", published=None, source="", via="", lang="en", domain=None) -> dict:
    domain = domain or domain_of(url)
    return {
        "id": "E" + hashlib.md5((url or title).encode("utf-8")).hexdigest()[:8],
        "title": normalize(title),
        "url": url,
        "domain": domain,
        "source": source or domain,
        "snippet": normalize(snippet)[:700],
        "text": "",
        "published": published,
        "lang": lang,
        "via": via,
        "source_type": classify_source(domain, url, title),
        "region": region_of(domain, lang),
    }


# ----------------------------------------------------------------- news search

async def bing_news(query: str, status: SourceStatus, n: int = 12, market: str = "en-IN") -> list[dict]:
    url = f"https://www.bing.com/news/search?q={quote_plus(query)}&format=rss&mkt={market}"
    try:
        async with _client() as c:
            r = await c.get(url)
        feed = feedparser.parse(r.text)
    except httpx.HTTPError as exc:
        status.fail("Bing News", str(exc))
        return []
    out = []
    for e in feed.entries[:n]:
        link = e.get("link", "")
        real = parse_qs(urlparse(link).query).get("url", [link])[0]
        lang = "hi" if market.startswith("hi") else "en"
        out.append(make_item(title=_clean(e.get("title", "")), url=unquote(real), snippet=_clean(e.get("summary", "")),
                             published=_iso(e.get("published")), source=e.get("news_source", ""), via="Bing News", lang=lang))
    status.ok("Bing News", len(out))
    return out


async def google_news(query: str, status: SourceStatus, lang: str = "en", n: int = 15) -> list[dict]:
    if lang == "hi":
        params = "hl=hi&gl=IN&ceid=IN:hi"
    else:
        params = "hl=en-IN&gl=IN&ceid=IN:en"
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&{params}"
    name = "Google News (Hindi)" if lang == "hi" else "Google News"
    try:
        async with _client() as c:
            r = await c.get(url)
        feed = feedparser.parse(r.text)
    except httpx.HTTPError as exc:
        status.fail(name, str(exc))
        return []
    out = []
    for e in feed.entries[:n]:
        src = e.get("source") or {}
        src_title = src.get("title", "") if isinstance(src, dict) else ""
        src_href = src.get("href", "") if isinstance(src, dict) else ""
        title = _clean(e.get("title", ""))
        if src_title and title.endswith(" - " + src_title):
            title = title[: -len(src_title) - 3]
        out.append(make_item(title=title, url=e.get("link", ""), snippet="", published=_iso(e.get("published")),
                             source=src_title, via=name, lang=lang, domain=domain_of(src_href)))
    status.ok(name, len(out))
    return out


# ----------------------------------------------------------------- web search

async def web_search(query: str, status: SourceStatus, n: int = 10) -> list[dict]:
    try:
        async with _client() as c:
            r = await c.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": "in-en"})
        soup = BeautifulSoup(r.text, "lxml")
    except httpx.HTTPError as exc:
        status.fail("Web search (DuckDuckGo)", str(exc))
        return []
    out = []
    for res in soup.select(".result")[: n + 4]:
        a = res.select_one(".result__a")
        if not a or not a.get("href"):
            continue
        href = a["href"]
        if "duckduckgo.com/l/" in href:
            href = parse_qs(urlparse("https:" + href if href.startswith("//") else href).query).get("uddg", [href])[0]
        if "duckduckgo.com/y.js" in href or "ad_provider" in href:
            continue  # sponsored result
        snip = res.select_one(".result__snippet")
        out.append(make_item(title=a.get_text(" ", strip=True), url=href,
                             snippet=snip.get_text(" ", strip=True) if snip else "", via="Web search"))
        if len(out) >= n:
            break
    if out:
        status.ok("Web search (DuckDuckGo)", len(out))
    else:
        status.fail("Web search (DuckDuckGo)", "no results or blocked")
    return out


# ----------------------------------------------------------------- wikipedia

WIKI_UA = "TruthTrace/1.0 (educational news verification project)"


def _wiki_get_sync(url: str, params: dict | None = None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": WIKI_UA})
    with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


async def _wiki_get(url: str, params: dict | None = None):
    return await asyncio.to_thread(_wiki_get_sync, url, params)


async def wikipedia(query: str, status: SourceStatus, lang: str = "en", n: int = 3) -> list[dict]:
    api = f"https://{lang}.wikipedia.org/w/api.php"
    try:
        d = await _wiki_get(api, {"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": n})
        hits = d.get("query", {}).get("search", [])
        if not hits:
            status.ok("Wikipedia", 0)
            return []
        titles = "|".join(h["title"] for h in hits)
        d2 = await _wiki_get(api, {"action": "query", "prop": "extracts|info", "explaintext": 1, "exintro": 1,
                                   "exlimit": n, "titles": titles, "format": "json", "inprop": "url"})
        pages = d2.get("query", {}).get("pages", {})
    except (OSError, ValueError) as exc:
        status.fail("Wikipedia", str(exc))
        return []
    out = []
    for p in pages.values():
        text = normalize(p.get("extract", ""))
        if not text:
            continue
        item = make_item(title=p.get("title", "") + " — Wikipedia", url=p.get("fullurl", ""), snippet=text[:500],
                         via="Wikipedia", lang=lang, source="Wikipedia")
        item["text"] = text
        out.append(item)
    status.ok("Wikipedia", len(out))
    return out


async def wikipedia_summary(name: str) -> dict | None:
    """Role/description of a named person from Wikipedia (used for the influence panel)."""
    try:
        s = await _wiki_get("https://en.wikipedia.org/w/api.php", {
            "action": "query", "list": "search", "srsearch": name, "format": "json", "srlimit": 1})
        hits = s.get("query", {}).get("search", [])
        if not hits:
            return None
        title = hits[0]["title"]
        d = await _wiki_get("https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_")))
    except (OSError, ValueError):
        return None
    return {
        "title": d.get("title"),
        "description": d.get("description", ""),
        "extract": d.get("extract", ""),
        "url": d.get("content_urls", {}).get("desktop", {}).get("page", ""),
        "type": d.get("type"),
    }


# ----------------------------------------------------------------- article fetch

async def fetch_article(url: str) -> dict:
    """Download a page and pull out the readable text plus publication metadata."""
    if not url or "news.google.com" in url:
        return {"ok": False, "reason": "redirect link (no direct article URL)"}
    try:
        async with _client() as c:
            r = await c.get(url)
        if r.status_code >= 400:
            return {"ok": False, "reason": f"HTTP {r.status_code}"}
        if "html" not in r.headers.get("content-type", "html"):
            return {"ok": False, "reason": "not an HTML page"}
        soup = BeautifulSoup(r.text, "lxml")
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "reason": str(exc)[:120]}

    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n}) or soup.find("meta", attrs={"itemprop": n})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    title = meta("og:title", "twitter:title") or (soup.title.get_text(strip=True) if soup.title else "")
    published = meta("article:published_time", "datePublished", "pubdate", "publishdate", "date", "DC.date.issued", "og:published_time")
    modified = meta("article:modified_time", "dateModified", "og:updated_time")
    author = meta("author", "article:author", "twitter:creator")
    if not published:
        t = soup.find("time", attrs={"datetime": True})
        published = t["datetime"] if t else None
    for s in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        s.decompose()
    # The article body is the element whose direct <p>/<div> children hold the most text.
    best, best_len = None, 0
    for node in soup.find_all(["article", "main", "section", "div"]):
        n = sum(len(ch.get_text(" ", strip=True)) for ch in node.find_all(["p"], recursive=False))
        if n > best_len:
            best, best_len = node, n
    container = best if best is not None and best_len > 300 else (soup.find("article") or soup.body or soup)
    paras = [normalize(p.get_text(" ")) for p in container.find_all(["p"])]
    paras = [p for p in paras if len(p) > 40]
    if sum(len(p) for p in paras) < 300:
        paras = [normalize(t) for t in container.get_text("\n").split("\n") if len(t.strip()) > 60]
    text = "\n".join(paras)[:20000]
    links = [a.get("href", "") for a in container.find_all("a", href=True)]
    ext_domains = {domain_of(l) for l in links if l.startswith("http")}
    primary = sorted(d for d in ext_domains if classify_source(d) in ("official", "academic"))
    low = r.text.lower()
    return {
        "ok": True,
        "final_url": str(r.url),
        "title": normalize(title),
        "text": text,
        "published": _iso(published),
        "modified": _iso(modified),
        "author": author,
        "outbound_domains": len(ext_domains),
        "primary_links": primary[:10],
        "has_correction_note": any(k in low for k in ("correction:", "this article has been corrected", "clarification:", "an earlier version of this")),
    }


# ----------------------------------------------------------------- spread / discussion

async def gdelt_timeline(query: str, status: SourceStatus) -> list[dict] | None:
    """Daily share of global online news mentioning the query (GDELT). Rate limited: 1 req / 5 s."""
    from .textutils import content_tokens

    words = [w for w in content_tokens(query) if len(w) > 3 and w.isascii()][:5]
    if not words:
        status.fail("GDELT news volume", "query has no usable keywords")
        return None
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {"query": " ".join(words), "mode": "timelinevolraw", "format": "json", "timespan": "3months"}
    try:
        async with _client() as c:
            for attempt in range(2):
                r = await c.get(url, params=params)
                if r.status_code == 429 and attempt == 0:
                    await asyncio.sleep(5.5)
                    continue
                break
        if r.status_code != 200 or not r.text.strip().startswith("{"):
            status.fail("GDELT news volume", f"HTTP {r.status_code}: {r.text[:80]}")
            return None
        data = r.json().get("timeline", [])
    except (httpx.HTTPError, ValueError) as exc:
        status.fail("GDELT news volume", str(exc))
        return None
    if not data:
        status.ok("GDELT news volume", 0)
        return []
    series = [{"date": p["date"][:8], "count": p.get("value", 0), "total": p.get("norm", 0)} for p in data[0].get("data", [])]
    status.ok("GDELT news volume", len(series))
    return series


def _post(*, platform, author, text, url, published, title="") -> dict:
    return {"platform": platform, "author": author or "unknown", "text": normalize(text)[:1200],
            "title": normalize(title), "url": url, "published": published}


async def reddit_posts(query: str, status: SourceStatus, n: int = 40) -> list[dict]:
    try:
        async with _client() as c:
            r = await c.get(f"https://www.reddit.com/search.rss?q={quote_plus(query)}&sort=relevance&t=year&limit={n}")
        feed = feedparser.parse(r.text)
    except httpx.HTTPError as exc:
        status.fail("Reddit", str(exc))
        return []
    out = [_post(platform="Reddit", author=e.get("author", ""), title=e.get("title", ""),
                 text=_clean(e.get("title", "") + ". " + (e.get("summary") or "")), url=e.get("link", ""),
                 published=_iso(e.get("updated") or e.get("published"))) for e in feed.entries
           if "/comments/" in e.get("link", "")]
    if r.status_code == 200:
        status.ok("Reddit", len(out))
    else:
        status.fail("Reddit", f"HTTP {r.status_code}")
    return out


async def bluesky_posts(query: str, status: SourceStatus, n: int = 50) -> list[dict]:
    try:
        async with _client() as c:
            r = await c.get("https://api.bsky.app/xrpc/app.bsky.feed.searchPosts", params={"q": query, "limit": n})
        if r.status_code != 200:
            status.fail("Bluesky", f"HTTP {r.status_code}")
            return []
        posts = r.json().get("posts", [])
    except (httpx.HTTPError, ValueError) as exc:
        status.fail("Bluesky", str(exc))
        return []
    out = []
    for p in posts:
        handle = p.get("author", {}).get("handle", "")
        rkey = p.get("uri", "").rsplit("/", 1)[-1]
        out.append(_post(platform="Bluesky", author=handle, text=p.get("record", {}).get("text", ""),
                         url=f"https://bsky.app/profile/{handle}/post/{rkey}", published=_iso(p.get("record", {}).get("createdAt"))))
    status.ok("Bluesky", len(out))
    return out


async def hn_comments(query: str, status: SourceStatus, n: int = 40) -> list[dict]:
    try:
        async with _client() as c:
            r = await c.get("https://hn.algolia.com/api/v1/search", params={"query": query, "tags": "comment", "hitsPerPage": n})
        hits = r.json().get("hits", [])
    except (httpx.HTTPError, ValueError) as exc:
        status.fail("Hacker News", str(exc))
        return []
    out = [_post(platform="Hacker News", author=h.get("author"), text=_clean(h.get("comment_text") or ""),
                 url=f"https://news.ycombinator.com/item?id={h.get('objectID')}", published=_iso(h.get("created_at"))) for h in hits]
    status.ok("Hacker News", len(out))
    return out


async def lemmy_posts(query: str, status: SourceStatus, n: int = 30) -> list[dict]:
    try:
        async with _client() as c:
            r = await c.get("https://lemmy.world/api/v3/search", params={"q": query, "type_": "Comments", "limit": n, "sort": "TopAll"})
        data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        status.fail("Lemmy", str(exc))
        return []
    out = []
    for cv in data.get("comments", []):
        cm = cv.get("comment", {})
        out.append(_post(platform="Lemmy", author=(cv.get("creator") or {}).get("actor_id", ""), text=cm.get("content", ""),
                         url=cm.get("ap_id", ""), published=_iso(cm.get("published"))))
    status.ok("Lemmy", len(out))
    return out


# ----------------------------------------------------------------- translation (fallback when no AI model)

async def translate_free(text: str, source: str, target: str) -> str | None:
    """MyMemory free translation API. Returns None when unavailable."""
    if not text.strip():
        return text
    chunks, out = re.findall(r".{1,450}(?:\s|$)", text, re.S) or [text], []
    try:
        async with _client() as c:
            for ch in chunks[:6]:
                r = await c.get("https://api.mymemory.translated.net/get", params={"q": ch, "langpair": f"{source}|{target}"})
                d = r.json()
                if d.get("responseStatus") != 200:
                    return None
                out.append(html.unescape(d["responseData"]["translatedText"]))
    except (httpx.HTTPError, ValueError, KeyError):
        return None
    return " ".join(out)
