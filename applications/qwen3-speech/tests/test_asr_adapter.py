"""Tests for the ASR adapter's failure handling and event-loop hygiene.

The backend is never contacted: either it is pointed at a closed port, or the
functions that would reach it are stubbed. Heavy runtime dependencies of the
VAD path (torch, silero_vad) are faked so the suite runs without a GPU image.

Run from the repo root, with versions pinned to the ASR image so that
version-specific behaviour (e.g. websockets 16's lazy submodules) shows up here.
--timeout matters: a WebSocket test that waits for an event the adapter never
sends otherwise blocks forever instead of failing.
    uv run -p 3.12 --with fastapi==0.136.3 --with starlette==1.3.1 \
        --with httpx==0.28.1 --with websockets==16.0 \
        --with python-multipart==0.0.32 --with numpy==2.2.6 \
        --with soundfile==0.14.0 --with pytest --with pytest-timeout \
        pytest applications/qwen3-speech/tests -q --timeout=30
"""

import asyncio
import importlib.util
import io
import json
import sys
import threading
import time
import types
import wave
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

ADAPTER_PATH = Path(__file__).resolve().parents[1] / "base" / "asr" / "asr-adapter.py"
# Port 1 is reserved and nothing listens on it, so connects fail immediately.
DEAD_HTTP = "http://127.0.0.1:1"
DEAD_WS = "ws://127.0.0.1:1"


def _wav_bytes(seconds: float = 1.0, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


@pytest.fixture
def adapter():
    spec = importlib.util.spec_from_file_location("asr_adapter", ADAPTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.BACKEND_URL = DEAD_HTTP
    module.BACKEND_WS = DEAD_WS
    return module


def test_ready_is_503_while_backend_down_and_health_stays_up(adapter):
    """Readiness tracks vLLM; liveness must not, or a reload restarts the pod."""
    client = TestClient(adapter.app)
    assert client.get("/ready").status_code == 503
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("raw, text, language", [
    # Captured 2026-10-01 from the realtime path: the second sub-sentence
    # marker follows a full stop with no newline, and leaked into the
    # completed transcript.
    ("language Chinese<asr_text>今天天气不错，我打算下去玩。妈妈，现在。"
     "language Chinese<asr_text>现在几点了？",
     "今天天气不错，我打算下去玩。妈妈，现在。\n现在几点了？", "chinese"),
    ("language English<asr_text>Hello.\nlanguage English<asr_text>World.",
     "Hello.\nWorld.", "english"),
    # Silence: the backend names no language.
    ("language None<asr_text>", "", None),
    ("", "", None),
])
def test_parse_asr_text_strips_every_marker(adapter, raw, text, language):
    assert adapter._parse_asr_text(raw) == (text, language)


class _FakeRealtimeBackend:
    """Minimal vLLM realtime stand-in: one transcription.done per final commit."""

    def __init__(self, done_text: str):
        import websockets
        self._done_text = done_text
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()

        async def handler(ws):
            await ws.send(json.dumps({"type": "session.created", "id": "sess-test"}))
            async for frame in ws:
                if json.loads(frame).get("final"):
                    await ws.send(json.dumps({"type": "transcription.done",
                                              "text": self._done_text}))

        async def start():
            self._server = await websockets.serve(handler, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]
            ready.set()

        self._thread = threading.Thread(
            target=lambda: (self._loop.run_until_complete(start()),
                            self._loop.run_forever()), daemon=True)
        self._thread.start()
        ready.wait(5)

    def close(self):
        self._loop.call_soon_threadsafe(self._server.close)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)


@pytest.mark.parametrize("done_text, transcript, language", [
    ("language None<asr_text>", "", None),
    ("language Chinese<asr_text>你好。language Chinese<asr_text>再见。",
     "你好。\n再见。", "chinese"),
])
def test_realtime_sends_exactly_one_completed_per_final_commit(
        adapter, done_text, transcript, language):
    """Empty segments still close out, so the client never waits on silence."""
    backend = _FakeRealtimeBackend(done_text)
    adapter.BACKEND_WS = f"ws://127.0.0.1:{backend.port}"
    try:
        with TestClient(adapter.app).websocket_connect("/v1/realtime") as ws:
            assert json.loads(ws.receive_text())["type"] == "session.created"
            ws.send_text(json.dumps({"type": "input_audio_buffer.commit"}))
            ws.send_text(json.dumps({"type": "input_audio_buffer.commit",
                                     "final": True}))
            event = json.loads(ws.receive_text())
    finally:
        backend.close()
    assert event == {"type": "conversation.item.input_audio_transcription.completed",
                     "transcript": transcript, "language": language}


def test_backend_down_returns_503_with_retry_after(adapter):
    client = TestClient(adapter.app)
    resp = client.post("/v1/audio/transcriptions",
                       files={"file": ("a.wav", _wav_bytes(), "audio/wav")})
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == str(adapter.BACKEND_RETRY_AFTER_SECONDS)
    assert resp.json()["error"]["type"] == "backend_unavailable"


def test_realtime_backend_down_sends_error_and_closes_1013(adapter):
    client = TestClient(adapter.app)
    with client.websocket_connect("/v1/realtime") as ws:
        event = json.loads(ws.receive_text())
        assert event["type"] == "error"
        assert event["error"]["type"] == "backend_unavailable"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        assert closed.value.code == 1013


def test_vad_does_not_block_health(adapter, monkeypatch):
    """A slow VAD pass must not stall other requests on the same loop."""
    vad_seconds = 1.5

    def slow_gaps(_path):
        time.sleep(vad_seconds)  # stands in for ffmpeg decode + Silero
        return []

    async def fake_transcribe(_client, _audio, _filename):
        return "ok", {"language": "chinese", "usage": None}

    monkeypatch.setattr(adapter, "_audio_duration", lambda _p: 200.0)
    monkeypatch.setattr(adapter, "_speech_gaps", slow_gaps)
    monkeypatch.setattr(adapter, "_extract_segment", lambda *_a: b"")
    monkeypatch.setattr(adapter, "_transcribe_once", fake_transcribe)

    async def scenario():
        transport = httpx.ASGITransport(app=adapter.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # Timed from before the upload starts: if the VAD pass blocks the
            # loop, even the sleep below cannot return until it finishes, so a
            # clock started after the sleep would never see the stall.
            started = time.monotonic()
            upload = asyncio.create_task(c.post(
                "/v1/audio/transcriptions",
                files={"file": ("a.wav", _wav_bytes(), "audio/wav")}))
            await asyncio.sleep(0.3)  # let the upload reach the VAD stage
            health = await c.get("/health")
            health_done = time.monotonic() - started
            result = await upload
        return health, health_done, result

    health, health_done, result = asyncio.run(scenario())
    assert health.status_code == 200
    assert health_done < vad_seconds, (
        f"/health answered {health_done:.2f}s in, behind the {vad_seconds}s VAD pass")
    assert result.status_code == 200
    assert result.json()["segments"] == 2  # 200s at a 120s budget


def test_vad_inference_is_serialised(adapter, monkeypatch, tmp_path):
    """Silero is stateful; concurrent recordings must not interleave inference."""
    active, peak = 0, 0
    guard = threading.Lock()

    def fake_timestamps(*_args, **_kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.2)
        with guard:
            active -= 1
        return [{"start": 0.0, "end": 1.0}, {"start": 2.0, "end": 3.0}]

    monkeypatch.setitem(sys.modules, "torch",
                        types.SimpleNamespace(from_numpy=lambda a: a))
    monkeypatch.setitem(sys.modules, "silero_vad", types.SimpleNamespace(
        get_speech_timestamps=fake_timestamps, load_silero_vad=lambda: object()))

    source = tmp_path / "a.wav"
    source.write_bytes(_wav_bytes())
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        adapter._speech_gaps(str(source)))) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert peak == 1, f"{peak} VAD inferences ran at once"
    assert results == [[1.5]] * 3  # midpoint of the 1.0s-2.0s gap
