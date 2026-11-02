"""Public Opinion dashboard and dimension 11 (public sentiment and spread), document §11.

Three separate things are shown: sentiment, reasoning, and whether the reasons contain verified
claims. Posts are not people: duplicates are removed, copied text is grouped, each account is
counted once, and the result is labelled "sampled online discussion" — never a population figure.
"""
from __future__ import annotations

import asyncio
import re
from collections import Counter, defaultdict

from .. import llm, sources
from ..textutils import content_tokens, coverage, extract_numbers, jaccard, normalize, shingles, tokens

POS = set("""good great support supports supported agree love excellent welcome welcomed benefit benefits helpful happy fair right
proud success successful positive best better improve improved improvement win wonderful brilliant thanks thank amazing
important necessary needed smart wise safe strong progress progressive yes favour favor boost growth hope hopeful
सही अच्छा समर्थन बढ़िया शानदार धन्यवाद""".split())
NEG = set("""bad terrible oppose opposed against disagree hate awful unfair wrong worst worse fail failed failure harmful
dangerous scam fraud corrupt corruption lie lies liar fake shame shameful disgrace stupid ridiculous useless problem problems
concern concerned worried worry risk risky threat angry anger protest unjust ban cruel nonsense negative loss damage hurt
no never disaster mess chaos waste गलत बुरा विरोध शर्म झूठ खराब""".split())
NEGATORS = {"not", "no", "never", "don't", "doesn't", "isn't", "wasn't", "can't", "won't", "नहीं"}
CLAIM_PATTERNS = re.compile(r"(\d|percent|%|study|studies|data|according to|report|survey|statistics|million|crore|lakh|proved|proof)", re.I)


def sentiment(text: str) -> str:
    toks = tokens(text)
    score = 0
    for i, t in enumerate(toks):
        val = 1 if t in POS else -1 if t in NEG else 0
        if val and any(p in NEGATORS for p in toks[max(0, i - 3):i]):
            val = -val
        score += val
    if text.strip().endswith("?") and abs(score) <= 1:
        return "undecided"
    if score >= 1:
        return "positive"
    if score <= -1:
        return "negative"
    return "neutral"


def clean_sample(posts: list[dict], topic: str) -> dict:
    """Remove duplicates, group copied content, and flag bot-like / high-volume accounts."""
    on_topic = [p for p in posts if p["text"] and coverage(topic, p["title"] + " " + p["text"]) >= 0.3]
    seen, unique, exact_dupes = set(), [], 0
    for p in on_topic:
        key = normalize(p["text"]).lower()
        if key in seen:
            exact_dupes += 1
            continue
        seen.add(key)
        unique.append(p)
    sh = [shingles(p["text"], 3) for p in unique]
    copied_groups, assigned = [], set()
    for i in range(len(unique)):
        if i in assigned:
            continue
        group = [i]
        for j in range(i + 1, len(unique)):
            if j not in assigned and sh[i] and jaccard(sh[i], sh[j]) >= 0.7:
                group.append(j)
        if len(group) > 1:
            assigned.update(group)
            copied_groups.append(group)
    near_dupes = sum(len(g) - 1 for g in copied_groups)
    drop = {j for g in copied_groups for j in g[1:]}
    kept = [p for i, p in enumerate(unique) if i not in drop]
    per_author = Counter(p["author"] for p in kept)
    high_volume = [a for a, n in per_author.items() if n > 3]
    bot_like = set()
    for p in kept:
        handle = p["author"].split("/")[-1]
        if re.search(r"\d{5,}", handle) or len(re.findall(r"#\w+", p["text"])) >= 4 or len(re.findall(r"https?://", p["text"])) >= 3:
            bot_like.add(p["author"])
    coordinated = [[unique[j]["author"] for j in g] for g in copied_groups if len({unique[j]["author"] for j in g}) >= 3]
    return {"posts": kept, "collected": len(posts), "on_topic": len(on_topic), "exact_duplicates_removed": exact_dupes,
            "near_duplicates_removed": near_dupes, "unique_accounts": len(per_author), "high_volume_accounts": high_volume,
            "bot_like_accounts": sorted(bot_like), "coordinated_copy_groups": coordinated}


def _per_person(posts: list[dict], key: str) -> Counter:
    by_author = defaultdict(list)
    for p in posts:
        by_author[p["author"]].append(p[key])
    return Counter(Counter(v).most_common(1)[0][0] for v in by_author.values())


def _keywords(posts: list[dict], topic: str, n: int = 8) -> list[str]:
    topic_t = set(content_tokens(topic))
    c = Counter(w for p in posts for w in set(content_tokens(p["text"])) if w not in topic_t and len(w) > 3 and not w.startswith("http"))
    return [w for w, _ in c.most_common(n)]


async def run(topic: str, status, position: str | None = None) -> dict:
    results = await asyncio.gather(
        sources.reddit_posts(topic, status), sources.bluesky_posts(topic, status), sources.hn_comments(topic, status),
        sources.lemmy_posts(topic, status), sources.gdelt_timeline(topic, status), return_exceptions=True)
    posts = [p for r in results[:4] if isinstance(r, list) for p in r]
    timeline = results[4] if isinstance(results[4], list) else None
    sample = clean_sample(posts, topic)
    kept = sample["posts"]
    for p in kept:
        p["sentiment"] = sentiment(p["text"])
        p["has_checkable_claim"] = bool(CLAIM_PATTERNS.search(p["text"])) and bool(extract_numbers(p["text"]) or "study" in p["text"].lower() or "according" in p["text"].lower())
    method = "heuristic lexicon (tone towards the topic — not stance)"
    categories = ["positive", "negative", "neutral", "undecided"]
    arguments = []
    if llm.enabled() and kept:
        stance_target = position or topic
        batch = kept[:80]
        listing = "\n".join(f"[{i}] {p['text'][:400]}" for i, p in enumerate(batch))
        data = await llm.ask_json("stance classification of sampled online posts", f"""POSITION: "{stance_target}"
POSTS:
{listing}
For each post give its stance towards the POSITION: support|oppose|neutral|undecided (undecided = asks or weighs both sides; neutral = off-position or purely informational).
Then group the reasons people give into arguments. For each argument say whether it relies on a factual claim and whether that claim
is verifiable as stated (you cannot mark it verified from these posts alone — use "unverified" unless the claim is common knowledge, and say why).
Return JSON: {{"stances": [{{"i": 0, "stance": "..."}}], "arguments": [{{"side": "support|oppose|other", "argument": "...", "post_indexes": [0], "factual_claim": "... or null", "claim_status": "unverified|widely documented|contradicted by well-established facts", "note": "..."}}]}}""", 6000)
        if isinstance(data, dict) and data.get("stances"):
            for s in data["stances"]:
                try:
                    i = int(s.get("i"))
                except (TypeError, ValueError):
                    continue
                if 0 <= i < len(batch) and s.get("stance") in ("support", "oppose", "neutral", "undecided"):
                    batch[i]["stance"] = s["stance"]
            kept = [p for p in batch if p.get("stance")]
            for a in data.get("arguments", []):
                idx = [i for i in a.get("post_indexes", []) if isinstance(i, int) and 0 <= i < len(batch)]
                arguments.append({**a, "post_count": len(idx), "examples": [{"text": batch[i]["text"][:240], "url": batch[i]["url"]} for i in idx[:3]]})
            method = f"AI stance classification towards: “{stance_target}”"
            categories = ["support", "oppose", "neutral", "undecided"]
    key = "stance" if categories[0] == "support" else "sentiment"
    per_post = Counter(p.get(key) for p in kept)
    per_person = _per_person(kept, key)
    if not arguments:
        for cat in categories[:2]:
            group = [p for p in kept if p.get(key) == cat]
            if group:
                arguments.append({"side": cat, "argument": "Frequent words: " + ", ".join(_keywords(group, topic)), "post_count": len(group),
                                  "factual_claim": None, "claim_status": f"{sum(p['has_checkable_claim'] for p in group)} posts contain a checkable claim — unverified",
                                  "examples": [{"text": p["text"][:240], "url": p["url"]} for p in group[:3]]})
    dates = sorted(p["published"][:10] for p in kept if p.get("published"))
    per_day = Counter(dates)
    platforms = Counter(p["platform"] for p in kept)
    total_people = sum(per_person.values())
    return {
        "topic": topic,
        "label": "Sampled online discussion — not a representative survey",
        "method": method,
        "categories": categories,
        "per_person": {c: per_person.get(c, 0) for c in categories},
        "per_post": {c: per_post.get(c, 0) for c in categories},
        "per_person_pct": {c: round(100 * per_person.get(c, 0) / total_people, 1) if total_people else 0 for c in categories},
        "sample": {k: v for k, v in sample.items() if k != "posts"},
        "platforms": dict(platforms),
        "collection_period": {"from": dates[0] if dates else None, "to": dates[-1] if dates else None},
        "posts_per_day": [{"date": d, "count": n} for d, n in sorted(per_day.items())],
        "news_volume": timeline,
        "arguments": arguments,
        "posts": [{k: p.get(k) for k in ("platform", "author", "text", "url", "published", "sentiment", "stance", "has_checkable_claim")} for p in kept[:60]],
        "uncertainty": _uncertainty(total_people),
        "limitations": [
            "Only public posts from platforms with open search (Reddit, Bluesky, Hacker News, Lemmy) are sampled; X/Twitter, Facebook, WhatsApp and Instagram are not.",
            "People who post online are not a mirror of the whole public; never read this as 'X% of citizens'.",
            "Popularity is not truth: if most posts believe a false claim, it is still false.",
        ],
    }


def _uncertainty(n: int) -> str:
    if n == 0:
        return "No on-topic posts found — no sentiment can be reported."
    moe = 0.98 / (n ** 0.5) * 100
    return (f"{n} unique accounts. Even if this were a random sample, the margin of error would be about ±{moe:.0f} points; "
            "it is not a random sample, so the true uncertainty is larger.")
