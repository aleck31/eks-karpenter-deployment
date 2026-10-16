"""Tests for the Breeze TTS adapter's OpenAI API surface.

The breeze_infer backend is replaced by an httpx MockTransport that returns PCM
and records the multipart fields it was sent, so these run without a GPU.

    uv run -p 3.12 --with fastapi --with httpx --with python-multipart --with pydub \
        --with pysbd --with pytest pytest applications/breeze2-tts/tests -q
(ffmpeg must be on PATH.)
"""

import base64
import importlib.util
import io
import json
import struct
import wave
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

ADAPTER = Path(__file__).resolve().parents[1] / "base" / "openai-adapter.py"
PCM = struct.pack("<2400h", *([1000, -1000] * 1200))  # 0.1 s at 24 kHz


def wav16k(seconds=1.0):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack(f"<{int(seconds * 16000)}h",
                                  *([3000, -3000] * int(seconds * 8000))))
    return buf.getvalue()


@pytest.fixture
def app(tmp_path, monkeypatch):
    voices = tmp_path / "voices"
    (voices / "alloy").mkdir(parents=True)
    (voices / "alloy" / "ref.wav").write_bytes(wav16k())
    (voices / "registry.json").write_text(json.dumps({"alloy": {
        "file": "ref.wav", "name": "Alloy", "builtin": True, "ref_text": "你好",
        "seed": 42, "created_at": "2026-09-15T03:42:39+00:00",
        "updated_at": "2026-09-15T03:42:39+00:00"}}))
    (voices / ".schema").write_text("3")
    monkeypatch.setenv("VOICES_DIR", str(voices))

    spec = importlib.util.spec_from_file_location("adapter", ADAPTER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.VOICES_DIR = voices
    sent = []

    def handler(request: httpx.Request):
        if request.url.path == "/v1/audio/speech":
            sent.append(request.content)
            return httpx.Response(200, content=PCM)
        if request.url.path == "/v1/audio/transcriptions":
            return httpx.Response(200, json={"text": "自动转写的文本"})
        return httpx.Response(200)

    real = httpx.AsyncClient

    def client(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(m.httpx, "AsyncClient", client)
    m.sent = sent
    return m


def fields(body: bytes) -> dict:
    """Plain form fields from a multipart body sent to the backend."""
    out = {}
    for part in body.split(b"--")[1:]:
        if b'name="' not in part or b"filename=" in part:
            continue
        name = part.split(b'name="')[1].split(b'"')[0].decode()
        out[name] = part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0].decode()
    return out


def post(c, **body):
    return c.post("/v1/audio/speech", json={"model": "tts-1", "input": "测试。", **body})


def test_speech_voice_string_and_object(app):
    with TestClient(app.app) as c:
        for voice in ("alloy", {"id": "alloy"}):
            r = post(c, voice=voice, response_format="pcm")
            assert r.status_code == 200, r.text
            assert r.content == PCM
    assert fields(app.sent[0])["ref_text"] == "你好"


def test_instructions_direct_a_cloned_voice(app):
    with TestClient(app.app) as c:
        assert post(c, voice="alloy", instructions="Speak slowly.").status_code == 200
    f = fields(app.sent[-1])
    assert f["instruction"] == "Speak slowly." and f["ref_text"] == "你好"


def test_instructions_without_voice_designs_one(app):
    with TestClient(app.app) as c:
        assert post(c, instructions="A warm low voice.", response_format="wav").status_code == 200
        r = post(c)
    f = fields(app.sent[-1])
    assert f["instruction"] == "A warm low voice." and "ref_text" not in f
    assert r.status_code == 400 and r.json()["error"]["param"] == "voice"


def test_stream_format_audio_and_sse(app):
    with TestClient(app.app) as c:
        raw = post(c, voice="alloy", response_format="pcm", stream_format="audio")
        sse = post(c, voice="alloy", response_format="pcm", stream_format="sse")
    assert raw.status_code == 200 and raw.content == PCM
    assert sse.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(l[6:]) for l in sse.text.splitlines() if l.startswith("data: ")]
    assert [e["type"] for e in events][-1] == "speech.audio.done"
    assert set(events[-1]["usage"]) == {"input_tokens", "output_tokens", "total_tokens"}
    audio = b"".join(base64.b64decode(e["audio"]) for e in events if e["type"] == "speech.audio.delta")
    assert audio == PCM


def test_errors_are_openai_shape(app):
    with TestClient(app.app) as c:
        cases = {
            "voice": post(c, voice="nope"),
            "response_format": post(c, voice="alloy", response_format="ogg"),
            "speed": post(c, voice="alloy", speed=1.5),
            "model": c.post("/v1/audio/speech", json={"input": "x", "voice": "alloy"}),
        }
    for param, r in cases.items():
        assert r.status_code == 400, (param, r.text)
        err = r.json()["error"]
        assert set(err) == {"message", "type", "param", "code"}
        assert err["type"] == "invalid_request_error" and err["param"] == param


def test_create_voice_openai_shape_and_auto_transcript(app, monkeypatch):
    with TestClient(app.app) as c:
        r = c.post("/v1/audio/voices", data={"name": "Mine", "consent": "cons_1"},
                   files={"audio_sample": ("s.wav", wav16k(), "audio/wav")})
        assert r.status_code == 400 and r.json()["error"]["param"] == "ref_text"
        monkeypatch.setattr(app, "ASR_TRANSCRIBE_URL", "http://asr/v1/audio/transcriptions")
        r = c.post("/v1/audio/voices", data={"name": "Mine"},
                   files={"audio_sample": ("s.wav", wav16k(), "audio/wav")})
        assert r.status_code == 200, r.text
        v = r.json()
        assert v["object"] == "audio.voice" and v["type"] == "audio_sample"
        assert v["id"].startswith("voice_") and v["name"] == "Mine"
        assert isinstance(v["created_at"], int) and v["ref_text"] == "自动转写的文本"
        # The new voice is usable by id at once.
        assert post(c, voice={"id": v["id"]}).status_code == 200
        listing = c.get("/v1/audio/voices").json()
        assert listing["object"] == "list" and {x["id"] for x in listing["data"]} == {"alloy", v["id"]}
        d = c.delete(f"/v1/audio/voices/{v['id']}").json()
        assert d == {"id": v["id"], "object": "audio.voice", "deleted": True}
