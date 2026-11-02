"""SQLite storage: analysis history, claim memory, source history over time, reader feedback."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from . import config
from .textutils import content_tokens, jaccard

SCHEMA = """
CREATE TABLE IF NOT EXISTS analyses (
    id TEXT PRIMARY KEY, created TEXT, kind TEXT, title TEXT, claim_tokens TEXT,
    verdict TEXT, confidence TEXT, lang TEXT, media_hash TEXT, result TEXT
);
CREATE TABLE IF NOT EXISTS source_events (
    domain TEXT, analysis_id TEXT, created TEXT, stance TEXT, verdict TEXT, source_type TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT, analysis_id TEXT, created TEXT, kind TEXT,
    message TEXT, evidence_url TEXT, status TEXT DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_src ON source_events(domain);
"""


def _conn():
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)  # idempotent; keeps the app working if the database file is deleted
    return c


def init():
    _conn().close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save(kind: str, title: str, result: dict, verdict: str | None = None, confidence: str | None = None,
         lang: str | None = None, media_hash: str | None = None, claim_text: str | None = None) -> str:
    aid = uuid.uuid4().hex[:12]
    toks = " ".join(sorted(set(content_tokens(claim_text or title))))
    with _conn() as c:
        c.execute("INSERT INTO analyses VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (aid, now(), kind, title[:300], toks, verdict, confidence, lang, media_hash, json.dumps(result, default=str)))
    return aid


def record_sources(aid: str, verdict: str, items: list[dict], stances: dict):
    with _conn() as c:
        c.executemany("INSERT INTO source_events VALUES (?,?,?,?,?,?)",
                      [(it["domain"], aid, now(), stances.get(it["id"], "unknown"), verdict, it["source_type"]) for it in items if it["domain"]])


def get(aid: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM analyses WHERE id=?", (aid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["result"] = json.loads(d["result"])
    return d


def history(limit: int = 100, kind: str | None = None) -> list[dict]:
    q = "SELECT id, created, kind, title, verdict, confidence, lang FROM analyses"
    args = []
    if kind:
        q += " WHERE kind=?"
        args.append(kind)
    q += " ORDER BY created DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def similar_claims(text: str, media_hash: str | None = None, threshold: float = 0.6, exclude: str | None = None) -> list[dict]:
    """Claim memory: earlier checks of the same claim (same words, or the same media file)."""
    toks = set(content_tokens(text))
    out = []
    with _conn() as c:
        rows = c.execute("SELECT id, created, title, claim_tokens, verdict, confidence, media_hash FROM analyses WHERE kind='verify' ORDER BY created DESC LIMIT 500").fetchall()
    for r in rows:
        if r["id"] == exclude:
            continue
        sim = jaccard(toks, set((r["claim_tokens"] or "").split()))
        same_media = bool(media_hash and r["media_hash"] == media_hash)
        if sim >= threshold or same_media:
            out.append({"id": r["id"], "created": r["created"], "title": r["title"], "verdict": r["verdict"],
                        "confidence": r["confidence"], "similarity": round(sim, 2), "same_media": same_media})
    return out[:5]


def source_history(domain: str | None = None) -> list[dict]:
    with _conn() as c:
        if domain:
            rows = c.execute("SELECT * FROM source_events WHERE domain=? ORDER BY created", (domain,)).fetchall()
            return [dict(r) for r in rows]
        rows = c.execute("""SELECT domain, source_type, COUNT(*) n,
                  SUM(stance='supports' AND verdict='Contradicted') backed_false,
                  SUM(stance='contradicts' AND verdict='Supported') disputed_true,
                  SUM(stance='supports' AND verdict='Supported') + SUM(stance='contradicts' AND verdict='Contradicted') aligned,
                  MIN(created) first_seen, MAX(created) last_seen
                  FROM source_events GROUP BY domain ORDER BY n DESC LIMIT 200""").fetchall()
    return [dict(r) for r in rows]


def domain_track_record(domain: str) -> dict | None:
    with _conn() as c:
        r = c.execute("""SELECT COUNT(*) n, SUM(stance='supports' AND verdict='Contradicted') backed_false,
                         SUM(stance='contradicts' AND verdict='Supported') disputed_true,
                         SUM(stance='supports' AND verdict='Supported') + SUM(stance='contradicts' AND verdict='Contradicted') aligned
                         FROM source_events WHERE domain=?""", (domain,)).fetchone()
    return dict(r) if r and r["n"] else None


def add_feedback(aid: str, kind: str, message: str, evidence_url: str | None) -> int:
    with _conn() as c:
        cur = c.execute("INSERT INTO feedback (analysis_id, created, kind, message, evidence_url) VALUES (?,?,?,?,?)",
                        (aid, now(), kind, message[:2000], (evidence_url or "")[:500]))
        return cur.lastrowid


def feedback(aid: str | None = None) -> list[dict]:
    with _conn() as c:
        if aid:
            rows = c.execute("SELECT * FROM feedback WHERE analysis_id=? ORDER BY created DESC", (aid,)).fetchall()
        else:
            rows = c.execute("SELECT f.*, a.title, a.verdict FROM feedback f LEFT JOIN analyses a ON a.id=f.analysis_id ORDER BY f.created DESC LIMIT 200").fetchall()
    return [dict(r) for r in rows]


def set_feedback_status(fid: int, status: str):
    with _conn() as c:
        c.execute("UPDATE feedback SET status=? WHERE id=?", (status, fid))


def stats() -> dict:
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) FROM analyses").fetchone()[0]
        by = dict(c.execute("SELECT verdict, COUNT(*) FROM analyses WHERE kind='verify' GROUP BY verdict").fetchall())
        fb = c.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        upheld = c.execute("SELECT COUNT(*) FROM feedback WHERE status='upheld'").fetchone()[0]
    return {"analyses": n, "verdicts": by, "feedback": fb, "feedback_upheld": upheld}
