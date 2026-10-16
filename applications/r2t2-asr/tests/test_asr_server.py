"""Tests for the R2T2 ASR server's serving layer.

The R2T2 model and FireRed VAD are replaced by deterministic fakes: these tests
cover segmentation, the event protocol, batching and failure handling, not
recognition quality (that is measured on FLEURS, see deployment-guide.md).

Run inside the image (dependency versions then match production):
    docker run --rm -v $PWD/applications/r2t2-asr:/src -w /src --entrypoint bash <image> \
        -c "pip install -q pytest==8.4.2 && python3 -m pytest tests -q"
or locally:
    uv run -p 3.12 --with fastapi --with httpx --with python-multipart --with numpy \
        --with soundfile --with zhconv==1.4.3 --with pytest pytest applications/r2t2-asr/tests -q
"""

import base64
import importlib.util
import io
import json
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

SERVER = Path(__file__).resolve().parents[1] / "base" / "asr-server.py"
SR = 16000
SCRIPT = "今天天气不错我们去公园散步吧"


# --- fakes ---

class FakeState:
    def __init__(self, language):
        self.language = language or ""
        self.chunk_size_sec = 0.16
        self.chunk_size_samples = 2560
        self.text = ""
        self.buffered = 0
        self.buffered_loud = False


class FakeEngine:
    """Each chunk containing sound adds one character; the last one stays unfixed.

    Characters are counted per stream, so concurrent segments each read SCRIPT
    from the start regardless of how threads interleave.
    """

    def __init__(self):
        self.steps = 0

    def new_state(self, language, chunk_s, context=""):
        self.last_context = context
        return FakeState(language)

    def step(self, pcm, state, max_new_tokens):
        self.steps += 1
        if len(pcm) < state.chunk_size_samples:  # R2T2 buffers partial chunks
            state.buffered += len(pcm)
            state.buffered_loud = state.buffered_loud or bool(np.abs(pcm).max() > 0)
            return ""
        if np.abs(pcm).max() > 0:
            state.text += SCRIPT[len(state.text) % len(SCRIPT)]
        return state.text[:-1]

    def finish(self, state, max_new_tokens):
        # Like R2T2, finish() decodes audio still buffered in a partial chunk.
        if state.buffered_loud:
            state.text += SCRIPT[len(state.text) % len(SCRIPT)]
        state.buffered, state.buffered_loud = 0, False
        return state.text

    @staticmethod
    def language(state):
        return "chinese" if state.text else None


class _R:
    def __init__(self, start=False, end=False, frame=1):
        self.is_speech_start, self.is_speech_end = start, end
        self.speech_start_frame = self.speech_end_frame = frame


class FakeVadStream:
    """Speech start on the first loud chunk, speech end on the first silent one after."""

    def __init__(self):
        self.in_speech = False

    def detect_chunk(self, pcm_int16):
        loud = np.abs(pcm_int16).max() > 0
        if loud and not self.in_speech:
            self.in_speech = True
            return [_R(start=True)]
        if not loud and self.in_speech:
            self.in_speech = False
            return [_R(end=True)]
        return []

    def reset(self):
        self.in_speech = False


class FakeVad:
    def __init__(self, spans=None):
        self.spans = spans

    def stream(self, **overrides):
        self.overrides = overrides
        return FakeVadStream()

    def speech_spans(self, audio):
        if self.spans is not None:
            return self.spans
        return [(0.0, len(audio) / SR)]


def tone(seconds):
    t = np.arange(int(seconds * SR)) / SR
    return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def silence(seconds):
    return np.zeros(int(seconds * SR), dtype=np.float32)


def wav_bytes(audio):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((audio * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


@pytest.fixture
def server():
    spec = importlib.util.spec_from_file_location("asr_server", SERVER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    def fake_load():
        m._engine = FakeEngine()
        m._vad = FakeVad()

    m._load_runtime = fake_load
    return m


def kinds(events):
    return [e.kind for e in events]


# --- Session ---

def test_client_commit_yields_append_only_deltas_and_one_completed(server):
    s = server.Session(FakeEngine(), FakeVad(), None, 160)
    events = s.feed(tone(1.0)) + s.finalize()
    deltas = [e.text for e in events if e.kind == "delta"]
    completed = [e for e in events if e.kind == "completed"]
    assert len(completed) == 1
    assert completed[0].text == "".join(deltas)  # nothing ever rewritten
    assert completed[0].text and SCRIPT.startswith(completed[0].text)
    assert completed[0].language == "chinese"


def test_final_on_empty_segment_still_completes(server):
    s = server.Session(FakeEngine(), FakeVad(), None, 160)
    events = s.finalize()
    assert kinds(events) == ["completed"]
    assert events[0].text == ""


def test_server_vad_closes_segment_at_pause(server):
    s = server.Session(FakeEngine(), FakeVad(), None, 160, server_vad=True)
    events = s.feed(np.concatenate([tone(1.0), silence(0.5), tone(1.0), silence(0.5)]))
    done = [e.text for e in events if e.kind == "completed"]
    assert len(done) == 2
    assert all(done)
    assert kinds(events).count("speech_started") == 2


class FrameVadStream:
    """FireRed-like: 10 ms frames, end reported min_silence frames into a silence."""

    def __init__(self, min_silence_frame=20):
        self.min_silence, self.frame, self.silent, self.in_speech = min_silence_frame, 0, 0, False

    def detect_chunk(self, pcm_int16):
        out = []
        for i in range(0, len(pcm_int16) - 159, 160):
            self.frame += 1
            loud = np.abs(pcm_int16[i:i + 160]).max() > 0
            if loud:
                self.silent = 0
                if not self.in_speech:
                    self.in_speech = True
                    out.append(_R(start=True, frame=self.frame))
            elif self.in_speech:
                self.silent += 1
                if self.silent >= self.min_silence:
                    self.in_speech = False
                    out.append(_R(end=True, frame=self.frame))
        return out

    def reset(self):
        self.__init__(self.min_silence)


class FrameVad(FakeVad):
    def stream(self, **overrides):
        self.overrides = overrides
        return FrameVadStream(overrides.get("min_silence_frame", 20))


class LoudCountEngine(FakeEngine):
    """Records how many loud samples each segment was given."""

    def __init__(self):
        super().__init__()
        self.loud = []

    def new_state(self, language, chunk_s, context=""):
        self.loud.append(0)
        return super().new_state(language, chunk_s, context)

    def step(self, pcm, state, max_new_tokens):
        self.loud[-1] += int((np.abs(pcm) > 0).sum())
        return super().step(pcm, state, max_new_tokens)


def test_server_vad_cut_keeps_next_turn_onset(server):
    # The pause is detected 200 ms into the gap, mid-chunk, and the next phrase
    # starts in that same chunk. Its onset must go to the next turn, not be
    # decoded into the closing one and then lost.
    eng = LoudCountEngine()
    s = server.Session(eng, FrameVad(), None, 480, server_vad=True, vad_overrides={"min_silence_frame": 20})
    first, second = tone(0.83), tone(1.2)
    events = s.feed(np.concatenate([first, silence(0.25), second, silence(0.6)]))
    assert kinds(events).count("completed") == 2
    assert eng.loud[0] == int((np.abs(first) > 0).sum())
    assert eng.loud[1] == int((np.abs(second) > 0).sum())


def test_vad_timestamps_are_client_audio_positions_across_commits(server):
    s = server.Session(FakeEngine(), FrameVad(), None, 480, server_vad=True,
                       vad_overrides={"min_silence_frame": 20})
    s.feed(tone(1.0))
    s.finalize()  # its 0.5 s of padding is not client audio
    events = s.feed(np.concatenate([silence(1.0), tone(1.0), silence(1.0)]))
    started = [e.ms for e in events if e.kind == "speech_started"]
    stopped = [e.ms for e in events if e.kind == "speech_stopped"]
    assert len(started) == 1 and abs(started[0] - 2000) <= 10
    # Where speech ended, not where the 200 ms of silence confirmed it.
    # The committed turn's end is confirmed only after the commit; its
    # position is still the client's 1.0 s, not shifted by the padding.
    assert len(stopped) == 2 and all(abs(a - b) <= 10 for a, b in zip(stopped, (1000, 3000)))


def test_short_pause_does_not_end_turn_at_default_silence(server):
    s = server.Session(FakeEngine(), FrameVad(), None, 480, server_vad=True,
                       vad_overrides={"min_silence_frame": server.VAD_SILENCE_MS // 10})
    events = s.feed(np.concatenate([tone(0.6), silence(0.33), tone(1.0), silence(0.45),
                                    tone(0.6), silence(0.3), tone(0.8), silence(1.5)]))
    assert kinds(events).count("completed") == 1  # comma pauses stay inside the turn


def test_finalize_padding_never_adds_a_second_completed(server):
    s = server.Session(FakeEngine(), FakeVad(), None, 160, server_vad=True)
    events = s.feed(tone(0.6)) + s.finalize()  # padding would read as a pause
    assert kinds(events).count("completed") == 1


def test_without_vad_long_segment_is_force_split(server):
    s = server.Session(FakeEngine(), FakeVad(), None, 160,
                       stream_policy=server.SegmentPolicy(100.0, 2.0))
    events = s.feed(tone(5.0))
    assert kinds(events).count("completed") >= 2


def test_stream_cuts_at_first_pause_past_threshold(server):
    # Pauses after 1 s and 2.5 s of audio; threshold 2 s -> only the second cuts.
    p = server.SegmentPolicy(2.0, 30.0)
    s = server.Session(FakeEngine(), FakeVad(), None, 160, stream_policy=p)
    audio = np.concatenate([tone(1.0), silence(0.5), tone(1.0), silence(0.5), tone(1.0)])
    events = s.feed(audio)
    assert kinds(events).count("completed") == 1
    assert len(s.finalize()[-1].text) > 0  # third tone is its own segment


def test_rolling_peak_forgets_loud_past(server):
    peak = server.RecentPeak()
    peak.update(np.full(SR, 0.9, dtype=np.float32))
    assert peak.update(np.full(SR, 0.001, dtype=np.float32)) == pytest.approx(0.9)
    for _ in range(3):
        last = peak.update(np.full(SR, 0.001, dtype=np.float32))
    assert last == pytest.approx(0.001)  # gain can rise for a quiet speaker


def test_clear_drops_segment(server):
    s = server.Session(FakeEngine(), FakeVad(), None, 160)
    s.feed(tone(1.0))
    assert s.clear() == []
    assert s.finalize()[-1].text == ""


def test_partial_tail_is_decoded_by_finish(server):
    eng = FakeEngine()
    s = server.Session(eng, None, None, 480)
    s.feed(tone(0.7))  # one 640 ms first chunk, 60 ms left over
    s.finalize(pad_s=0.0)
    assert eng.steps == 2  # the leftover went to step() before finish()


# --- helpers ---

def test_hallucination_detection(server):
    assert server.detect_hallucination("好的。" * 6)
    assert server.detect_hallucination("Okay. Okay. Okay. Okay. Okay. Okay.")
    assert not server.detect_hallucination("今天天气不错，我们去公园散步吧。")


def test_join_texts(server):
    assert server.join_texts(["你好。", "再见。"]) == "你好。再见。"
    assert server.join_texts(["Hello.", "World."]) == "Hello. World."
    assert server.join_texts(["我用AWS", "部署"]) == "我用AWS部署"
    assert server.join_texts(["", " ok ", ""]) == "ok"


def test_segment_bounds_are_contiguous_and_cut_at_pauses(server):
    p = server.SegmentPolicy(5.0, 10.0)
    spans = [(0.5, 3.0), (3.4, 7.0), (7.4, 12.0), (12.6, 25.0)]  # pauses 3.2, 7.2, 12.3
    bounds = server.segment_bounds(25.0, spans, p)
    secs = [(a / SR, b / SR) for a, b in bounds]
    assert bounds[0][0] == 0 and bounds[-1][1] == 25 * SR
    assert all(b1 == a2 for (_, b1), (a2, _) in zip(bounds, bounds[1:]))  # no audio dropped
    assert secs[0][1] == pytest.approx(7.2)  # 3.2 is under the 5 s threshold
    assert secs[1][1] == pytest.approx(12.3)  # first pause 5 s after the last cut
    assert all(b - a <= 10.0 + 1e-6 for a, b in secs)  # hard cap
    # No pause anywhere: falls back to the clock, still bounded.
    assert [(b - a) / SR for a, b in server.segment_bounds(25.0, [(0.0, 25.0)], p)] == [10.0, 10.0, 5.0]
    # VAD found nothing at all: the audio is still transcribed.
    assert server.segment_bounds(4.0, [], p) == [(0, 4 * SR)]
    # Threshold 0 cuts at every pause.
    assert len(server.segment_bounds(25.0, spans, server.SegmentPolicy(0.0, 30.0))) == 4


def test_local_gain_lifts_quiet_window_next_to_loud_one(server):
    loud = np.full(3 * SR, 0.9, dtype=np.float32)
    quiet = np.full(3 * SR, 10 ** (-55 / 20), dtype=np.float32)
    out = server.local_gain(np.concatenate([loud, quiet]))
    assert np.abs(out[3 * SR:]).max() == pytest.approx(10 ** (server.VAD_TARGET_PEAK_DBFS / 20), rel=1e-3)
    assert np.abs(out[:3 * SR]).max() == pytest.approx(0.9)


def test_vad_gain_only_boosts_and_is_capped(server):
    assert server.vad_gain(1.0) == 1.0  # never attenuates
    assert server.vad_gain(0.0) == 1.0
    quiet = 10 ** (-47 / 20)  # FLEURS English that VAD missed
    assert abs(20 * np.log10(quiet * server.vad_gain(quiet)) - server.VAD_TARGET_PEAK_DBFS) < 0.01
    assert server.vad_gain(1e-9) == 10 ** (server.VAD_MAX_GAIN_DB / 20)


def test_to_simplified(server):
    pytest.importorskip("zhconv")
    assert server.to_simplified("執政黨在議會選舉中") == "执政党在议会选举中"


# --- Batcher ---

def test_batcher_merges_concurrent_calls_and_keeps_order(server):
    calls = []

    class LLM:
        def generate(self, prompts, sampling_params=None, use_tqdm=False):
            calls.append(len(prompts))
            time.sleep(0.05)
            return [f"out:{p}" for p in prompts]

    b = server.Batcher(LLM())
    results = [None] * 8

    def w(i):
        results[i] = b.generate([i])

    threads = [threading.Thread(target=w, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [[f"out:{i}"] for i in range(8)]
    assert sum(calls) == 8 and max(calls) > 1


def test_engine_failure_marks_dead_and_fails_health(server):
    class Broken:
        def generate(self, *a, **k):
            raise RuntimeError("EngineCore died")

    b = server.Batcher(Broken())
    with pytest.raises(server.EngineDead):
        b.generate([1])
    with pytest.raises(server.EngineDead):  # later calls fail fast
        b.generate([2])
    server._batcher = b
    with TestClient(server.app) as c:
        assert c.get("/health").status_code == 503
        assert c.get("/ready").status_code == 503
        r = c.post("/v1/audio/transcriptions", files={"file": ("a.wav", wav_bytes(tone(1)), "audio/wav")})
        assert r.status_code == 503 and r.headers["retry-after"] == "30"


# --- HTTP: /v1/audio/transcriptions ---

def _post(c, audio, **data):
    return c.post("/v1/audio/transcriptions", data=data,
                  files={"file": ("a.wav", wav_bytes(audio), "audio/wav")})


def test_http_json_is_openai_shape(server, monkeypatch):
    monkeypatch.setattr(server, "FILE_POLICY", server.SegmentPolicy(1.0, 30.0))
    with TestClient(server.app) as c:
        server._vad = FakeVad(spans=[(0.0, 1.0), (2.0, 3.0)])
        r = _post(c, np.concatenate([tone(1.0), silence(1.0), tone(1.0)]), language="zh")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"text", "usage"}
    assert body["usage"] == {"type": "duration", "seconds": 3}
    assert body["text"].count(SCRIPT[0]) == 2  # two segments, joined in order


def test_http_verbose_json_srt_vtt_text(server, monkeypatch):
    monkeypatch.setattr(server, "FILE_POLICY", server.SegmentPolicy(1.0, 30.0))
    audio = np.concatenate([tone(1.0), silence(1.0), tone(1.0)])
    with TestClient(server.app) as c:
        server._vad = FakeVad(spans=[(0.0, 1.0), (2.0, 3.0)])
        v = _post(c, audio, response_format="verbose_json").json()
        srt = _post(c, audio, response_format="srt").text
        vtt = _post(c, audio, response_format="vtt").text
        txt = _post(c, audio, response_format="text")
    assert v["task"] == "transcribe" and v["duration"] == 3.0
    assert [(s["start"], s["end"]) for s in v["segments"]] == [(0.0, 1.5), (1.5, 3.0)]
    assert srt.startswith("1\n00:00:00,000 --> 00:00:01,500\n")
    assert vtt.startswith("WEBVTT\n\n00:00:00.000 --> 00:00:01.500\n")
    assert txt.headers["content-type"].startswith("text/plain") and txt.text == v["text"]


def test_http_stream_sse(server):
    with TestClient(server.app) as c:
        r = _post(c, tone(1.0), stream="true")
    events = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
    assert [e["type"] for e in events][-1] == "transcript.text.done"
    assert "".join(e["delta"] for e in events if e["type"] == "transcript.text.delta") == events[-1]["text"]


def test_http_prompt_reaches_model(server):
    with TestClient(server.app) as c:
        _post(c, tone(1.0), prompt="会议纪要")
        assert server._engine.last_context == "会议纪要"


def test_http_errors_are_openai_shape(server):
    with TestClient(server.app) as c:
        bad = c.post("/v1/audio/transcriptions", files={"file": ("a.wav", b"nope", "audio/wav")})
        fmt = _post(c, tone(1.0), response_format="xml")
        server._vad = FakeVad(spans=[])
        missed = _post(c, tone(1.0))
    assert bad.status_code == 400 and set(bad.json()["error"]) == {"message", "type", "param", "code"}
    assert fmt.status_code == 400 and fmt.json()["error"]["param"] == "response_format"
    assert missed.status_code == 200 and missed.json()["text"]  # VAD miss loses nothing


# --- WebSocket: GA transcription session ---

def _append(ws, audio, rate=SR):
    if rate != SR:
        n = int(len(audio) * rate / SR)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
    pcm = (audio * 32767).astype(np.int16).tobytes()
    for i in range(0, len(pcm), 6400):
        ws.send_text(json.dumps({"type": "input_audio_buffer.append",
                                 "audio": base64.b64encode(pcm[i:i + 6400]).decode()}))


def _recv_until(ws, kind):
    events = []
    while True:
        e = json.loads(ws.receive_text())
        events.append(e)
        if e["type"] == kind or e["type"] == "error":
            return events


def _session(ws, **audio_input):
    ws.send_text(json.dumps({"type": "session.update", "session": {
        "type": "transcription", "audio": {"input": audio_input}}}))
    return _recv_until(ws, "session.updated")[-1]


def test_realtime_manual_commit_flow(server):
    with TestClient(server.app) as c, c.websocket_connect("/v1/realtime") as ws:
        created = json.loads(ws.receive_text())
        assert created["type"] == "session.created"
        assert created["session"]["type"] == "transcription"
        assert created["session"]["audio"]["input"]["turn_detection"] == {
            "type": "server_vad", "silence_duration_ms": 500, "prefix_padding_ms": 300}
        upd = _session(ws, format={"type": "audio/pcm", "rate": 16000}, turn_detection=None,
                       transcription={"model": "gpt-4o-transcribe", "language": "zh"})
        assert upd["session"]["audio"]["input"]["turn_detection"] is None
        assert upd["session"]["audio"]["input"]["transcription"]["language"] == "zh"
        _append(ws, tone(1.0))
        ws.send_text(json.dumps({"type": "input_audio_buffer.commit", "event_id": "c1"}))
        events = _recv_until(ws, "conversation.item.input_audio_transcription.completed")
    committed = next(e for e in events if e["type"] == "input_audio_buffer.committed")
    deltas = [e for e in events if e["type"].endswith("transcription.delta")]
    done = events[-1]
    assert all("event_id" in e for e in events)
    assert {e["item_id"] for e in deltas} | {done["item_id"]} == {committed["item_id"]}
    assert done["content_index"] == 0 and done["transcript"] == "".join(e["delta"] for e in deltas)
    assert done["languages"] == [{"code": "zh"}]


def test_realtime_two_turns_link_previous_item(server):
    with TestClient(server.app) as c, c.websocket_connect("/v1/realtime") as ws:
        ws.receive_text()
        _session(ws, format={"type": "audio/pcm", "rate": 16000}, turn_detection=None)
        ids = []
        for _ in range(2):
            _append(ws, tone(1.0))
            ws.send_text(json.dumps({"type": "input_audio_buffer.commit"}))
            ev = _recv_until(ws, "conversation.item.input_audio_transcription.completed")
            ids.append(next(e for e in ev if e["type"] == "input_audio_buffer.committed"))
    assert ids[0]["previous_item_id"] is None
    assert ids[1]["previous_item_id"] == ids[0]["item_id"] != ids[1]["item_id"]


def test_realtime_server_vad_default_and_24k_input(server):
    with TestClient(server.app) as c, c.websocket_connect("/v1/realtime") as ws:
        ws.receive_text()  # defaults: server_vad, audio/pcm 24 kHz
        _append(ws, np.concatenate([tone(1.0), silence(1.0)]), rate=24000)
        events = _recv_until(ws, "conversation.item.input_audio_transcription.completed")
    kinds_ = [e["type"] for e in events]
    assert kinds_.index("input_audio_buffer.speech_started") < kinds_.index("input_audio_buffer.speech_stopped") \
        < kinds_.index("input_audio_buffer.committed")
    assert events[-1]["transcript"]
    assert server._vad.overrides == {"min_silence_frame": 50}  # OpenAI's 500 ms default


def test_realtime_vad_params_and_prompt_reach_session(server):
    with TestClient(server.app) as c, c.websocket_connect("/v1/realtime") as ws:
        ws.receive_text()
        _session(ws, turn_detection={"type": "server_vad", "threshold": 0.6, "silence_duration_ms": 1300},
                 transcription={"prompt": "AWS Karpenter"})
        _append(ws, np.concatenate([tone(1.0), silence(1.0)]), rate=24000)
        _recv_until(ws, "conversation.item.input_audio_transcription.completed")
    assert server._vad.overrides == {"speech_threshold": 0.6, "min_silence_frame": 130}
    assert server._engine.last_context == "AWS Karpenter"


def test_realtime_errors(server):
    with TestClient(server.app) as c, c.websocket_connect("/v1/realtime") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "input_audio_buffer.commit", "event_id": "c9"}))
        e = json.loads(ws.receive_text())
        assert e["error"]["code"] == "input_audio_buffer_commit_empty" and e["error"]["event_id"] == "c9"
        ws.send_text("not json")
        assert json.loads(ws.receive_text())["error"]["code"] == "invalid_json"
        ws.send_text(json.dumps({"type": "session.update", "session": {"type": "realtime"}}))
        assert json.loads(ws.receive_text())["error"]["param"] == "session.type"
        ws.send_text(json.dumps({"type": "session.update", "session": {
            "type": "transcription", "audio": {"input": {"transcription": {"language": "fr"}}}}}))
        assert json.loads(ws.receive_text())["error"]["code"] == "unsupported_language"
        ws.send_text(json.dumps({"type": "input_audio_buffer.clear"}))
        assert json.loads(ws.receive_text())["type"] == "input_audio_buffer.cleared"


def test_realtime_not_ready_closes_1013(server):
    with TestClient(server.app) as c:
        server._ready = False
        with c.websocket_connect("/v1/realtime") as ws:
            assert json.loads(ws.receive_text())["error"]["code"] == "backend_unavailable"
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
    assert closed.value.code == 1013


def test_realtime_session_limit(server, monkeypatch):
    with TestClient(server.app) as c:
        monkeypatch.setattr(server, "_sessions", server.asyncio.Semaphore(1))
        with c.websocket_connect("/v1/realtime") as first:
            first.receive_text()
            with c.websocket_connect("/v1/realtime") as second:
                assert json.loads(second.receive_text())["error"]["code"] == "backend_unavailable"
                with pytest.raises(WebSocketDisconnect) as closed:
                    second.receive_text()
    assert closed.value.code == 1013
