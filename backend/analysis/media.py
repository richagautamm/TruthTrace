"""Dimensions 9 and 10 – image/video and audio authenticity (document §8, five checks).

Detector-style signals here are *indicators*, never a verdict: the result is
"Media authenticity inconclusive" unless there is declared provenance (content credentials /
generator metadata) or dated metadata that conflicts with the claimed context.
"""
from __future__ import annotations

import base64
import hashlib
import io
import re
import struct
import tempfile
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import ExifTags, Image, ImageChops

GENERATOR_MARKERS = {
    b"trainedAlgorithmicMedia": "IPTC digital source type = trained algorithmic media (AI-generated)",
    b"compositeWithTrainedAlgorithmicMedia": "IPTC digital source type = composite with AI-generated media",
    b"Midjourney": "Midjourney", b"DALL-E": "DALL-E", b"DALL\xc2\xb7E": "DALL-E", b"Stable Diffusion": "Stable Diffusion",
    b"Adobe Firefly": "Adobe Firefly", b"OpenAI": "OpenAI", b"Imagen": "Google Imagen", b"SynthID": "Google SynthID",
    b"ComfyUI": "ComfyUI", b"NovelAI": "NovelAI", b"Gemini": "Google Gemini", b"Leonardo.Ai": "Leonardo.Ai",
}
C2PA_MARKERS = (b"c2pa", b"jumb", b"cai:")
EDITOR_MARKERS = (b"Photoshop", b"GIMP", b"Snapseed", b"Lightroom", b"Picsart", b"Canva", b"FaceApp", b"Remini", b"CapCut", b"InShot")

REVERSE_SEARCH = [
    {"name": "Google Lens", "url": "https://lens.google.com/"},
    {"name": "TinEye", "url": "https://tineye.com/"},
    {"name": "Bing Visual Search", "url": "https://www.bing.com/visualsearch"},
    {"name": "Yandex Images", "url": "https://yandex.com/images/"},
    {"name": "InVID-WeVerify (video keyframes)", "url": "https://www.invid-project.eu/tools-and-services/invid-verification-plugin/"},
]


def _b64_png(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _b64_jpg(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def average_hash(img: Image.Image) -> str:
    small = img.convert("L").resize((8, 8))
    px = list(small.tobytes())
    avg = sum(px) / len(px)
    return "%016x" % int("".join("1" if p > avg else "0" for p in px), 2)


def _markers(data: bytes) -> dict:
    head = data[:2_000_000] + data[-200_000:]
    gen = sorted({v for k, v in GENERATOR_MARKERS.items() if k in head})
    c2pa = any(m in head for m in C2PA_MARKERS)
    editors = sorted({m.decode() for m in EDITOR_MARKERS if m in head})
    return {"generator": gen, "c2pa": c2pa, "editors": editors}


def _exif(img: Image.Image) -> dict:
    out = {}
    try:
        raw = img.getexif()
        for k, v in raw.items():
            name = ExifTags.TAGS.get(k, str(k))
            if name in ("Make", "Model", "Software", "DateTime", "Artist", "Copyright", "ImageDescription"):
                out[name] = str(v)[:120]
        ifd = raw.get_ifd(0x8769)
        for k, v in ifd.items():
            name = ExifTags.TAGS.get(k, str(k))
            if name in ("DateTimeOriginal", "DateTimeDigitized", "LensModel", "OffsetTimeOriginal"):
                out[name] = str(v)[:60]
        if raw.get_ifd(0x8825):
            out["GPS"] = "present (location embedded)"
    except Exception:
        pass
    for k in ("parameters", "prompt", "Description", "Software", "Comment"):
        if k in getattr(img, "info", {}):
            out["PNG:" + k] = str(img.info[k])[:200]
    return out


def error_level_analysis(img: Image.Image, quality: int = 90) -> dict:
    """Re-save as JPEG and amplify the difference. Uneven bright regions *may* indicate edits; this is an indicator only."""
    rgb = img.convert("RGB")
    rgb.thumbnail((1200, 1200))
    buf = io.BytesIO()
    rgb.save(buf, "JPEG", quality=quality)
    resaved = Image.open(io.BytesIO(buf.getvalue()))
    diff = ImageChops.difference(rgb, resaved)
    extrema = diff.getextrema()
    max_diff = max(e[1] for e in extrema) or 1
    scale = 255.0 / max_diff
    ela = diff.point(lambda p: min(255, int(p * scale)))
    gray = diff.convert("L")
    hist = gray.histogram()
    total = sum(hist)
    mean = sum(i * h for i, h in enumerate(hist)) / total
    high = sum(hist[20:]) / total
    return {"image": _b64_png(ela), "mean_error": round(mean, 2), "max_error": max_diff, "high_error_share": round(high, 4)}


def _parse_exif_date(s: str | None):
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _context_check(created: datetime | None, claims_recent: bool) -> dict | None:
    if not created:
        return None
    age = datetime.now(timezone.utc) - created
    if claims_recent and age > timedelta(days=30):
        return {"severity": "high", "finding": f"File metadata says it was created on {created.date()} ({age.days} days ago), but it is being presented as new. "
                                               "Possible 'authentic media, false context' — metadata can be edited, so confirm with reverse search."}
    return {"severity": "info", "finding": f"Metadata creation date: {created.date()}."}


def analyse_image(data: bytes, filename: str, claims_recent: bool = False) -> dict:
    img = Image.open(io.BytesIO(data))
    img.load()
    marks = _markers(data)
    exif = _exif(img)
    soft = (exif.get("Software", "") + " " + exif.get("PNG:parameters", "") + exif.get("PNG:prompt", "")).lower()
    if any(k in soft for k in ("stable diffusion", "midjourney", "dall", "firefly", "comfyui", "steps:", "sampler")):
        marks["generator"] = sorted(set(marks["generator"]) | {"generator settings in metadata"})
    ela = error_level_analysis(img)
    created = _parse_exif_date(exif.get("DateTimeOriginal") or exif.get("DateTime"))
    checks = {
        "A_origin_credentials": {
            "title": "Origin and content credentials",
            "result": ("Content credentials (C2PA) manifest present — open it in a C2PA verifier (contentcredentials.org/verify) to read the signed edit history."
                       if marks["c2pa"] else "No content credentials found. Missing credentials do NOT mean the file is fake."),
            "generator_markers": marks["generator"], "editing_software": marks["editors"],
        },
        "B_reverse_search": {
            "title": "Reverse image search",
            "result": "Not run automatically (no free reverse-search API). Use the links below with this file to find older copies or the original context.",
            "links": REVERSE_SEARCH,
        },
        "C_manipulation_signs": {
            "title": "Signs of manipulation (Error Level Analysis)",
            "result": (f"Mean error level {ela['mean_error']}, {ela['high_error_share']:.1%} of pixels with high error. "
                       "Bright, uneven patches can indicate pasted or edited areas, but recompression, text and sharp edges also look bright. Indicator only."),
            "ela": ela,
        },
        "D_scene_verification": {
            "title": "Scene verification",
            "result": "Requires a human check of landmarks, signs, weather, shadows and uniforms against the claimed place and date.",
        },
        "E_metadata": {"title": "Metadata", "result": exif or "No EXIF metadata (common for images shared through social media, which strips it).", "context": _context_check(created, claims_recent)},
    }
    return _outcome("image", filename, data, checks, marks, created, claims_recent,
                    extra={"width": img.width, "height": img.height, "format": img.format, "average_hash": average_hash(img)})


def _mp4_creation_time(data: bytes):
    """Read 'creation_time' from the mvhd box of an MP4/MOV file."""
    i = data.find(b"mvhd")
    if i < 0:
        return None
    version = data[i + 4]
    try:
        if version == 1:
            secs = struct.unpack(">Q", data[i + 8:i + 16])[0]
        else:
            secs = struct.unpack(">I", data[i + 8:i + 12])[0]
    except struct.error:
        return None
    if secs <= 0:
        return None
    dt = datetime(1904, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=secs)
    return dt if 1990 < dt.year <= datetime.now().year + 1 else None


def analyse_video(data: bytes, filename: str, claims_recent: bool = False) -> dict:
    import cv2

    suffix = Path(filename).suffix or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    cap = cv2.VideoCapture(path)
    frames_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    keyframes, cuts, prev_hist, sample_every = [], [], None, max(1, int(fps // 2) or 1)
    picks = {int(frames_total * p) for p in (0.05, 0.3, 0.55, 0.8)} if frames_total else set()
    idx = 0
    while frames_total and idx < frames_total and idx < 20000:
        ok, frame = cap.read()
        if not ok:
            break
        if idx in picks or (not picks and idx == 0):
            img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            img.thumbnail((480, 480))
            keyframes.append({"time": round(idx / fps, 1) if fps else None, "image": _b64_jpg(img)})
        if idx % sample_every == 0:
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
            cv2.normalize(hist, hist)
            if prev_hist is not None:
                d = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA)
                if d > 0.55:
                    cuts.append(round(idx / fps, 1) if fps else idx)
            prev_hist = hist
        idx += 1
    cap.release()
    Path(path).unlink(missing_ok=True)
    marks = _markers(data)
    created = _mp4_creation_time(data[:5_000_000])
    duration = round(frames_total / fps, 1) if fps else None
    checks = {
        "A_origin_credentials": {"title": "Origin and content credentials",
                                 "result": "Content credentials present." if marks["c2pa"] else "No content credentials found (absence is not evidence of fakery).",
                                 "generator_markers": marks["generator"], "editing_software": marks["editors"]},
        "B_reverse_search": {"title": "Reverse video / keyframe search",
                             "result": f"{len(keyframes)} keyframes extracted below — search them to find older uploads of the same footage.",
                             "links": REVERSE_SEARCH, "keyframes": keyframes},
        "C_manipulation_signs": {"title": "Signs of manipulation",
                                 "result": f"{len(cuts)} abrupt visual changes (cuts/splices) detected at seconds {cuts[:10]}. Cuts are normal in edited news video; "
                                           "they matter only if the clip is presented as one continuous recording. Face-blending and lip-sync checks need a dedicated deepfake model and are not run.",
                                 "cuts": cuts[:30]},
        "D_scene_verification": {"title": "Scene verification", "result": "Compare landmarks, signs, weather, shadows and uniforms in the keyframes with the claimed place and date (human review)."},
        "E_audio_transcript": {"title": "Audio, transcript and speaker", "result": "Not assessed automatically. Compare the spoken words with an original recording or official transcript using the transcript-match tool."},
        "metadata": {"title": "Container metadata", "result": {"duration_s": duration, "fps": round(fps, 2), "resolution": f"{w}x{h}",
                                                               "creation_time": created.isoformat() if created else "not present"},
                     "context": _context_check(created, claims_recent)},
    }
    return _outcome("video", filename, data, checks, marks, created, claims_recent, extra={"duration_s": duration, "resolution": f"{w}x{h}"})


def analyse_audio(data: bytes, filename: str, claims_recent: bool = False) -> dict:
    info = {"size_kb": round(len(data) / 1024, 1)}
    if filename.lower().endswith(".wav"):
        try:
            with wave.open(io.BytesIO(data)) as w:
                info.update({"duration_s": round(w.getnframes() / w.getframerate(), 2), "sample_rate": w.getframerate(), "channels": w.getnchannels()})
        except wave.Error:
            pass
    tags = {}
    if data[:3] == b"ID3":
        for tag in (b"TIT2", b"TPE1", b"TDRC", b"TENC", b"TSSE"):
            i = data.find(tag, 0, 4096)
            if i > 0:
                size = struct.unpack(">I", data[i + 4:i + 8])[0]
                tags[tag.decode()] = re.sub(r"[\x00-\x1f]", "", data[i + 11:i + 10 + min(size, 80)].decode("latin-1", "ignore"))
    marks = _markers(data)
    checks = {
        "A_origin_credentials": {"title": "Audio origin", "result": "No provenance record found." if not marks["c2pa"] else "Content credentials present.",
                                 "generator_markers": marks["generator"], "editing_software": marks["editors"], "tags": tags},
        "E_audio_transcript": {"title": "Transcript and speaker", "result": "Voice-clone detection is not run (no validated free detector). Use the transcript-match tool: paste what the clip says and the official transcript/original recording text."},
        "metadata": {"title": "File information", "result": info},
    }
    return _outcome("audio", filename, data, checks, marks, None, claims_recent, extra=info)


def _outcome(kind, filename, data, checks, marks, created, claims_recent, extra) -> dict:
    reasons = []
    if marks["generator"]:
        label = "Synthetic media (declared in file metadata)"
        reasons.append("The file itself declares AI generation: " + ", ".join(marks["generator"]) + ". If it is shown as proof of a real event, that is misleading use.")
    elif created and claims_recent and (datetime.now(timezone.utc) - created) > timedelta(days=30):
        label = "Possible authentic media, false context"
        reasons.append(f"Metadata creation date {created.date()} is older than the 'new' event it is shared as. Metadata can be altered — verify with reverse search.")
    else:
        label = "Media authenticity inconclusive"
        reasons.append("No declared provenance and no original file to compare. Detector-style signals are indicators, not proof.")
    return {
        "kind": kind, "filename": filename, "sha256": hashlib.sha256(data).hexdigest(), "size_kb": round(len(data) / 1024, 1),
        "label": label, "reasons": reasons, "checks": checks, **extra,
        "possible_labels": ["Authentic media, false context", "Synthetic media, misleading use", "Manipulated audio or video", "Media authenticity inconclusive"],
        "warning": "A detector score is never the final judge (§8). Detection research reports a 10–15% performance drop on new, unseen cases.",
    }


def analyse(data: bytes, filename: str, content_type: str, claims_recent: bool = False) -> dict:
    ct = (content_type or "").lower()
    name = filename.lower()
    if ct.startswith("image/") or name.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff")):
        return analyse_image(data, filename, claims_recent)
    if ct.startswith("video/") or name.endswith((".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp")):
        return analyse_video(data, filename, claims_recent)
    if ct.startswith("audio/") or name.endswith((".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac", ".opus")):
        return analyse_audio(data, filename, claims_recent)
    return {"kind": "unknown", "filename": filename, "label": "Unsupported file type", "reasons": ["Upload an image, video or audio file."], "checks": {}}


def media_kind(filename: str, content_type: str) -> str | None:
    ct, n = (content_type or "").lower(), (filename or "").lower()
    if ct.startswith("image/") or n.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return "image"
    if ct.startswith("video/") or n.endswith((".mp4", ".mov", ".webm", ".mkv", ".avi", ".3gp")):
        return "video"
    if ct.startswith("audio/") or n.endswith((".mp3", ".wav", ".m4a", ".ogg", ".aac", ".opus")):
        return "audio"
    return None
