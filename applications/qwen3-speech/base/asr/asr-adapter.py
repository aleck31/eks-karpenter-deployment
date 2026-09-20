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
import subprocess
import tempfile
import httpx
from fastapi import FastAPI, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

app = FastAPI(
    title="Qwen3-ASR OpenAI Adapter",
    description=(
        "OpenAI-compatible ASR endpoints.\n\n"
        "**`/v1/realtime` is a WebSocket.** OpenAPI cannot describe one, so the "
        "entry under `paths` is a plain GET that returns the event protocol; "
        "the streaming endpoint itself is reached by upgrading the same path."
    ),
)

# Segments run concurrently, so wall time is roughly
# ceil(segments / SEGMENT_CONCURRENCY) * SEGMENT_MAX_SECONDS * 0.8 -- about 13
# minutes for a one-hour recording, not the ~48 serial processing would take.
# 0.35s of budget per second of audio covers that with room to spare while
# staying under the ALB's 1800s idle timeout.
TRANSCRIBE_TIMEOUT = float(os.getenv("ASR_TRANSCRIBE_TIMEOUT", "300"))
TRANSCRIBE_TIMEOUT_PER_AUDIO_SECOND = float(
    os.getenv("ASR_TIMEOUT_PER_AUDIO_SECOND", "0.35")
)
# vLLM batches requests, so segments overlap. Bounded because each concurrent
# segment holds its own encoder cache allocation.
SEGMENT_CONCURRENCY = int(os.getenv("ASR_SEGMENT_CONCURRENCY", "4"))
# Measured on Qwen3-ASR-1.7B: audio embedding tokens per second of input, used
# to restate the backend's encoder-cache error as an audio-length limit.
AUDIO_TOKENS_PER_SECOND = 13.0

# Long recordings are split at speech boundaries before reaching the backend.
# Quality degrades well before the token ceiling: 65s transcribed to exactly the
# input text, 309s returned about two thirds of the words, 619s returned 202
# characters -- each with a 200 and no sign anything was dropped.
#
# Cutting on speech boundaries rather than a clock is the point. The backend's
# own realtime path commits on a fixed window and splits words mid-syllable
# ("浪也很" / "高沙滩上").
SEGMENT_MAX_SECONDS = float(os.getenv("ASR_SEGMENT_MAX_SECONDS", "120"))
# Below this a recording goes through untouched -- segmenting a voice turn would
# cost a VAD pass for nothing.
SEGMENT_MIN_SECONDS = float(os.getenv("ASR_SEGMENT_MIN_SECONDS", "150"))
# Silero VAD operates on 16kHz mono.
VAD_SAMPLE_RATE = 16000
# A gap has to be at least this long to be a sentence boundary rather than the
# pause between two words.
VAD_MIN_GAP_SECONDS = float(os.getenv("ASR_VAD_MIN_GAP_SECONDS", "0.35"))

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


def _suffix_of(filename: str) -> str:
    """Keep the caller's extension so ffprobe/ffmpeg pick the right demuxer."""
    ext = os.path.splitext(filename)[1].lower()
    return ext if 1 < len(ext) <= 6 and ext[1:].isalnum() else ".wav"


def _audio_duration(path: str) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, check=True, timeout=60,
        )
        return float(out.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return None


_vad_model = None


def _load_vad():
    """Load Silero VAD once. Energy thresholding was the wrong tool here.

    ffmpeg's silencedetect only measures amplitude, so in a meeting recording
    room tone, keyboards or a projector fan sit above any threshold low enough
    to catch real pauses -- it would report no silence at all and every cut
    would fall back to the clock. Silero VAD classifies speech, so gaps survive
    background noise.
    """
    global _vad_model
    if _vad_model is None:
        from silero_vad import load_silero_vad
        _vad_model = load_silero_vad()
    return _vad_model


def _speech_gaps(path: str) -> list[float]:
    """Midpoint of every gap between speech runs, in seconds.

    Midpoint rather than either edge: the start of a gap clips the previous
    word's decay, the end of it clips the next word's onset.

    Audio is decoded with ffmpeg and soundfile rather than silero_vad's own
    read_audio, which routes through torchaudio and so pulls in torchcodec on
    2.9+. Both are already present for other reasons.
    """
    try:
        import numpy as np
        import soundfile as sf
        import torch
        from silero_vad import get_speech_timestamps

        pcm_path = f"{path}.vad.wav"
        try:
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", path,
                 "-ar", str(VAD_SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", pcm_path],
                capture_output=True, check=True, timeout=600,
            )
            samples, _ = sf.read(pcm_path, dtype="float32", always_2d=False)
        finally:
            if os.path.exists(pcm_path):
                os.unlink(pcm_path)

        if samples.ndim > 1:
            samples = samples.mean(axis=1)
        runs = get_speech_timestamps(
            torch.from_numpy(np.ascontiguousarray(samples)),
            _load_vad(), sampling_rate=VAD_SAMPLE_RATE, return_seconds=True,
        )
    except Exception:
        # No VAD, no cut points: _segment_bounds falls back to the clock, which
        # is worse but still bounded.
        return []
    gaps = []
    for previous, following in zip(runs, runs[1:]):
        gap = following["start"] - previous["end"]
        if gap >= VAD_MIN_GAP_SECONDS:
            gaps.append(previous["end"] + gap / 2)
    return gaps


def _segment_bounds(duration: float, gaps: list[float]) -> list[tuple[float, float]]:
    """Cut at the last speech gap before each limit, falling back to the clock.

    The clock fallback only fires when a whole budget's worth of audio holds no
    qualifying gap.
    """
    bounds, start = [], 0.0
    while duration - start > SEGMENT_MAX_SECONDS:
        limit = start + SEGMENT_MAX_SECONDS
        # A cut must advance past the halfway mark, or a cluster of early gaps
        # would produce a run of tiny segments.
        candidates = [g for g in gaps
                      if start + SEGMENT_MAX_SECONDS / 2 < g <= limit]
        cut = max(candidates) if candidates else limit
        bounds.append((start, cut))
        start = cut
    bounds.append((start, duration))
    return bounds


def _extract_segment(path: str, start: float, end: float) -> bytes:
    """Cut [start, end) losslessly into a 16kHz mono wav the backend accepts.

    The output path must be unique per call: segments of one request are cut
    concurrently, and a path derived only from the source had every segment
    writing and then unlinking the same file, so one segment's cleanup deleted
    another's output mid-read.
    """
    fd, out_path = tempfile.mkstemp(prefix="seg-", suffix=".wav")
    os.close(fd)
    try:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}",
             "-to", f"{end:.3f}", "-i", path,
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", out_path],
            capture_output=True, check=True, timeout=600,
        )
        with open(out_path, "rb") as fh:
            return fh.read()
    finally:
        if os.path.exists(out_path):
            os.unlink(out_path)


async def _transcribe_once(client: httpx.AsyncClient, audio: bytes,
                           filename: str) -> tuple[str, dict]:
    resp = await client.post(
        f"{BACKEND_URL}/v1/audio/transcriptions",
        files={"file": (filename, audio, "audio/wav")},
    )
    if resp.status_code != 200:
        raise _BackendRejected(resp)
    data = resp.json()
    text, language = _parse_asr_text(data.get("text", ""))
    return text, {"language": language, "usage": data.get("usage")}


class _BackendRejected(Exception):
    def __init__(self, resp: httpx.Response):
        self.resp = resp


def _backend_error_payload(resp: httpx.Response) -> dict:
    """Normalise a backend error, translating the ones callers can act on.

    vLLM rejects over-long audio two different ways depending on which ceiling
    it hits first -- the encoder cache or max_model_len -- and both messages
    name an internal buffer rather than the thing the caller controls. Restate
    either as an audio-length limit, derived from the measured audio embedding
    tokens per second.
    """
    try:
        body = resp.json()
    except ValueError:
        return {"error": {"message": f"Transcription backend returned HTTP {resp.status_code}",
                          "type": "backend_error"}}
    message = str(((body or {}).get("error") or {}).get("message", ""))

    needed = budget = None
    match = re.search(r"audio item with (\d+) embedding tokens.*?"
                      r"encoder cache size (\d+)", message)
    if match:
        needed, budget = int(match.group(1)), int(match.group(2))
    else:
        match = re.search(r"decoder prompt \(length (\d+)\) is longer than the "
                          r"maximum model length of (\d+)", message)
        if match:
            needed, budget = int(match.group(1)), int(match.group(2))

    if needed is None or budget is None:
        # Always assigned together; stated so the pairing survives an edit.
        return body
    return {"error": {
        "message": f"Audio is too long: it needs {needed} tokens against a budget of "
                   f"{budget}. At {AUDIO_TOKENS_PER_SECOND:g} tokens per second that is "
                   f"about {budget / AUDIO_TOKENS_PER_SECOND:.0f}s "
                   f"({budget / AUDIO_TOKENS_PER_SECOND / 60:.1f} minutes). Split the "
                   "recording, or raise both --max-model-len and "
                   "--max-num-batched-tokens on the backend.",
        "type": "audio_too_long",
        "limit_seconds": round(budget / AUDIO_TOKENS_PER_SECOND),
    }}


# --- HTTP: /v1/audio/transcriptions ---

@app.post("/v1/audio/transcriptions")
async def transcribe(file: UploadFile = File(...), model: str = Form(default="")):
    # model is accepted for OpenAI compatibility and deliberately not forwarded.
    # vLLM registers the model under its filesystem path, so passing the
    # caller's value through rejects every friendly name with a 404 while
    # leaking the server's layout to anyone who guesses right. This pod serves
    # one model, so there is nothing to select. Matches the TTS adapter, which
    # also accepts and discards the field.
    audio_bytes = await file.read()
    filename = file.filename or "audio.wav"

    with tempfile.NamedTemporaryFile(suffix=_suffix_of(filename), delete=False) as fh:
        fh.write(audio_bytes)
        source_path = fh.name
    try:
        duration = _audio_duration(source_path)
        segments = (_segment_bounds(duration, _speech_gaps(source_path))
                    if duration and duration > SEGMENT_MIN_SECONDS else None)
        budget = max(TRANSCRIBE_TIMEOUT,
                     (duration or 0) * TRANSCRIBE_TIMEOUT_PER_AUDIO_SECOND)

        async with httpx.AsyncClient(timeout=budget) as client:
            try:
                if segments is None:
                    text, meta = await _transcribe_once(client, audio_bytes, filename)
                    parts, language = [text], meta["language"]
                else:
                    gate = asyncio.Semaphore(SEGMENT_CONCURRENCY)

                    async def run(index: int, start: float, end: float):
                        # The gate covers the cut as well as the request: a
                        # one-hour recording is 30 segments, and cutting them
                        # all at once would fan out 30 ffmpeg processes across
                        # the container's 2 CPUs.
                        async with gate:
                            # Slicing is CPU work in ffmpeg; keep it off the
                            # event loop so concurrent segments overlap.
                            chunk = await asyncio.to_thread(
                                _extract_segment, source_path, start, end)
                            return await _transcribe_once(
                                client, chunk, f"seg{index:03d}.wav")

                    results = await asyncio.gather(*[
                        run(i, s0, e0) for i, (s0, e0) in enumerate(segments)
                    ])
                    # Ordered by segment, not by completion: gather preserves
                    # input order, which is what makes the concatenation valid.
                    parts = [text for text, _ in results if text]
                    language = next((m["language"] for _, m in results
                                     if m["language"]), None)
            except httpx.TimeoutException:
                # ~0.8x realtime, so a voice-turn-sized timeout turned long
                # recordings into an empty 500 naming nothing.
                return JSONResponse(
                    status_code=504,
                    content={"error": {
                        "message": f"Transcription did not finish within "
                                   f"{budget:g}s. Raise ASR_TRANSCRIBE_TIMEOUT or "
                                   "ASR_TIMEOUT_PER_AUDIO_SECOND.",
                        "type": "timeout",
                    }},
                )
            except _BackendRejected as rejected:
                return JSONResponse(status_code=rejected.resp.status_code,
                                    content=_backend_error_payload(rejected.resp))
    finally:
        if os.path.exists(source_path):
            os.unlink(source_path)

    result: dict[str, object] = {"text": "".join(parts)}
    if language:
        result["language"] = language
    if duration is not None:
        result["duration"] = round(duration, 2)
    if segments is not None:
        # Lets a caller tell a segmented transcript from a single-pass one.
        result["segments"] = len(segments)
    return JSONResponse(content=result)


# --- WebSocket: /v1/realtime ---

_backend_model_id: str | None = None


async def _resolve_backend_model() -> str | None:
    """Look up the id vLLM registered the weights under, and cache it.

    vLLM names the model after its filesystem path. Callers should not have to
    know that, so the adapter substitutes it on their behalf -- the HTTP path
    achieves the same by simply not forwarding the field.
    """
    global _backend_model_id
    if _backend_model_id is None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                resp = await c.get(f"{BACKEND_URL}/v1/models")
                resp.raise_for_status()
                _backend_model_id = resp.json()["data"][0]["id"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            return None
    return _backend_model_id


def _rewrite_model(raw: str, real_model: str | None) -> str:
    """Point any model reference in a client frame at the backend's real id.

    session.update requires model at the top level of the frame -- the backend
    rejects it nested under session -- so normalise either spelling to there.
    Rewriting keeps the WebSocket as tolerant of the value as HTTP is, and
    supplies it when the caller left it out.
    """
    if not real_model:
        return raw
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(data, dict):
        return raw
    if data.get("type") != "session.update" and "model" not in data:
        return raw
    data["model"] = real_model
    session = data.get("session")
    if isinstance(session, dict):
        session.pop("model", None)
    return json.dumps(data)


@app.get("/v1/realtime")
async def realtime_info():
    """Describe the WebSocket protocol served at this same path.

    Exists so the streaming endpoint is discoverable from the OpenAPI schema.
    OpenAPI cannot express a WebSocket, so FastAPI leaves @app.websocket routes
    out of `paths` entirely -- a client generating from the schema would never
    learn this endpoint is here.
    """
    return {
        "protocol": "websocket",
        "url": "ws(s)://<host>/v1/realtime",
        "description": "Streaming transcription. Send raw audio frames, receive "
                       "OpenAI Realtime transcription events.",
        "server_events": [
            "conversation.item.input_audio_transcription.delta",
            "conversation.item.input_audio_transcription.completed",
        ],
        "notes": [
            "Backend emits 'language XXX<asr_text>' prefixes and vendor event "
            "names; this adapter strips the prefix, lifts the language out, and "
            "renames events to the OpenAI convention.",
            "Transcription context accumulates over a session, so a very long "
            "one can reach the model's max-model-len.",
        ],
    }


@app.websocket("/v1/realtime")
async def realtime_proxy(client_ws: WebSocket):
    await client_ws.accept()

    import websockets

    backend_url = f"{BACKEND_WS}/v1/realtime"
    real_model = await _resolve_backend_model()
    async with websockets.connect(backend_url) as backend_ws:
        # Forward session.created to client
        msg = await backend_ws.recv()
        await client_ws.send_text(msg)

        # State for filtering prefix tokens
        prefix_done = False  # True after <asr_text> token seen
        language = None

        async def client_to_backend():
            """Forward client messages to vLLM, retargeting model references."""
            try:
                while True:
                    data = await client_ws.receive_text()
                    await backend_ws.send(_rewrite_model(data, real_model))
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
