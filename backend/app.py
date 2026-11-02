"""TruthTrace web server. Run with:  python run.py   (or: uvicorn backend.app:app)"""
from __future__ import annotations

import json
import logging

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, llm, sources
from .analysis import compare, evolution, influencers, media as media_mod, meaning, opinion, perspectives, verify

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
app = FastAPI(title="TruthTrace", version="1.0", description="News verification and narrative tracking system")
db.init()
FRONTEND = config.ROOT / "frontend"
MAX_UPLOAD = 60 * 1024 * 1024
_eval_cache: dict = {}


class TopicIn(BaseModel):
    query: str = Field(min_length=3, max_length=400)
    position: str | None = Field(default=None, max_length=300)
    original: str | None = Field(default=None, max_length=2000)


class MeaningIn(BaseModel):
    original: str | None = Field(default=None, max_length=5000)
    variant: str | None = Field(default=None, max_length=5000)
    versions: list[str] | None = None


class FeedbackIn(BaseModel):
    analysis_id: str
    kind: str = Field(pattern="^(error report|new evidence|publisher appeal|other)$")
    message: str = Field(min_length=5, max_length=2000)
    evidence_url: str | None = Field(default=None, max_length=500)


class FeedbackStatusIn(BaseModel):
    status: str = Field(pattern="^(open|upheld|rejected)$")


def _store(kind: str, title: str, result: dict, **kw) -> dict:
    result["analysis_id"] = db.save(kind, title, result, **kw)
    return result


@app.get("/api/status")
async def status():
    return {"ai": llm.provider_info(), "stats": db.stats(), "verdicts": verify.VERDICTS}


@app.post("/api/verify")
async def api_verify(text: str = Form(""), headline: str = Form(""), url: str = Form(""), original_text: str = Form(""),
                     file: UploadFile | None = File(None)):
    if not (text.strip() or headline.strip() or url.strip() or file):
        raise HTTPException(400, "Enter a claim, headline, link or upload a media file.")
    media = None
    if file and file.filename:
        data = await file.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "File too large (max 60 MB).")
        if not media_mod.media_kind(file.filename, file.content_type):
            raise HTTPException(400, "Unsupported file type. Upload an image, video or audio file.")
        media = {"data": data, "filename": file.filename, "content_type": file.content_type or ""}
    result = await verify.run(text=text[:8000], headline=headline[:500], url=url.strip(), original_text=original_text[:5000], media=media)
    memory = db.similar_claims(result["claim"]["main_claim"], (result.get("media") or {}).get("sha256"))
    result["claim_memory"] = memory
    if memory:
        prev = memory[0]
        result["update_tracking"] = {"previous_verdict": prev["verdict"], "previous_check": prev["created"], "previous_id": prev["id"],
                                     "changed": prev["verdict"] != result["verdict"]["verdict"]}
    _store("verify", result["claim"]["main_claim"], result, verdict=result["verdict"]["verdict"], confidence=result["verdict"]["confidence"],
           lang=result["input"]["language"], media_hash=(result.get("media") or {}).get("sha256"), claim_text=result["claim"]["main_claim"])
    db.record_sources(result["analysis_id"], result["verdict"]["verdict"], result["evidence"],
                      {k: v["stance"] for k, v in result["stances"].items()})
    return JSONResponse(json.loads(json.dumps(result, default=str)))


@app.post("/api/compare")
async def api_compare(body: TopicIn):
    st = sources.SourceStatus()
    result = await compare.run(body.query, st)
    result["data_sources"] = st.as_list()
    return _store("compare", body.query, result)


@app.post("/api/evolution")
async def api_evolution(body: TopicIn):
    st = sources.SourceStatus()
    result = await evolution.run(body.query, st, original=body.original)
    result["data_sources"] = st.as_list()
    return _store("evolution", body.query, result)


@app.post("/api/meaning")
async def api_meaning(body: MeaningIn):
    if body.versions and len([v for v in body.versions if v.strip()]) >= 2:
        result = await meaning.chain(body.versions[:8])
        return _store("meaning-chain", body.versions[0], result)
    if body.original and body.variant:
        result = await meaning.compare(body.original, body.variant)
        return _store("meaning", body.original, result, verdict=result["verdict"])
    raise HTTPException(400, "Provide an original and a later version, or at least two versions.")


@app.post("/api/perspectives")
async def api_perspectives(body: TopicIn):
    st = sources.SourceStatus()
    result = await perspectives.run(body.query, st)
    result["data_sources"] = st.as_list()
    return _store("perspectives", body.query, result)


@app.post("/api/opinion")
async def api_opinion(body: TopicIn):
    st = sources.SourceStatus()
    result = await opinion.run(body.query, st, position=body.position)
    result["data_sources"] = st.as_list()
    return _store("opinion", body.query, result)


@app.post("/api/influencers")
async def api_influencers(body: TopicIn):
    st = sources.SourceStatus()
    result = await influencers.run(body.query, st)
    result["data_sources"] = st.as_list()
    return _store("influencers", body.query, result)


@app.post("/api/media")
async def api_media(file: UploadFile = File(...), claims_recent: bool = Form(False)):
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File too large (max 60 MB).")
    result = media_mod.analyse(data, file.filename or "upload", file.content_type or "", claims_recent)
    return _store("media", file.filename or "upload", result, verdict=result["label"], media_hash=result.get("sha256"))


@app.get("/api/history")
async def api_history(kind: str | None = None):
    return db.history(kind=kind)


@app.get("/api/analysis/{aid}")
async def api_analysis(aid: str):
    row = db.get(aid)
    if not row:
        raise HTTPException(404, "Not found")
    row["feedback"] = db.feedback(aid)
    return row


@app.post("/api/feedback")
async def api_feedback(body: FeedbackIn):
    if not db.get(body.analysis_id):
        raise HTTPException(404, "Unknown analysis")
    return {"id": db.add_feedback(body.analysis_id, body.kind, body.message, body.evidence_url)}


@app.get("/api/feedback")
async def api_feedback_list():
    return db.feedback()


@app.post("/api/feedback/{fid}/status")
async def api_feedback_status(fid: int, body: FeedbackStatusIn):
    db.set_feedback_status(fid, body.status)
    return {"ok": True}


@app.get("/api/sources")
async def api_sources(domain: str | None = None):
    return db.source_history(domain)


@app.get("/api/evaluation")
async def api_evaluation():
    path = config.DATA_DIR / "eval_results.json"
    out = {"claims": None, "meaning": None}
    if path.exists():
        out["claims"] = json.loads(path.read_text(encoding="utf-8"))
    from eval import run_eval  # local package, imported lazily

    if "meaning" not in _eval_cache:
        _eval_cache["meaning"] = await run_eval.meaning_eval()
    out["meaning"] = _eval_cache["meaning"]
    out["feedback"] = db.stats()
    return out


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")
