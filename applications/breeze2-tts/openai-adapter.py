"""OpenAI-compatible TTS adapter for the Breeze TTS 2 breeze_infer backend.

Takes over the endpoint contract previously served by voxcpm2-tts, so the
request fields callers already send keep working. Two fields are new and both
exist because of how Breeze cloning works:

  ref_text  Breeze requires a reference clip *and* its exact transcript as a
            pair; passing only the audio is rejected by the backend. Stored per
            voice so callers do not have to resend it on every request.
  seed      Breeze's sampling is seeded and reproducible within one set of
            --fast-* flags, so a caller can audition seeds and persist the one
            they liked onto the voice.
"""

import asyncio
import base64
import io
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pysbd
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from pydub import AudioSegment


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _heal_registry()
    yield


app = FastAPI(title="Breeze TTS 2 OpenAI TTS Adapter", lifespan=_lifespan)

BACKEND_URL = os.getenv("BREEZE_BACKEND_URL", "http://localhost:8000")
VOICES_DIR = Path(os.getenv("VOICES_DIR", "/shared/voices"))

# Breeze emits raw little-endian signed 16-bit PCM with no container.
BACKEND_SAMPLE_RATE = 24000
BACKEND_CHANNELS = 1
BACKEND_SAMPLE_WIDTH = 2

# _normalize_audio() forces every uploaded reference to this shape. A stored
# recording that deviates was written straight onto the volume rather than
# through the API, which is worth surfacing rather than leaving each caller to
# compare against a constant it has to hardcode.
REF_SAMPLE_RATE = 16000
REF_CHANNELS = 1
# Reference length guidance, in ascending order. Nothing here rejects an
# upload -- a 2s clip still registers and still clones, just less faithfully.
# Breeze's model card states only "clean speech with minimal background noise"
# without a duration range, so these stay the empirically derived bounds the
# presets were curated against (shortest accepted: 4.34s).
REF_MIN_SECONDS = 3.0
REF_GOOD_MIN_SECONDS = 5.0
REF_GOOD_MAX_SECONDS = 10.0
REF_MAX_USEFUL_SECONDS = 15.0

# Declared by the caller at registration, never inferred from the audio: pitch
# alone misreads altos, tenors, children and non-binary speakers, and a wrong
# label is worse than none. Unlabelled voices stay "unknown".
VOICE_GENDERS = ("female", "male", "neutral", "unknown")

# voice_id doubles as a directory name under VOICES_DIR, so it is constrained to
# characters that cannot traverse or escape a path.
_VOICE_ID_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")

# The backend seeds four RNGs from one value (breeze_infer.runtime.set_all_seeds:
# random, numpy, torch, torch.cuda). The usable range is their intersection, and
# numpy is the narrowest: its legacy MT19937 seeding takes an unsigned 32-bit
# value and raises before torch is ever reached. torch itself documents
# [-2**63, 2**64-1] with negatives remapped, but that headroom is unreachable
# here. Validating in the adapter turns what the backend reports as a 500 into
# a 400 that names the bound.
SEED_MIN = 0
SEED_MAX = 2**32 - 1
# The backend's own default (breeze_infer.api: `seed: int = Form(42)`). Written
# explicitly onto every registered voice rather than left unset: an unset seed
# still produces seeded output, just with the value living in the backend's
# signature instead of the registry, so an upstream change to that default
# would silently shift the timbre of every voice with nothing to point at.
SEED_DEFAULT = 42

# Long input is synthesised in segments rather than passed through whole.
#
# Autoregressive TTS degrades well before it hits its context ceiling: measured
# on this backend, 162 characters still reproduced the text faithfully, while
# 324+ produced repeated passages, drifting pace, and in one run 179s of audio
# that an ASR pass could not transcribe at all -- with no error raised. The
# ceiling itself (2048 positions at the 12.5 Hz codec frame rate, about 164s of
# audio) is far past where quality goes.
#
# Segmenting also shortens first-audio latency on long input, since only the
# first segment has to be generated before bytes start flowing.
MAX_SEGMENT_CHARS = 160
# Silence inserted between segments that were split at a sentence boundary. A
# forced split inside a sentence gets none -- a pause there would be audible as
# a stumble rather than punctuation.
SEGMENT_PAUSE_MS = 250

# Closing marks that belong to the sentence that precedes them. pysbd leaves a
# Chinese closing quote on the following sentence ("他说：“好的。" / "”然后走了。"),
# so they are reattached after segmentation.
_CLOSERS = "”’\"'）)】」』》"
# Fallback split points for a single sentence that exceeds the budget.
_CLAUSE_END = "，,、：:"

# Sentence boundaries come from pysbd rather than a hand-written rule set.
# Hand-rolling this went wrong on exactly the cases SBD libraries exist to
# handle: "Dr. Smith" split mid-name, "2.718. Done." failed to split, and
# "Version 1.2.3 shipped on Jan. 5." needs both behaviours at once. pysbd is
# rule-based (deterministic, no model download, MIT) and covers Chinese and
# English; on mixed input both language settings produced identical output, so
# no language detection is needed.
_SEGMENTER = pysbd.Segmenter(language="zh", clean=False)


def _split_sentences(text: str) -> list[str]:
    """Sentence-split via pysbd, reattaching orphaned closing punctuation."""
    pieces = [p for p in (s.strip() for s in _SEGMENTER.segment(text)) if p]
    merged: list[str] = []
    for piece in pieces:
        lead = 0
        while lead < len(piece) and piece[lead] in _CLOSERS:
            lead += 1
        if lead and merged:
            merged[-1] += piece[:lead]
            piece = piece[lead:].strip()
            if not piece:
                continue
        merged.append(piece)
    return merged

# Shared with voxcpm2-tts on the same volume. Not bumped for ref_text/seed:
# neither can be inferred from an existing entry, so there is no migration to
# run -- a voice without ref_text is rejected at synthesis time with an
# actionable error instead.
REGISTRY_SCHEMA = 2

# The backend serves one request at a time and answers HTTP 409 for anything
# concurrent. Serialising here turns that into a queue rather than an error the
# caller has to retry, but the queue is bounded: past the wait budget a caller
# is better off being told the service is busy than blocking indefinitely.
BACKEND_QUEUE_TIMEOUT = float(os.getenv("BREEZE_QUEUE_TIMEOUT", "120"))
_SLOT_POLL_SECONDS = 0.05


class _BackendSlot:
    """Single-occupancy gate for the backend.

    Deliberately not an asyncio.Lock. `asyncio.wait_for(lock.acquire())` can
    leave the lock held when the timeout fires in the same loop iteration the
    acquire completes: wait_for cancels the waiter, but the lock may already
    have been handed to it, and that ownership is then lost with no holder left
    to release it. On a single-occupancy backend that is a permanent outage --
    observed once here, with the GPU idle and every request answering 503 after
    the full queue budget.

    A flag checked and set with no await in between cannot leak: the loop is
    single-threaded, so the test and the assignment are atomic with respect to
    other coroutines, and a waiter cancelled inside the sleep never set it.
    """

    def __init__(self) -> None:
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    async def acquire(self, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._busy:
            if loop.time() >= deadline:
                raise HTTPException(
                    status_code=503,
                    detail="Synthesis backend is busy. Breeze serves one request at a "
                           f"time; the {timeout:g}s queue budget was exhausted. "
                           "Retry shortly.",
                )
            await asyncio.sleep(_SLOT_POLL_SECONDS)
        self._busy = True

    def release(self) -> None:
        self._busy = False


_BACKEND_SLOT = _BackendSlot()

CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    "opus": "audio/opus",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/L16",
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


def _reference_path(voice_id: str, registry: dict | None = None) -> Path:
    registry = registry if registry is not None else _load_registry()
    filename = registry.get(voice_id, {}).get("file", "ref.wav")
    return VOICES_DIR / voice_id / filename


def _probe_audio(audio_path: Path) -> dict:
    """Extract duration / sample rate / channels from a reference recording."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate,channels,codec_name",
             "-show_entries", "format=duration",
             "-of", "json", str(audio_path)],
            capture_output=True, check=True, timeout=15,
        )
        probed = json.loads(out.stdout)
        stream = (probed.get("streams") or [{}])[0]
        return {
            "duration_seconds": round(float(probed.get("format", {}).get("duration", 0)), 2),
            "sample_rate": int(stream.get("sample_rate", 0)) or None,
            "channels": stream.get("channels"),
            "codec": stream.get("codec_name"),
            "size_bytes": audio_path.stat().st_size,
        }
    except (subprocess.SubprocessError, ValueError, KeyError, OSError):
        return {}


def _audio_warnings(audio: dict) -> list[str]:
    """Advise on a stored reference. Never blocks: a short clip still clones.

    The thresholds live here rather than in each client: the server knows the
    model's preferences, callers would each hardcode their own copy.
    """
    warnings = []
    duration = audio.get("duration_seconds")
    good = f"{REF_GOOD_MIN_SECONDS:g}-{REF_GOOD_MAX_SECONDS:g}s"
    if duration is not None:
        if duration < REF_MIN_SECONDS:
            warnings.append(
                f"duration {duration}s is very short for cloning; it will still work "
                f"but expect the timbre to drift. Re-record at {good} for a faithful clone"
            )
        elif duration < REF_GOOD_MIN_SECONDS:
            warnings.append(
                f"duration {duration}s is usable; {good} clones more reliably"
            )
        elif duration > REF_MAX_USEFUL_SECONDS:
            warnings.append(
                f"duration {duration}s is longer than needed; anything past "
                f"{REF_MAX_USEFUL_SECONDS:g}s adds request size and processing time "
                f"without improving the clone. {good} is the sweet spot"
            )
    rate, channels = audio.get("sample_rate"), audio.get("channels")
    if rate is not None and rate != REF_SAMPLE_RATE:
        warnings.append(
            f"sample rate {rate}Hz is not the expected {REF_SAMPLE_RATE}Hz; "
            "this recording did not go through API normalization"
        )
    if channels is not None and channels != REF_CHANNELS:
        warnings.append(
            f"{channels} channels instead of mono; "
            "this recording did not go through API normalization"
        )
    return warnings


def _registry_warnings(meta: dict) -> list[str]:
    """Advise on fields this backend needs that an entry may predate."""
    warnings = []
    if not meta.get("ref_text"):
        warnings.append(
            "ref_text is not set; Breeze requires the reference clip's exact "
            "transcript, so this voice cannot be used for cloning until one is "
            "supplied via PUT /v1/audio/voices/{voice_id}"
        )
    return warnings


def _schema_file() -> Path:
    return VOICES_DIR / ".schema"


def _read_schema() -> int:
    try:
        return int(_schema_file().read_text().strip())
    except (OSError, ValueError):
        return 1


def _heal_registry() -> dict:
    """Run pending one-time migrations against the shared registry.

    Migrations are gated on a persisted schema version, not on whether a field
    happens to be absent. The v2 step marks presets as builtin by inferring
    from a missing created_at, which is only sound for entries that predate the
    builtin field. Re-running that inference on every boot would mean any
    custom voice whose created_at went missing -- interrupted write, hand edit,
    older client -- gets silently locked behind the 403 guard and can never be
    deleted by its owner. Recording the version freezes the inference to the
    one population it is valid for; every write since sets builtin explicitly.
    """
    registry = _load_registry()
    schema = _read_schema()
    changed = False

    if schema < 2:
        for meta in registry.values():
            if "builtin" not in meta:
                meta["builtin"] = "created_at" not in meta
                changed = True

    # Not a migration: audio metadata is recorded at registration time, so this
    # only fills entries written before that, and is keyed on the file existing
    # rather than on any inference about the entry's origin.
    for voice_id, meta in registry.items():
        # A plain default for a field added later, not an inference about the
        # entry: gender is declared by the caller or stays unknown.
        if "gender" not in meta:
            meta["gender"] = "unknown"
            changed = True
        if "audio" not in meta:
            audio_path = VOICES_DIR / voice_id / meta.get("file", "ref.wav")
            if audio_path.exists():
                meta["audio"] = _probe_audio(audio_path)
                changed = True

    if changed:
        try:
            _save_registry(registry)
        except OSError:
            # Read-only volume: serve the healed values from memory this boot
            # and leave the schema marker alone so the migration retries later.
            return registry
    if schema < REGISTRY_SCHEMA:
        try:
            _schema_file().write_text(str(REGISTRY_SCHEMA))
        except OSError:
            pass
    return registry


def _validate_gender(gender: str) -> str:
    if gender not in VOICE_GENDERS:
        raise HTTPException(
            status_code=400,
            detail=f"gender must be one of {', '.join(VOICE_GENDERS)}; got '{gender}'",
        )
    return gender


def _validate_voice_id(voice_id: str) -> str:
    """Reject a voice_id that could escape the voices directory.

    voice_id is used verbatim as a directory name, and DELETE removes that
    directory with shutil.rmtree. Without this check "../x" writes outside the
    registry and "." deletes every stored voiceprint. Filesystem permissions
    happen to block the most obvious escape here -- /shared is root-owned while
    the container runs as uid 10001 -- but that is incidental protection: any
    target the service can already write to stays reachable.
    """
    if not voice_id or voice_id in (".", ".."):
        raise HTTPException(status_code=400, detail="voice_id must not be empty or a path component")
    if not _VOICE_ID_RE.fullmatch(voice_id):
        raise HTTPException(
            status_code=400,
            detail="voice_id may contain letters, digits, hyphen, underscore and dot "
                   "only, up to 64 characters, and may not start with a dot. It names "
                   "a directory on disk, so path separators are rejected.",
        )
    return voice_id


def _validate_seed(seed: int | None) -> int | None:
    """Reject out-of-range seeds here rather than letting the backend 500.

    Validated on write as well as on synthesis: a voice carrying an unusable
    seed would otherwise fail every future request, with nothing in the voice
    detail to explain why.
    """
    if seed is None:
        return None
    if not SEED_MIN <= seed <= SEED_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"seed must be between {SEED_MIN} and {SEED_MAX} inclusive; "
                   f"got {seed}. The backend seeds numpy's legacy MT19937, which "
                   "takes an unsigned 32-bit value.",
        )
    return seed


def _assert_not_builtin(registry: dict, voice_id: str, action: str):
    """Reject destructive actions on built-in preset voices.

    Built-in voices are curated reference recordings that cannot be regenerated
    identically, so overwriting or deleting them is irreversible.
    """
    if registry.get(voice_id, {}).get("builtin"):
        raise HTTPException(
            status_code=403,
            detail=f"Voice '{voice_id}' is a built-in preset; {action} is not allowed. "
                   "Register a new voice with a different voice_id instead.",
        )


# --- Audio helpers ---

def _wav_header(data_size: int) -> bytes:
    """Build a 44-byte RIFF header for the backend's PCM shape.

    data_size may be 0xFFFFFFFF for a stream of unknown length; players that
    read until EOF accept that, and it is the only way to emit a WAV container
    before the waveform exists.
    """
    byte_rate = BACKEND_SAMPLE_RATE * BACKEND_CHANNELS * BACKEND_SAMPLE_WIDTH
    riff_size = 0xFFFFFFFF if data_size == 0xFFFFFFFF else data_size + 36
    return (
        b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, BACKEND_CHANNELS,
                                BACKEND_SAMPLE_RATE, byte_rate,
                                BACKEND_CHANNELS * BACKEND_SAMPLE_WIDTH,
                                BACKEND_SAMPLE_WIDTH * 8)
        + b"data" + struct.pack("<I", data_size)
    )


def _pcm_to_format(pcm: bytes, target_format: str) -> bytes:
    if target_format == "pcm":
        return pcm
    if target_format == "wav":
        return _wav_header(len(pcm)) + pcm
    audio = AudioSegment.from_raw(
        io.BytesIO(pcm),
        sample_width=BACKEND_SAMPLE_WIDTH,
        frame_rate=BACKEND_SAMPLE_RATE,
        channels=BACKEND_CHANNELS,
    )
    buf = io.BytesIO()
    if target_format == "mp3":
        audio.export(buf, format="mp3", bitrate="192k")
    elif target_format == "opus":
        audio.export(buf, format="opus", codec="libopus", bitrate="64k",
                     parameters=["-ar", "48000"])
    elif target_format == "flac":
        audio.export(buf, format="flac")
    elif target_format == "aac":
        audio.export(buf, format="adts", codec="aac")
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {target_format}")
    return buf.getvalue()


def _build_multipart(fields: dict, ref_audio: bytes | None) -> tuple[bytes, str]:
    """Assemble the backend's multipart body.

    Hand-rolled rather than delegated to httpx so the reference bytes are sent
    exactly as stored -- the transcript must match the audio, and a re-encode
    in between would invalidate that pairing.
    """
    boundary = "----breeze2adapter"
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    if ref_audio is not None:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="ref_audio"; '
            f'filename="ref.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
            + ref_audio + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"




def _split_oversized(sentence: str) -> list[str]:
    """Break a single over-budget sentence, preferring clause punctuation."""
    parts, buf = [], []
    for ch in sentence:
        buf.append(ch)
        if ch in _CLAUSE_END and len(buf) >= MAX_SEGMENT_CHARS // 2:
            parts.append("".join(buf))
            buf = []
    if buf:
        parts.append("".join(buf))

    # Still too long (no usable punctuation): cut on character count. Ugly but
    # bounded, and preferable to handing the backend input it silently mangles.
    out = []
    for part in parts:
        while len(part) > MAX_SEGMENT_CHARS:
            out.append(part[:MAX_SEGMENT_CHARS])
            part = part[MAX_SEGMENT_CHARS:]
        if part:
            out.append(part)
    return out


def segment_text(text: str) -> list[tuple[str, int]]:
    """Split input into (segment, pause_ms_after) pairs within the size budget.

    Sentences are packed greedily so short ones stay together and keep their
    natural prosody. The pause after the final segment is always 0 -- trailing
    silence is the caller's business, not ours.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= MAX_SEGMENT_CHARS:
        return [(text, 0)]

    pieces: list[tuple[str, bool]] = []  # (text, ended_at_sentence_boundary)
    for sentence in _split_sentences(text):
        if len(sentence) <= MAX_SEGMENT_CHARS:
            pieces.append((sentence, True))
            continue
        chunks = _split_oversized(sentence)
        for i, chunk in enumerate(chunks):
            pieces.append((chunk, i == len(chunks) - 1))

    segments: list[tuple[str, int]] = []
    buf, buf_ends_sentence = "", False
    for piece, ends_sentence in pieces:
        if buf and len(buf) + len(piece) > MAX_SEGMENT_CHARS:
            segments.append((buf, SEGMENT_PAUSE_MS if buf_ends_sentence else 0))
            buf, buf_ends_sentence = piece, ends_sentence
        else:
            buf += piece
            buf_ends_sentence = ends_sentence
    if buf:
        segments.append((buf, 0))

    if segments:
        last_text, _ = segments[-1]
        segments[-1] = (last_text, 0)
    return segments


def _silence_pcm(duration_ms: int) -> bytes:
    frames = int(BACKEND_SAMPLE_RATE * duration_ms / 1000)
    return b"\x00" * (frames * BACKEND_CHANNELS * BACKEND_SAMPLE_WIDTH)


def _backend_failure_detail(status: int) -> str:
    """Describe a backend failure without echoing its URL back to the caller.

    The previous behaviour relayed httpx's str(exc), which embeds the internal
    backend address, and reused the backend's status code -- so a request the
    adapter itself assembled wrongly surfaced as a 4xx blaming the caller, and
    a backend fault surfaced as a 500 that looked like this service crashing.
    A 502 is accurate for both: the upstream this service depends on failed.
    """
    return f"Synthesis backend returned HTTP {status}"



async def _post_segment(client: httpx.AsyncClient, fields: dict,
                        ref_audio: bytes | None) -> bytes:
    """One backend request. Caller owns the backend slot."""
    body, content_type = _build_multipart(fields, ref_audio)
    try:
        resp = await client.post(
            f"{BACKEND_URL}/v1/audio/speech",
            content=body, headers={"Content-Type": content_type},
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502, detail=_backend_failure_detail(e.response.status_code)
        )
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Synthesis backend is unreachable: {type(e).__name__}",
        )
    return resp.content


async def _synthesize(fields: dict, ref_audio: bytes | None,
                      segments: list[tuple[str, int]],
                      timeout: float = 900.0) -> bytes:
    """Synthesise every segment under a single slot acquisition.

    Taking the slot once for the whole request rather than per segment keeps a
    multi-segment synthesis atomic: releasing between segments would let another
    caller interleave, so a long passage could be stretched out arbitrarily and
    the seed's effect would be the only thing holding the timbre together.
    """
    await _BACKEND_SLOT.acquire(BACKEND_QUEUE_TIMEOUT)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            out = bytearray()
            for text, pause_ms in segments:
                out += await _post_segment(client, {**fields, "text": text}, ref_audio)
                if pause_ms:
                    out += _silence_pcm(pause_ms)
            return bytes(out)
    finally:
        _BACKEND_SLOT.release()


async def _stream_backend(fields: dict, ref_audio: bytes | None,
                          segments: list[tuple[str, int]],
                          target_format: str) -> StreamingResponse:
    """Stream segment PCM through, transcoding on the fly when needed.

    The slot is acquired inside the innermost generator rather than before
    returning the response: if the client disconnects between this function
    returning and the body being iterated, a lock acquired out here would never
    be released and the backend would be wedged for the pod's lifetime. Tying
    its lifetime to the generator means every path that can end the generator,
    including being closed without ever yielding, runs the release.
    """

    async def pcm_chunks():
        await _BACKEND_SLOT.acquire(BACKEND_QUEUE_TIMEOUT)
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=900.0, write=30.0, pool=10.0)
        )
        try:
            for text, pause_ms in segments:
                body, content_type = _build_multipart({**fields, "text": text}, ref_audio)
                async with client.stream(
                    "POST", f"{BACKEND_URL}/v1/audio/speech",
                    content=body, headers={"Content-Type": content_type},
                ) as resp:
                    if resp.status_code != 200:
                        raise HTTPException(
                            status_code=502,
                            detail=_backend_failure_detail(resp.status_code),
                        )
                    async for chunk in resp.aiter_bytes(chunk_size=4096):
                        yield chunk
                if pause_ms:
                    yield _silence_pcm(pause_ms)
        finally:
            await client.aclose()
            _BACKEND_SLOT.release()

    if target_format == "pcm":
        source = pcm_chunks()
    elif target_format == "wav":
        async def with_header():
            yield _wav_header(0xFFFFFFFF)
            async for chunk in pcm_chunks():
                yield chunk
        source = with_header()
    else:
        source = _transcode_stream(pcm_chunks(), target_format)

    return StreamingResponse(
        source,
        media_type=CONTENT_TYPES[target_format],
        headers={"X-Breeze-Segments": str(len(segments))},
    )


def _encoder_args(target_format: str) -> list[str]:
    """ffmpeg output args per format. All of these are framed or streamable,
    so they can be produced incrementally from a pipe."""
    if target_format == "mp3":
        return ["-f", "mp3", "-b:a", "192k"]
    if target_format == "opus":
        return ["-f", "ogg", "-c:a", "libopus", "-b:a", "64k", "-ar", "48000"]
    if target_format == "aac":
        return ["-f", "adts", "-c:a", "aac"]
    if target_format == "flac":
        return ["-f", "flac"]
    raise HTTPException(status_code=400, detail=f"Unsupported format: {target_format}")


async def _transcode_stream(pcm_iter, target_format: str):
    """Pipe backend PCM through ffmpeg and yield the encoded bytes as they appear.

    Buffering the whole waveform before encoding would throw away the low
    first-audio latency that is the reason to stream at all, and the previous
    behaviour -- rejecting the combination with a 400 -- broke callers that had
    been streaming mp3 from the endpoint this module took over.
    """
    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "s16le", "-ar", str(BACKEND_SAMPLE_RATE), "-ac", str(BACKEND_CHANNELS),
        "-i", "pipe:0", *_encoder_args(target_format), "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    async def feed():
        try:
            async for chunk in pcm_iter:
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if proc.stdin.can_write_eof():
                try:
                    proc.stdin.write_eof()
                except (OSError, RuntimeError):
                    pass

    feeder = asyncio.create_task(feed())
    try:
        while True:
            out = await proc.stdout.read(4096)
            if not out:
                break
            yield out
    finally:
        # Cancel rather than await: on client disconnect the feeder can be
        # blocked on drain() with nobody draining ffmpeg's output, and awaiting
        # it would hang while still holding the backend slot.
        feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)
        await pcm_iter.aclose()
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


# --- TTS endpoint ---

class SpeechRequest(BaseModel):
    model: str = "tts-1"
    input: str
    voice: str = "alloy"
    response_format: str = Field(default="mp3")
    # Accepted for OpenAI compatibility and ignored: Breeze exposes no rate
    # control, and silently resampling would change the pitch it was asked to
    # preserve. Use voice_description to direct delivery instead.
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    stream: bool = Field(default=False)
    # Carried over from the voxcpm2-tts contract so existing callers keep
    # working; forwarded to Breeze as --cfg-scale.
    cfg_value: float | None = Field(
        default=None, description="Guidance scale. Raises instruction adherence; 4 is "
                                  "upstream's suggestion for voice_description."
    )
    seed: int | None = Field(
        default=None, description="Overrides the voice's stored seed for this request. "
                                  "Reproducible only within one set of backend --fast-* "
                                  "flags."
    )
    voice_description: str | None = Field(
        default=None,
        description="Generate from a description instead of a registered voice. Timbre "
                    "varies between calls; register a voice to get a stable one.",
    )


@app.post("/v1/audio/speech")
async def create_speech(req: SpeechRequest):
    segments = segment_text(req.input)
    if not segments:
        raise HTTPException(status_code=400, detail="input is empty")

    fields: dict = {}
    ref_audio: bytes | None = None

    # Voice Design must be asked for explicitly. Inferring it from an
    # unrecognised voice value means a typo'd or stale voice id answers 200
    # with an arbitrary timbre, and the caller has no way to notice.
    _validate_seed(req.seed)

    if req.voice_description is not None:
        fields["instruction"] = req.voice_description
        seed = req.seed
    else:
        registry = _load_registry()
        if req.voice not in registry:
            raise HTTPException(
                status_code=404,
                detail=f"Voice '{req.voice}' is not registered. List available voices at "
                       "GET /v1/audio/voices, or pass voice_description to generate from a "
                       "description instead.",
            )
        meta = registry[req.voice]
        ref_path = _reference_path(req.voice, registry)
        if not ref_path.exists():
            raise HTTPException(
                status_code=503,
                detail=f"Voice '{req.voice}' is registered but its reference audio is "
                       "unavailable, so the timbre it promises cannot be reproduced.",
            )
        ref_text = meta.get("ref_text")
        if not ref_text:
            raise HTTPException(
                status_code=422,
                detail=f"Voice '{req.voice}' has no ref_text. Breeze clones from a "
                       "reference clip paired with its exact transcript and rejects the "
                       "clip alone. Set it with PUT /v1/audio/voices/"
                       f"{req.voice} (field ref_text).",
            )
        ref_audio = ref_path.read_bytes()
        fields["ref_text"] = ref_text
        stored = _validate_seed(meta.get("seed"))
        seed = req.seed if req.seed is not None else (
            stored if stored is not None else SEED_DEFAULT
        )

    if seed is not None:
        fields["seed"] = seed
    if req.cfg_value is not None:
        fields["cfg_scale"] = req.cfg_value

    if req.stream:
        return await _stream_backend(fields, ref_audio, segments, req.response_format)

    pcm = await _synthesize(fields, ref_audio, segments)
    audio_bytes = _pcm_to_format(pcm, req.response_format)
    content_type = CONTENT_TYPES.get(req.response_format, "application/octet-stream")
    return Response(
        content=audio_bytes, media_type=content_type,
        headers={"X-Breeze-Segments": str(len(segments))},
    )


# --- Clone endpoint (ad-hoc, no registered voice needed) ---

class CloneRequest(BaseModel):
    input: str
    reference_audio: str  # base64 encoded audio
    reference_format: str = "wav"
    # Required rather than optional as it was for voxcpm2-tts: Breeze rejects a
    # reference clip supplied without its transcript.
    prompt_text: str
    response_format: str = "mp3"
    cfg_value: float | None = None
    seed: int | None = None


@app.post("/v1/audio/clone")
async def clone_speech(req: CloneRequest):
    try:
        ref_audio = base64.b64decode(req.reference_audio, validate=True)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="reference_audio is not valid base64")

    _validate_seed(req.seed)
    segments = segment_text(req.input)
    if not segments:
        raise HTTPException(status_code=400, detail="input is empty")
    normalized = _normalize_audio(ref_audio, req.reference_format)
    fields: dict = {"ref_text": req.prompt_text}
    if req.seed is not None:
        fields["seed"] = req.seed
    if req.cfg_value is not None:
        fields["cfg_scale"] = req.cfg_value

    pcm = await _synthesize(fields, normalized, segments)
    audio_bytes = _pcm_to_format(pcm, req.response_format)
    content_type = CONTENT_TYPES.get(req.response_format, "application/octet-stream")
    return Response(
        content=audio_bytes, media_type=content_type,
        headers={"X-Breeze-Segments": str(len(segments))},
    )


# --- Voice Management API ---

@app.get("/v1/audio/voices")
async def list_voices():
    registry = _load_registry()
    voices = []
    for vid, meta in registry.items():
        voices.append({
            "voice_id": vid, "name": meta.get("name", vid),
            "description": meta.get("description", ""),
            "gender": meta.get("gender", "unknown"),
            "type": "builtin" if meta.get("builtin") else "custom",
            "builtin": bool(meta.get("builtin")),
            # Surfaced in the listing because a voice missing it cannot be
            # synthesised at all, which a caller should not have to discover
            # one detail lookup at a time.
            "ready": bool(meta.get("ref_text")),
            "seed": meta.get("seed"),
        })
    return {"voices": voices}


@app.post("/v1/audio/voices")
async def create_voice(
    voice_id: str = Form(...),
    name: str = Form(None),
    description: str = Form(None),
    gender: str = Form("unknown"),
    ref_text: str = Form(...),
    seed: int = Form(SEED_DEFAULT),
    audio: UploadFile = File(...),
):
    registry = _load_registry()
    _validate_voice_id(voice_id)
    _validate_gender(gender)
    _validate_seed(seed)

    # Guard: never let a POST silently overwrite a built-in preset
    _assert_not_builtin(registry, voice_id, "overwriting via registration")

    audio_bytes = await audio.read()
    fmt = audio.filename.rsplit(".", 1)[-1].lower() if audio.filename else "wav"
    normalized = _normalize_audio(audio_bytes, fmt)

    voice_dir = VOICES_DIR / voice_id
    voice_dir.mkdir(parents=True, exist_ok=True)
    (voice_dir / "ref.wav").write_bytes(normalized)

    audio_meta = _probe_audio(voice_dir / "ref.wav")
    registry[voice_id] = {
        "file": "ref.wav",
        "name": name or voice_id,
        "description": description or "",
        "gender": gender,
        "builtin": False,
        "ref_text": ref_text,
        "seed": seed,
        "audio": audio_meta,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_registry(registry)
    # Surfaced on upload rather than only via a later detail lookup: the caller
    # can re-record while they still have the source material at hand.
    return {"voice_id": voice_id, "status": "created", "warnings": _audio_warnings(audio_meta)}


@app.put("/v1/audio/voices/{voice_id}")
async def update_voice(
    voice_id: str,
    name: str = Form(None),
    description: str = Form(None),
    gender: str = Form(None),
    ref_text: str = Form(None),
    seed: int = Form(None),
    audio: UploadFile = File(None),
):
    _validate_voice_id(voice_id)
    registry = _load_registry()
    if voice_id not in registry:
        raise HTTPException(status_code=404, detail=f"Voice '{voice_id}' not found")

    if audio:
        # Built-in presets: metadata is editable, reference audio is not
        _assert_not_builtin(registry, voice_id, "replacing the reference audio")
        if ref_text is None:
            raise HTTPException(
                status_code=400,
                detail="Replacing the reference audio requires ref_text in the same "
                       "request: the stored transcript describes the old recording and "
                       "a mismatched pair degrades the clone.",
            )
        audio_bytes = await audio.read()
        fmt = audio.filename.rsplit(".", 1)[-1].lower() if audio.filename else "wav"
        normalized = _normalize_audio(audio_bytes, fmt)
        voice_dir = VOICES_DIR / voice_id
        voice_dir.mkdir(parents=True, exist_ok=True)
        (voice_dir / "ref.wav").write_bytes(normalized)
        registry[voice_id]["audio"] = _probe_audio(voice_dir / "ref.wav")

    if name is not None:
        registry[voice_id]["name"] = name
    if description is not None:
        registry[voice_id]["description"] = description
    if gender is not None:
        registry[voice_id]["gender"] = _validate_gender(gender)
    if ref_text is not None:
        registry[voice_id]["ref_text"] = ref_text
    if seed is not None:
        registry[voice_id]["seed"] = _validate_seed(seed)
    _save_registry(registry)
    return {"voice_id": voice_id, "status": "updated"}


@app.get("/v1/audio/voices/{voice_id}")
async def get_voice(voice_id: str):
    registry = _load_registry()
    if voice_id not in registry:
        raise HTTPException(status_code=404, detail=f"Voice '{voice_id}' not found")
    meta = registry[voice_id]
    audio = meta.get("audio") or {}
    # Audio metadata is captured at registration time, so a recording lost
    # afterwards would keep being reported as present. Checking here keeps this
    # response consistent with what /preview and synthesis actually do.
    reference_present = _reference_path(voice_id, registry).exists()
    warnings = _registry_warnings(meta)
    if reference_present:
        warnings += _audio_warnings(audio)
    else:
        warnings.append(
            "reference audio is missing; the stored duration and sample rate no "
            "longer describe anything on disk and cloning cannot use this voice"
        )
    return {
        "voice_id": voice_id,
        "name": meta.get("name"),
        "description": meta.get("description"),
        "gender": meta.get("gender", "unknown"),
        "created_at": meta.get("created_at"),
        "type": "builtin" if meta.get("builtin") else "custom",
        "builtin": bool(meta.get("builtin")),
        "ref_text": meta.get("ref_text"),
        "seed": meta.get("seed"),
        "ready": bool(meta.get("ref_text")) and reference_present,
        "reference_audio": "present" if reference_present else "missing",
        "duration_seconds": audio.get("duration_seconds") if reference_present else None,
        "sample_rate": audio.get("sample_rate") if reference_present else None,
        "channels": audio.get("channels") if reference_present else None,
        "codec": audio.get("codec") if reference_present else None,
        "size_bytes": audio.get("size_bytes") if reference_present else None,
        "warnings": warnings,
    }


@app.delete("/v1/audio/voices/{voice_id}")
async def delete_voice(voice_id: str):
    _validate_voice_id(voice_id)
    registry = _load_registry()
    if voice_id not in registry:
        raise HTTPException(status_code=404, detail=f"Voice '{voice_id}' not found")
    _assert_not_builtin(registry, voice_id, "deletion")
    voice_dir = VOICES_DIR / voice_id
    if voice_dir.exists():
        shutil.rmtree(voice_dir)
    del registry[voice_id]
    _save_registry(registry)
    return {"voice_id": voice_id, "status": "deleted"}


@app.get("/v1/audio/voices/{voice_id}/preview")
async def preview_voice(voice_id: str):
    registry = _load_registry()
    if voice_id not in registry:
        raise HTTPException(status_code=404, detail=f"Voice '{voice_id}' not found")
    audio_path = _reference_path(voice_id, registry)
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="Reference audio file missing")
    return Response(content=audio_path.read_bytes(), media_type="audio/wav")


# --- Health ---

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    # breeze_infer exposes no dedicated readiness route; /docs is served only
    # once startup (model load plus CUDA graph capture) has finished, which is
    # exactly the condition this probe needs.
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(f"{BACKEND_URL}/docs")
            if resp.status_code == 200:
                return {"status": "ready"}
        except httpx.RequestError:
            pass
    raise HTTPException(status_code=503, detail="Backend not ready")
