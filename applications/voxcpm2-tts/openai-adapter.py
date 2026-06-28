"""OpenAI-compatible TTS adapter for VoxCPM2 Nano-vLLM backend with Voice Management."""

import base64
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, Response, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from pydub import AudioSegment

app = FastAPI(title="VoxCPM2 OpenAI TTS Adapter")

BACKEND_URL = os.getenv("VOXCPM_BACKEND_URL", "http://localhost:8000")
VOICES_DIR = Path(os.getenv("VOICES_DIR", "/shared/voices"))

# Voice Design fallback descriptions (used when no reference audio registered)
VOICE_DESIGN: dict[str, str] = {
    "alloy": "Female voice. A young woman in her mid-20s with a clear, balanced, and versatile voice. Speaks with natural confidence and a neutral American accent, suitable for narration and general conversation",
    "ash": "Male voice. A young man in his late 20s with a confident, direct voice. Slightly husky tone with assertive delivery, like a tech podcast host",
    "ballad": "Male voice. A warm-toned man in his 30s with an expressive, melodic voice. Rich baritone with gentle emotional inflections, like a storyteller by the fireplace",
    "coral": "Female voice. A friendly young woman in her mid-20s with a bright, conversational voice. Speaks with natural warmth and a slight smile, like chatting with a close friend",
    "echo": "Male voice. A young man in his late 20s with a smooth, warm voice. Relaxed and easygoing delivery with a mellow tone, like a late-night radio DJ",
    "fable": "Male voice. A distinguished British gentleman in his 40s with a deep, authoritative voice. Refined accent with measured pacing, like a classic audiobook narrator",
    "onyx": "Male voice. A mature man in his 40s with a deep, resonant bass voice. Calm and composed delivery that commands attention, like a documentary narrator",
    "nova": "Female voice. A young woman in her early 20s with an energetic, bright voice. Enthusiastic and upbeat with a playful sparkle, perfect for engaging with children and young audiences",
    "sage": "Female voice. A composed woman in her 30s with a calm, reassuring voice. Speaks with quiet authority and gentle wisdom, like a trusted counselor or teacher",
    "shimmer": "Female voice. A gentle young woman with a soft, warm, and soothing voice. Tender and nurturing tone with a comforting quality, like a kind older sister reading a bedtime story",
    "verse": "Male voice. A clear-spoken man in his early 30s with an articulate, versatile voice. Precise diction with natural expressiveness, like a professional voice actor",
    "marin": "Female voice. A natural young woman in her mid-20s with an approachable, down-to-earth voice. Casual and relatable with authentic warmth, like a friendly neighbor",
    "cedar": "Male voice. A steady, trustworthy man in his late 40s with a mature, grounded voice. Reliable and reassuring tone with unhurried pacing, like a wise mentor",
}

CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    "opus": "audio/opus",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
}


# --- Voice Registry ---

def _voices_json() -> Path:
    return VOICES_DIR / "registry.json"


def _load_registry() -> dict:
    path = _voices_json()
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _save_registry(registry: dict):
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    _voices_json().write_text(json.dumps(registry, indent=2, ensure_ascii=False))


def _normalize_audio(input_bytes: bytes, fmt: str) -> bytes:
    """Normalize audio to -16 LUFS, 16kHz mono WAV using ffmpeg."""
    with tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False) as inp:
        inp.write(input_bytes)
        inp_path = inp.name
    out_path = inp_path + ".norm.wav"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", inp_path, "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
             "-ar", "16000", "-ac", "1", out_path],
            capture_output=True, check=True, timeout=30,
        )
        return Path(out_path).read_bytes()
    finally:
        os.unlink(inp_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


def _get_voice_audio_b64(voice_id: str) -> str | None:
    """Get base64-encoded reference audio for a registered voice."""
    registry = _load_registry()
    if voice_id not in registry:
        return None
    filename = registry[voice_id].get("file", "ref.wav")
    audio_path = VOICES_DIR / voice_id / filename
    if not audio_path.exists():
        return None
    return base64.b64encode(audio_path.read_bytes()).decode()


# --- Audio helpers ---

def _convert_audio(audio_bytes: bytes, target_format: str) -> bytes:
    audio = AudioSegment.from_mp3(io.BytesIO(audio_bytes))
    buf = io.BytesIO()
    if target_format == "opus":
        audio.export(buf, format="opus", codec="libopus", bitrate="64k", parameters=["-ar", "48000"])
    elif target_format == "wav":
        audio.export(buf, format="wav")
    elif target_format == "flac":
        audio.export(buf, format="flac")
    elif target_format == "aac":
        audio.export(buf, format="adts", codec="aac")
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {target_format}")
    return buf.getvalue()


async def _call_backend(payload: dict, timeout: float = 180.0) -> bytes:
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(f"{BACKEND_URL}/generate", json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=str(e))
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Backend error: {e}")
    return resp.content


async def _stream_backend(payload: dict) -> StreamingResponse:
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0))

    async def generate():
        try:
            async with client.stream("POST", f"{BACKEND_URL}/generate", json=payload) as resp:
                if resp.status_code != 200:
                    raise HTTPException(status_code=resp.status_code, detail="Backend error")
                async for chunk in resp.aiter_bytes(chunk_size=4096):
                    yield chunk
        finally:
            await client.aclose()

    return StreamingResponse(generate(), media_type="audio/mpeg")


# --- TTS endpoint ---

class SpeechRequest(BaseModel):
    model: str = "tts-1"
    input: str
    voice: str = "alloy"
    response_format: str = Field(default="mp3")
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    stream: bool = Field(default=False)
    cfg_value: float | None = Field(default=None, description="CFG guidance scale (default 1.5)")


@app.post("/v1/audio/speech")
async def create_speech(req: SpeechRequest):
    # Check if voice has registered reference audio
    ref_b64 = _get_voice_audio_b64(req.voice)

    if ref_b64:
        # Controllable Cloning mode
        payload = {
            "target_text": req.input,
            "ref_audio_wav_base64": ref_b64,
            "ref_audio_wav_format": "wav",
        }
    else:
        # Voice Design mode (fallback to description)
        voice_desc = VOICE_DESIGN.get(req.voice, req.voice)
        payload = {"target_text": f"({voice_desc}){req.input}"}

    if req.cfg_value is not None:
        payload["cfg_value"] = req.cfg_value

    if req.stream:
        return await _stream_backend(payload)

    audio_bytes = await _call_backend(payload)

    if req.response_format != "mp3":
        audio_bytes = _convert_audio(audio_bytes, req.response_format)

    content_type = CONTENT_TYPES.get(req.response_format, "application/octet-stream")
    return Response(content=audio_bytes, media_type=content_type)


# --- Clone endpoint (ad-hoc, no registered voice needed) ---

class CloneRequest(BaseModel):
    input: str
    reference_audio: str  # base64 encoded audio
    reference_format: str = "wav"
    prompt_text: str | None = None
    response_format: str = "mp3"
    cfg_value: float | None = None


@app.post("/v1/audio/clone")
async def clone_speech(req: CloneRequest):
    payload = {
        "target_text": req.input,
        "ref_audio_wav_base64": req.reference_audio,
        "ref_audio_wav_format": req.reference_format,
    }
    if req.prompt_text:
        payload["prompt_wav_base64"] = req.reference_audio
        payload["prompt_wav_format"] = req.reference_format
        payload["prompt_text"] = req.prompt_text
    if req.cfg_value is not None:
        payload["cfg_value"] = req.cfg_value

    audio_bytes = await _call_backend(payload, timeout=300.0)

    if req.response_format != "mp3":
        audio_bytes = _convert_audio(audio_bytes, req.response_format)

    content_type = CONTENT_TYPES.get(req.response_format, "application/octet-stream")
    return Response(content=audio_bytes, media_type=content_type)


# --- Voice Management API ---

@app.get("/v1/audio/voices")
async def list_voices():
    registry = _load_registry()
    voices = []
    for vid, meta in registry.items():
        voices.append({
            "voice_id": vid, "name": meta.get("name", vid),
            "description": meta.get("description", ""),
        })
    # Also include design-only voices not yet registered
    for vid in VOICE_DESIGN:
        if vid not in registry:
            voices.append({"voice_id": vid, "name": vid, "description": VOICE_DESIGN[vid], "type": "design_only"})
    return {"voices": voices}


@app.post("/v1/audio/voices")
async def create_voice(
    voice_id: str = Form(...),
    name: str = Form(None),
    description: str = Form(None),
    audio: UploadFile = File(...),
):
    registry = _load_registry()

    audio_bytes = await audio.read()
    fmt = audio.filename.rsplit(".", 1)[-1].lower() if audio.filename else "wav"
    normalized = _normalize_audio(audio_bytes, fmt)

    voice_dir = VOICES_DIR / voice_id
    voice_dir.mkdir(parents=True, exist_ok=True)
    (voice_dir / "ref.wav").write_bytes(normalized)

    from datetime import datetime, timezone
    registry[voice_id] = {
        "file": "ref.wav",
        "name": name or voice_id,
        "description": description or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_registry(registry)
    return {"voice_id": voice_id, "status": "created"}


@app.put("/v1/audio/voices/{voice_id}")
async def update_voice(
    voice_id: str,
    name: str = Form(None),
    description: str = Form(None),
    audio: UploadFile = File(None),
):
    registry = _load_registry()
    if voice_id not in registry:
        raise HTTPException(status_code=404, detail=f"Voice '{voice_id}' not found")

    if audio:
        audio_bytes = await audio.read()
        fmt = audio.filename.rsplit(".", 1)[-1].lower() if audio.filename else "wav"
        normalized = _normalize_audio(audio_bytes, fmt)
        voice_dir = VOICES_DIR / voice_id
        voice_dir.mkdir(parents=True, exist_ok=True)
        (voice_dir / "ref.wav").write_bytes(normalized)

    if name is not None:
        registry[voice_id]["name"] = name
    if description is not None:
        registry[voice_id]["description"] = description
    _save_registry(registry)
    return {"voice_id": voice_id, "status": "updated"}


@app.get("/v1/audio/voices/{voice_id}")
async def get_voice(voice_id: str):
    registry = _load_registry()
    if voice_id not in registry:
        if voice_id in VOICE_DESIGN:
            return {"voice_id": voice_id, "type": "design_only", "description": VOICE_DESIGN[voice_id]}
        raise HTTPException(status_code=404, detail=f"Voice '{voice_id}' not found")
    meta = registry[voice_id]
    return {"voice_id": voice_id, "name": meta.get("name"), "description": meta.get("description"),
            "created_at": meta.get("created_at")}


@app.delete("/v1/audio/voices/{voice_id}")
async def delete_voice(voice_id: str):
    registry = _load_registry()
    if voice_id not in registry:
        raise HTTPException(status_code=404, detail=f"Voice '{voice_id}' not found")
    voice_dir = VOICES_DIR / voice_id
    if voice_dir.exists():
        import shutil
        shutil.rmtree(voice_dir)
    del registry[voice_id]
    _save_registry(registry)
    return {"voice_id": voice_id, "status": "deleted"}


@app.get("/v1/audio/voices/{voice_id}/preview")
async def preview_voice(voice_id: str):
    registry = _load_registry()
    if voice_id not in registry:
        raise HTTPException(status_code=404, detail=f"Voice '{voice_id}' not found")
    filename = registry[voice_id].get("file", "ref.wav")
    audio_path = VOICES_DIR / voice_id / filename
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="Reference audio file missing")
    return Response(content=audio_path.read_bytes(), media_type="audio/wav")


# --- Health ---

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(f"{BACKEND_URL}/ready")
            if resp.status_code == 200:
                return {"status": "ready"}
        except httpx.RequestError:
            pass
    raise HTTPException(status_code=503, detail="Backend not ready")
