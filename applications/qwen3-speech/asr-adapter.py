"""Qwen3-ASR OpenAI-compatible adapter.

Proxies vLLM's raw Qwen3-ASR output to clean OpenAI-format responses:
- Strips 'language XXX<asr_text>' prefix from text
- Extracts language into separate field
- Filters empty segments
- Renames WebSocket events to OpenAI Realtime convention
"""

import asyncio
import json
import os
import re
import httpx
from fastapi import FastAPI, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

app = FastAPI(title="Qwen3-ASR OpenAI Adapter")

BACKEND_URL = os.getenv("ASR_BACKEND_URL", "http://localhost:8000")
BACKEND_WS = os.getenv("ASR_BACKEND_WS", "ws://localhost:8000")

_PARSE_RE = re.compile(r"language\s+(\w+)<asr_text>")
_STRIP_FIRST_RE = re.compile(r"^language\s+\w+<asr_text>")
_STRIP_INLINE_RE = re.compile(r"\nlanguage\s+\w+<asr_text>")


def _parse_asr_text(raw: str) -> tuple[str, str | None]:
    """Strip all 'language XXX<asr_text>' markers, return clean text + first language."""
    m = _PARSE_RE.search(raw)
    language = m.group(1).lower() if m else None
    # Strip leading marker
    text = _STRIP_FIRST_RE.sub("", raw)
    # Replace inline markers with newline (preserve sentence separation)
    text = _STRIP_INLINE_RE.sub("\n", text).strip()
    return text, language


# --- HTTP: /v1/audio/transcriptions ---

@app.post("/v1/audio/transcriptions")
async def transcribe(file: UploadFile = File(...), model: str = Form(default="")):
    audio_bytes = await file.read()
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{BACKEND_URL}/v1/audio/transcriptions",
            files={"file": (file.filename, audio_bytes, file.content_type or "audio/wav")},
            data={"model": model} if model else {},
        )
    if resp.status_code != 200:
        return JSONResponse(status_code=resp.status_code, content=resp.json())

    data = resp.json()
    text, language = _parse_asr_text(data.get("text", ""))
    result = {"text": text}
    if language:
        result["language"] = language
    if "usage" in data:
        result["duration"] = data["usage"].get("seconds")
    return JSONResponse(content=result)


# --- WebSocket: /v1/realtime ---

@app.websocket("/v1/realtime")
async def realtime_proxy(client_ws: WebSocket):
    await client_ws.accept()

    import websockets

    backend_url = f"{BACKEND_WS}/v1/realtime"
    async with websockets.connect(backend_url) as backend_ws:
        # Forward session.created to client
        msg = await backend_ws.recv()
        await client_ws.send_text(msg)

        # State for filtering prefix tokens
        prefix_done = False  # True after <asr_text> token seen
        language = None

        async def client_to_backend():
            """Forward client messages to vLLM."""
            try:
                while True:
                    data = await client_ws.receive_text()
                    await backend_ws.send(data)
            except WebSocketDisconnect:
                await backend_ws.close()

        async def backend_to_client():
            """Filter and transform vLLM messages to client."""
            nonlocal prefix_done, language

            async for raw_msg in backend_ws:
                data = json.loads(raw_msg)
                msg_type = data.get("type", "")

                if msg_type == "transcription.delta":
                    delta = data.get("delta", "")

                    # Detect start of a new sub-sentence prefix mid-stream
                    if prefix_done and delta.strip() == "language":
                        prefix_done = False

                    if not prefix_done:
                        # Buffer prefix tokens: "language", " Chinese", "<asr_text>"
                        if delta.strip().startswith("language"):
                            continue
                        elif delta.strip() and "<" not in delta:
                            # Language name token (e.g. " English")
                            language = delta.strip().lower()
                            continue
                        elif "<asr_text>" in delta:
                            prefix_done = True
                            after = delta.split("<asr_text>", 1)[1]
                            if after:
                                await client_ws.send_text(json.dumps({
                                    "type": "conversation.item.input_audio_transcription.delta",
                                    "delta": after
                                }))
                            continue
                        else:
                            continue
                    else:
                        if delta:
                            await client_ws.send_text(json.dumps({
                                "type": "conversation.item.input_audio_transcription.delta",
                                "delta": delta
                            }))

                elif msg_type == "transcription.done":
                    raw_text = data.get("text", "")
                    text, lang = _parse_asr_text(raw_text)

                    if text:  # Only send non-empty segments
                        await client_ws.send_text(json.dumps({
                            "type": "conversation.item.input_audio_transcription.completed",
                            "transcript": text,
                            "language": lang or language
                        }))

                    # Reset state for next segment
                    prefix_done = False
                    language = None

                elif msg_type == "error":
                    await client_ws.send_text(json.dumps(data))
                    break

        await asyncio.gather(client_to_backend(), backend_to_client())


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(f"{BACKEND_URL}/health")
            if resp.status_code == 200:
                return {"status": "ready"}
        except httpx.RequestError:
            pass
    return JSONResponse(status_code=503, content={"status": "not ready"})
