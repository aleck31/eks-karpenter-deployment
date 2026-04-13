"""OpenAI-compatible TTS adapter for VoxCPM2 Nano-vLLM backend."""

import io
import os
import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from pydub import AudioSegment

app = FastAPI(title="VoxCPM2 OpenAI TTS Adapter")

BACKEND_URL = os.getenv("VOXCPM_BACKEND_URL", "http://localhost:8000")

# Voice name → VoxCPM2 Voice Design description mapping
# Add/modify entries to match your downstream voice names
VOICE_MAP: dict[str, str] = {
    # OpenAI built-in voices (13)
    # Prompts tuned for VoxCPM2 Voice Design — vivid, expressive descriptions
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
    # Child-friendly aliases
    "kids": "Female voice. A cheerful young woman with a bright, playful, and animated voice. Speaks with excitement and wonder, using a sing-song rhythm that delights children. Warm, encouraging, and full of energy, like a beloved kindergarten teacher",
    # Add custom voice IDs below as needed
    # "English_expressive_narrator": "A professional narrator with rich expressiveness and engaging delivery",
}

CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    "opus": "audio/opus",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
}


class SpeechRequest(BaseModel):
    model: str = "tts-1"
    input: str
    voice: str = "alloy"
    response_format: str = Field(default="mp3")
    speed: float = Field(default=1.0, ge=0.25, le=4.0)


@app.post("/v1/audio/speech")
async def create_speech(req: SpeechRequest):
    # Build voice design prefix if voice is mapped
    voice_desc = VOICE_MAP.get(req.voice, req.voice)
    target_text = f"({voice_desc}){req.input}"

    payload = {"target_text": target_text}

    async with httpx.AsyncClient(timeout=180.0) as client:
        try:
            resp = await client.post(f"{BACKEND_URL}/generate", json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=str(e))
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Backend error: {e}")

    audio_bytes = resp.content  # MP3 from backend

    # Convert format if needed
    if req.response_format == "mp3":
        out_bytes = audio_bytes
    else:
        audio = AudioSegment.from_mp3(io.BytesIO(audio_bytes))
        buf = io.BytesIO()
        export_params = {}
        if req.response_format == "opus":
            export_params = {"codec": "libopus", "bitrate": "64k", "parameters": ["-ar", "48000"]}
            audio.export(buf, format="opus", **export_params)
        elif req.response_format == "wav":
            audio.export(buf, format="wav")
        elif req.response_format == "flac":
            audio.export(buf, format="flac")
        elif req.response_format == "aac":
            audio.export(buf, format="adts", codec="aac")
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported format: {req.response_format}")
        out_bytes = buf.getvalue()

    content_type = CONTENT_TYPES.get(req.response_format, "application/octet-stream")
    return Response(content=out_bytes, media_type=content_type)


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
