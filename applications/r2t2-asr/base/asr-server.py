"""Confucius4-R2T2 ASR server: OpenAI audio transcription API over HTTP and WebSocket.

One process, one model. Implements POST /v1/audio/transcriptions and the
Realtime API (GA) transcription session on /v1/realtime. The R2T2 streaming
algorithm is called as shipped by NetEase Youdao
(R2T2ASRModel.streaming_transcribe / finish_streaming_transcribe); what this
file adds is the serving layer around it:

- Batcher merges the synchronous generate() calls of concurrent sessions into
  one vLLM call, so N live streams share the engine instead of queueing on it.
  The official ws_server.py runs one stream per worker process.
- Session reproduces the per-chunk loop of the official ws_server.py
  (adaptive max_new_tokens, FireRed streaming VAD, hallucination check) for
  one stream, emitting append-only deltas, and ends segments by SegmentPolicy.
- Uploads are cut at pauses and each segment runs through the same streaming
  path. R2T2's offline mode is not used: it dropped whole sentences on 50-80 s
  English audio.
"""

import asyncio
import base64
import json
import logging
import os
import queue
import re
import shutil
import string
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass

import numpy as np
from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

log = logging.getLogger("uvicorn.error")

SR = 16000
MODEL_ID = "confucius4-r2t2"
MODEL_PATH = os.getenv("R2T2_MODEL_PATH", "/models/Confucius4-R2T2")
VAD_MODEL_PATH = os.getenv("VAD_MODEL_PATH", "/models/FireRedVAD/Stream-VAD")

# KV cache in bytes, not a share of the card, so the footprint does not track
# whichever GPU Karpenter picks. Measured with this setting: 9.56 GiB loaded,
# 9.57 GiB at 8 concurrent streams.
KV_CACHE_BYTES = int(float(os.getenv("R2T2_KV_CACHE_GIB", "3")) * 1024 ** 3)
# Only a startup gate once KV is sized in bytes: free memory must be at least
# this share of the card or vLLM refuses to start.
GPU_MEMORY_UTILIZATION = float(os.getenv("R2T2_GPU_MEMORY_UTILIZATION", "0.3"))
MAX_MODEL_LEN = int(os.getenv("R2T2_MAX_MODEL_LEN", "8192"))

# Block size. Realtime 480 ms: against R2T2's headline 160 ms it scored the
# same on FLEURS (zh 5.50 vs 5.62%, en 7.65 vs 7.55%), showed first text as
# early, rewrote less and costs a third of the GPU; larger blocks would update
# captions too rarely. Files 2000 ms: a 480-2000 ms sweep was flat on accuracy
# and wall time fell from 183 s to 84 s.
REALTIME_STEP_MS = int(os.getenv("ASR_REALTIME_STEP_MS", "480"))
FILE_STEP_MS = int(os.getenv("ASR_FILE_STEP_MS", "2000"))
FILE_SEGMENT_CONCURRENCY = int(os.getenv("ASR_FILE_SEGMENT_CONCURRENCY", "8"))
MAX_SESSIONS = int(os.getenv("ASR_MAX_SESSIONS", "16"))
WORKER_THREADS = int(os.getenv("ASR_WORKER_THREADS", "64"))

# Values from the official ws_server.py.
LOOKAHEAD_MS = 160
UNFIXED_TOKEN_NUM = 1
TOKEN_WINDOW = 10
VAD_CONFIG = dict(smooth_window_size=5, speech_threshold=0.4, pad_start_frame=5,
                  min_speech_frame=8, max_speech_frame=2000, min_silence_frame=20,
                  chunk_max_frame=30000)


@dataclass(frozen=True)
class SegmentPolicy:
    """When a segment ends. One rule for uploads and live streams.

    A segment ends at the first VAD pause once it is at least `pause_cut_s`
    long, and unconditionally at `hard_s`. Segments must stay short: R2T2
    re-feeds the whole segment on every chunk, and on FLEURS long segments
    dropped whole sentences -- 60 s ones twice in three files, a 26 s one where
    a quiet sentence followed a loud one. Too short loses context instead:
    one sentence per segment roughly tripled small errors on one file.
    """
    pause_cut_s: float
    hard_s: float


def _policy(prefix: str, pause_cut: str, hard: str) -> SegmentPolicy:
    return SegmentPolicy(float(os.getenv(f"{prefix}_PAUSE_CUT_SECONDS", pause_cut)),
                         float(os.getenv(f"{prefix}_HARD_SECONDS", hard)))


# Uploads, live streams without server VAD, and live streams with it. Swept on
# FLEURS (pause_cut 0-30 s, hard 30 s): files 10 s and server-VAD 0 s (every
# pause) were the settings with no dropped sentences; client-driven streams
# 15 s had the lowest error of their sweep.
FILE_POLICY = _policy("ASR_FILE", "10", "30")
STREAM_POLICY = _policy("ASR_STREAM", "15", "30")
VAD_POLICY = _policy("ASR_VAD", "0", "30")
# Per-request overrides of block size and policy, for tuning only.
ALLOW_TUNING = os.getenv("ASR_ALLOW_TUNING", "0") == "1"

# Trailing silence before finishing a client-ended segment. The author's advice
# for file input, where nothing follows the last word; neutral on FLEURS.
FINAL_PAD_S = 0.5
# FireRed VAD is level-sensitive: on FLEURS English recordings peaking at
# -47 dBFS it marked 6% of the audio as speech, 92% after a boost. Its input
# (only its input; the model hears the original) is raised toward this peak,
# never lowered. The cap keeps near-digital silence from being blown up into
# noise; 50 dB covers the quietest FLEURS file (-49 dBFS).
VAD_TARGET_PEAK_DBFS = -6.0
VAD_MAX_GAIN_DB = 50.0
# Realtime server_vad: silence that ends a turn when the client sets none --
# OpenAI's documented default. VAD_CONFIG's 200 ms (min_silence_frame) is kept
# for cutting uploads, where it is only a candidate cut and FILE_POLICY decides.
# At 200 ms every comma ended a live turn, so turns were fragments.
VAD_SILENCE_MS = int(os.getenv("ASR_VAD_SILENCE_MS", "500"))
VAD_PREFIX_PADDING_MS = 300  # reported only; turns are contiguous, see Session._vad_scan
# A turn is cut this far before the frame where its end was detected, which
# trails the actual silence by the smoothing window (5 frames).
VAD_CUT_MARGIN_MS = 50
# Pieces of a split chunk shorter than this are not decoded on their own.
MIN_DECODE_SAMPLES = SR // 10
# The gain follows the loudest audio of the last few seconds, not of the whole
# input: a -55 dBFS speaker after a 0 dBFS one was otherwise never detected.
VAD_GAIN_WINDOW_S = 3.0
RETRY_AFTER_S = 30

# ISO 639-1 / 639-3 codes accepted on input (zh-* locales resolve via "zh").
LANGUAGES = {"zh": "Chinese", "cmn": "Chinese", "zho": "Chinese",
             "en": "English", "eng": "English"}
ISO_CODES = {"chinese": "zh", "english": "en"}


# --- text helpers, ported from the official ws_server.py (Apache-2.0) ---

_PUNCT_CHARS = "，。！？、；：,.!?;:~…·\"'()（）《》—-"
_PUNCT_RE = re.compile(r"[%s\s]+" % re.escape(_PUNCT_CHARS))
_CN_PUNCT = ("？！＂＃＄％＆＇（）＊＋，－／：；＜＝＞＠［＼］＾＿｀｛｜｝～、。〃〄々〆〇〈〉《》"
             "「」『』【】〔〕〖〗〘〙〚〛〜〝〞〟〰〾〿–—‘’‛“”„‟…‧﹏")
_STRIP_PUNCT = str.maketrans("", "", string.punctuation + _CN_PUNCT)
_TOKEN_RE = re.compile(r"[a-zA-Z]+|[^a-zA-Z]")


def split_text_to_tokens(text: str) -> list[str]:
    """Chinese by character, English by word, punctuation dropped."""
    return [m.group() for m in _TOKEN_RE.finditer(text.translate(_STRIP_PUNCT))
            if m.group().strip()]


def is_last_token_chinese(tokens: list[str]) -> bool:
    return bool(tokens) and any("\u4e00" <= ch <= "\u9fff" for ch in tokens[-1])


def detect_hallucination(text: str, repeat: int = 5, max_len: int = 50,
                         tail: int = 256) -> bool:
    """True if the tail of the text is one pattern looping `repeat` times."""
    if not text:
        return False
    text = text[-tail:]
    for exact, candidate in ((True, text), (False, _PUNCT_RE.sub("", text).lower())):
        n = len(candidate)
        # Normalised text starts at 3: 2-character patterns false-positive on English.
        for k in range(1 if exact else 3, max_len + 1):
            if n < k * repeat:
                continue
            pattern = candidate[-k:]
            if exact and pattern.strip(_PUNCT_CHARS + " \t") == "":
                continue
            if all(candidate[-(r + 1) * k:-r * k] == pattern for r in range(1, repeat)):
                return True
    return False


try:
    from zhconv import convert as _zh_convert
except ImportError:  # tests without zhconv installed
    def _zh_convert(text: str, _locale: str) -> str:
        return text


def to_simplified(text: str) -> str:
    # R2T2 occasionally emits Traditional characters (1 of 40 FLEURS
    # utterances); every client here expects Simplified.
    return _zh_convert(text, "zh-cn") if text else text


def _is_cjk(ch: str) -> bool:
    return "\u3000" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef"


def join_texts(parts: list[str]) -> str:
    """Concatenate segment texts: no separator across Chinese, a space across English."""
    out = ""
    for p in (p.strip() for p in parts):
        if not p:
            continue
        if out and not (_is_cjk(out[-1]) or _is_cjk(p[0])):
            out += " "
        out += p
    return out


# --- engine ---

class EngineDead(RuntimeError):
    pass


class Batcher:
    """Stand-in for vllm.LLM that merges concurrent generate() calls.

    R2T2's streaming_transcribe calls model.generate() synchronously for one
    stream. Each stream runs in a worker thread; its generate call lands here
    and one coordinator thread runs whatever has arrived as a single batched
    vLLM call. Only the coordinator touches the engine.

    Measured on FLEURS with 8 streams: outputs identical to single-stream on
    81-85 of 86 files (the rest differ by float noise in batched kernels), and
    throughput 2.5x single-stream.
    """

    def __init__(self, llm, gather_s: float = 0.005):
        self._llm = llm
        self._q: queue.Queue = queue.Queue()
        self._gather_s = gather_s
        self.dead: BaseException | None = None
        threading.Thread(target=self._loop, name="r2t2-batcher", daemon=True).start()

    def generate(self, prompts, sampling_params=None, use_tqdm=False):
        if self.dead is not None:
            raise EngineDead(str(self.dead))
        done, box = threading.Event(), {}
        self._q.put((prompts, sampling_params, done, box))
        done.wait()
        if "error" in box:
            raise box["error"]
        return box["out"]

    def _loop(self) -> None:
        while True:
            reqs = [self._q.get()]
            deadline = time.monotonic() + self._gather_s
            while (left := deadline - time.monotonic()) > 0:
                try:
                    reqs.append(self._q.get(timeout=left))
                except queue.Empty:
                    break
            prompts, params = [], []
            for p, sp, _, _ in reqs:
                prompts += p
                params += [sp] * len(p)
            try:
                outs = self._llm.generate(prompts, sampling_params=params, use_tqdm=False)
            except Exception as e:
                # vLLM does not recover from a dead EngineCore; record it so
                # /health fails and the kubelet restarts the container.
                self.dead = e
                log.exception("R2T2 engine failed")
                for _, _, done, box in reqs:
                    box["error"] = EngineDead(str(e))
                    done.set()
                continue
            i = 0
            for p, _, done, box in reqs:
                box["out"] = outs[i:i + len(p)]
                i += len(p)
                done.set()


class Engine:
    """The calls a Session makes into R2T2, behind one object so tests can swap it."""

    def __init__(self, asr):
        self.asr = asr

    def new_state(self, language: str | None, chunk_s: float, context: str = ""):
        return self.asr.init_streaming_state(
            context=context, language=language, unfixed_chunk_num=0,
            unfixed_token_num=UNFIXED_TOKEN_NUM, chunk_size_sec=chunk_s)

    def step(self, pcm: np.ndarray, state, max_new_tokens: float) -> str:
        """Feed audio; returns the committed (append-only) text so far."""
        _, fixed = self.asr.streaming_transcribe(pcm, state, int(max_new_tokens), False)
        # finish() re-parses from a rolled-back prefix that can lose the
        # "language X" header, leaving state.language empty; keep the last
        # language a chunk did report.
        if getattr(state, "language", ""):
            state.r2t2_language = state.language
        return fixed.split("|")[0]

    def finish(self, state, max_new_tokens: int) -> str:
        return self.asr.finish_streaming_transcribe(state, max_new_tokens).split("|")[0]

    @staticmethod
    def language(state) -> str | None:
        lang = getattr(state, "language", "") or getattr(state, "r2t2_language", "")
        if not lang:
            # R2T2 does not always report a language (seen on Chinese). Only
            # zh and en are supported, so the script of the text decides.
            text = getattr(state, "text", "") or ""
            cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
            latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
            lang = "chinese" if cjk and cjk >= latin / 4 else ("english" if latin else "")
        return (lang or "").lower() or None


class VadFactory:
    """FireRed streaming VAD with the model loaded once.

    FireRedStreamVad keeps its stream state (model caches, smoothing window,
    speech/silence state machine) on the instance and the network itself is
    stateless, so each session gets its own instance around the shared model.
    The official server shares one instance, which is safe only because it
    runs one stream per process.
    """

    def __init__(self, model_dir: str):
        import torch
        from fireredvad.core.audio_feat import AudioFeat
        from fireredvad.core.detect_model import DetectModel
        from fireredvad.stream_vad import FireRedStreamVadConfig

        # CPU, unlike the official server: the model is 2 MB and a 160 ms
        # chunk is 16 frames, and keeping CUDA out of this process leaves the
        # GPU to the vLLM engine process alone.
        torch.set_num_threads(1)
        self._config = FireRedStreamVadConfig(use_gpu=False, **VAD_CONFIG)
        self._feat = AudioFeat(os.path.join(model_dir, "cmvn.ark"))
        self._model = DetectModel.from_pretrained(model_dir).cpu()

    def stream(self, speech_threshold: float | None = None,
               min_silence_frame: int | None = None):
        """A fresh stream detector. Arguments override VAD_CONFIG per session."""
        from fireredvad.core.stream_vad_postprocessor import StreamVadPostprocessor
        from fireredvad.stream_vad import FireRedStreamVad

        c = self._config
        post = StreamVadPostprocessor(c.smooth_window_size,
                                      c.speech_threshold if speech_threshold is None else speech_threshold,
                                      c.pad_start_frame, c.min_speech_frame, c.max_speech_frame,
                                      c.min_silence_frame if min_silence_frame is None else min_silence_frame)
        return FireRedStreamVad(self._feat, self._model, post, c)

    def speech_spans(self, audio: np.ndarray) -> list[tuple[float, float]]:
        """(start, end) seconds of each speech span; spans are at most 20 s."""
        _, result = self.stream().detect_full(_to_int16(local_gain(audio)))
        return [tuple(t) for t in result["timestamps"]]


def vad_gain(peak: float) -> float:
    """Boost-only gain bringing `peak` toward VAD_TARGET_PEAK_DBFS."""
    if peak <= 0:
        return 1.0
    want = 10 ** (VAD_TARGET_PEAK_DBFS / 20) / peak
    return float(min(10 ** (VAD_MAX_GAIN_DB / 20), max(1.0, want)))


def local_gain(audio: np.ndarray) -> np.ndarray:
    """VAD input with the gain set per VAD_GAIN_WINDOW_S window."""
    n = int(VAD_GAIN_WINDOW_S * SR)
    out = np.empty_like(audio)
    for i in range(0, len(audio), n):
        w = audio[i:i + n]
        out[i:i + n] = w * vad_gain(float(np.abs(w).max()) if len(w) else 0.0)
    return out


class RecentPeak:
    """Loudest sample over the last VAD_GAIN_WINDOW_S of a stream."""

    def __init__(self):
        self._chunks: list[tuple[int, float]] = []

    def update(self, chunk: np.ndarray) -> float:
        self._chunks.append((len(chunk), float(np.abs(chunk).max()) if len(chunk) else 0.0))
        keep, total = [], 0
        for n, p in reversed(self._chunks):
            keep.append((n, p))
            total += n
            if total >= VAD_GAIN_WINDOW_S * SR:
                break
        self._chunks = keep[::-1]
        return max(p for _, p in self._chunks)


def _to_int16(audio: np.ndarray) -> np.ndarray:
    return (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)


# --- streaming session ---

@dataclass
class Event:
    kind: str  # delta | completed | speech_started | speech_stopped
    text: str = ""
    language: str | None = None
    ms: int = 0  # audio position, for speech_started / speech_stopped


class Session:
    """One audio stream: the per-chunk loop of the official ws_server.py.

    Synchronous; every call blocks on the engine and is meant to run in a
    worker thread. Text is append-only: a delta is only ever new characters
    after what was already emitted for the segment, and `completed` closes the
    segment with its full text.
    """

    def __init__(self, engine: Engine, vad_factory: VadFactory | None,
                 language: str | None, step_ms: int, server_vad: bool = False,
                 stream_policy: SegmentPolicy | None = None,
                 vad_policy: SegmentPolicy | None = None,
                 context: str = "", vad_overrides: dict | None = None):
        self.engine = engine
        self.language = language
        self.context = context
        self.server_vad = server_vad
        self.stream_policy = stream_policy or STREAM_POLICY
        self.vad_policy = vad_policy or VAD_POLICY
        self.step = int(round(step_ms / 1000 * SR))
        self.look = int(round(LOOKAHEAD_MS / 1000 * SR))
        self.chunk_s = step_ms / 1000
        self.vad = vad_factory.stream(**(vad_overrides or {})) if vad_factory else None
        # Frames of silence the VAD waits before reporting an end; the speech
        # itself ended that long before the frame it reports.
        self.vad_silence_frames = (vad_overrides or {}).get(
            "min_silence_frame", VAD_CONFIG["min_silence_frame"])
        self.first_max_new = max(1, int((self.step + self.look) / 1280))
        self.max_new: float = self.first_max_new
        self.floor = min(32, max(4, 2 * int(self.step / 1280)))
        self.tokens: list[str] = []
        self.pending = np.zeros(0, dtype=np.float32)
        self.is_first = True
        self.peak = RecentPeak()
        # VAD frame indices restart when the detector is reset by clear();
        # this is the stream position (ms) they are relative to.
        self.vad_base_ms = 0
        self.audio_ms = 0
        # Samples the VAD has seen since its last reset, to place its frames,
        # and where finalize() padding sits among them. That padding is not
        # client audio; counted as such, every commit shifted later
        # timestamps 0.5 s.
        self.vad_samples = 0
        self.vad_pads: list[tuple[int, int]] = []  # (vad sample, length)
        # Start of the next turn held back from a split chunk, decoded with
        # the next chunk because it is too short to decode alone.
        self.carry = np.zeros(0, dtype=np.float32)
        # Set while finalize() feeds its padding, which is silence by
        # construction and must not close the segment a second time.
        self._closing = False
        self._new_segment()

    @property
    def policy(self) -> SegmentPolicy:
        return self.vad_policy if self.server_vad else self.stream_policy

    def _new_segment(self) -> None:
        self.state = self.engine.new_state(self.language, self.chunk_s, self.context)
        self.last_fixed = ""
        self.seg_s = 0.0
        self.fed = False

    def configure(self, language: str | None, server_vad: bool,
                  stream_policy: SegmentPolicy | None = None,
                  vad_policy: SegmentPolicy | None = None,
                  context: str | None = None) -> None:
        """Language and prompt apply from the next segment, or now if nothing was fed."""
        self.server_vad = server_vad
        self.stream_policy = stream_policy or self.stream_policy
        self.vad_policy = vad_policy or self.vad_policy
        changed = language != self.language or (context is not None and context != self.context)
        self.language = language
        if context is not None:
            self.context = context
        if changed and not self.fed:
            self._new_segment()

    def feed(self, pcm: np.ndarray) -> list[Event]:
        self.audio_ms += int(len(pcm) * 1000 / SR)
        self.pending = np.concatenate([self.pending, np.asarray(pcm, dtype=np.float32)])
        events: list[Event] = []
        while True:
            need = self.step + self.look if self.is_first else self.step
            if len(self.pending) < need:
                return events
            seg, self.pending = self.pending[:need], self.pending[need:]
            events += self._process(seg)

    def _process(self, seg: np.ndarray) -> list[Event]:
        events, cut = self._vad_scan(seg)
        p = self.policy
        if cut is not None and not self._closing and self.seg_s + len(seg) / SR >= p.pause_cut_s:
            # Split the chunk where the pause was detected: audio before it is
            # the silence that ended this turn, audio after it may already be
            # the next phrase. Feeding the whole chunk to the closing segment
            # lost that onset -- the first word of every turn after a cut.
            head, tail = seg[:cut], seg[cut:]
            if len(head):
                events += self._decode(head)
            # The same trailing silence a client commit gets: cut at the pause,
            # the segment ends right after its last word, and R2T2 did not emit
            # that word -- "第一句" came back as "第一".
            events += self._decode(np.zeros(int(FINAL_PAD_S * SR), dtype=np.float32))
            events += self._close_segment(emit_empty=False)
            if len(tail) >= MIN_DECODE_SAMPLES:
                events += self._decode(tail)
            else:
                self.carry = tail
            return events
        events += self._decode(seg)
        if not self._closing and (self.seg_s >= p.hard_s or detect_hallucination(self.last_fixed)):
            events += self._close_segment(emit_empty=False)
        return events

    def _vad_scan(self, seg: np.ndarray) -> tuple[list[Event], int | None]:
        """VAD events for `seg`, and the sample index in it to cut at on a pause."""
        events: list[Event] = []
        cut = None
        start = self.vad_samples
        self.vad_samples += len(seg)
        if self.vad is None:
            return events, cut
        gain = vad_gain(self.peak.update(seg))
        for r in self.vad.detect_chunk(_to_int16(seg * gain)):
            if r.is_speech_start:
                events.append(Event("speech_started", ms=self._client_ms(max(0, r.speech_start_frame - 1) * 10)))
            if r.is_speech_end:
                # OpenAI's audio_end_ms is where speech ended, not where the
                # silence after it was long enough to be sure.
                spoke = max(0, r.speech_end_frame - 1 - self.vad_silence_frames) * 10
                events.append(Event("speech_stopped", ms=self._client_ms(spoke)))
                # speech_end_frame is where the end was *detected*, after
                # min_silence frames of silence; the smoothing window lags it
                # by a few frames more, so step back by that margin.
                at = (max(0, r.speech_end_frame - 1) * 10 - VAD_CUT_MARGIN_MS) * SR // 1000
                cut = min(len(seg), max(0, at - start))
                if cut < MIN_DECODE_SAMPLES:
                    cut = 0  # too little to decode alone; it goes to the next turn
        return events, cut

    def _client_ms(self, vad_ms: int) -> int:
        """A VAD position (ms since its reset) as a position in the client's audio."""
        pos = vad_ms * SR // 1000
        skip = sum(min(n, max(0, pos - start)) for start, n in self.vad_pads)
        return self.vad_base_ms + (pos - skip) * 1000 // SR

    def _decode(self, seg: np.ndarray) -> list[Event]:
        events: list[Event] = []
        if len(self.carry):
            seg, self.carry = np.concatenate([self.carry, seg]), self.carry[:0]
        self.is_first = False
        self.fed = True
        self.state.chunk_size_sec = len(seg) / SR
        self.state.chunk_size_samples = len(seg)
        self.seg_s += len(seg) / SR

        fixed = self.engine.step(seg, self.state, self.max_new)
        if len(fixed) > len(self.last_fixed):
            new = fixed[len(self.last_fixed):]
            self.last_fixed += new
            events.append(Event("delta", new))
            self.tokens = (self.tokens + split_text_to_tokens(new))[-TOKEN_WINDOW:]
            self.max_new = max(1, int(self.step / 1280))
        elif not is_last_token_chinese(self.tokens):
            self.max_new += 0.5
        else:
            self.max_new = max(1, int(self.step / 1280))
        if is_last_token_chinese(self.tokens):
            self.max_new *= 2
        self.max_new = min(self.floor, self.max_new)
        return events

    def _close_segment(self, emit_empty: bool) -> list[Event]:
        events: list[Event] = []
        text = self.engine.finish(self.state, self.first_max_new)
        if len(text) > len(self.last_fixed):
            new = text[len(self.last_fixed):]
            events.append(Event("delta", new))
            full = self.last_fixed + new
        else:
            full = self.last_fixed
        language = self.engine.language(self.state)
        if full or emit_empty:
            events.append(Event("completed", full, language))
        self._new_segment()
        return events

    def finalize(self, pad_s: float = FINAL_PAD_S) -> list[Event]:
        """End the segment now (client commit). Always emits exactly one `completed`."""
        events: list[Event] = []
        if self.fed:
            self._closing = True
            pad = int(pad_s * SR)
            pad_at = self.vad_samples + len(self.pending)
            try:
                events = [e for e in self.feed(np.zeros(pad, dtype=np.float32))
                          if e.kind == "delta"]
            finally:
                self._closing = False
                self.audio_ms -= int(pad_s * 1000)  # padding is not client audio
                # The VAD saw everything fed except what is left in pending,
                # which goes straight to the engine below: pad - len(pending)
                # more samples than the client sent.
                if self.vad is not None and pad > len(self.pending):
                    self.vad_pads.append((pad_at, pad - len(self.pending)))
        if len(self.carry):
            self.pending = np.concatenate([self.carry, self.pending])
            self.carry = self.carry[:0]
        if len(self.pending):
            # Shorter than a chunk: R2T2 buffers it and finish() decodes it.
            self.state.chunk_size_samples = len(self.pending) + 1
            self.engine.step(self.pending, self.state, self.max_new)
            self.pending = np.zeros(0, dtype=np.float32)
        return events + self._close_segment(emit_empty=True)

    def clear(self) -> list[Event]:
        """Drop the segment without a result (input_audio_buffer.clear)."""
        self.pending = np.zeros(0, dtype=np.float32)
        self.carry = self.carry[:0]
        if self.vad is not None:
            self.vad.reset()
            self.vad_base_ms = self.audio_ms
            self.vad_samples = 0
            self.vad_pads = []
        self._new_segment()
        return []


# Uploads are cut by segment_bounds before reaching a Session, so its own
# rules must never fire there.
_NO_CUT = SegmentPolicy(float("inf"), float("inf"))


def transcribe_span(engine: Engine, audio: np.ndarray, language: str | None,
                    step_ms: int = FILE_STEP_MS, context: str = "") -> tuple[str, str | None]:
    """One segment of an upload through the streaming path, fed as fast as possible."""
    s = Session(engine, None, language, step_ms, stream_policy=_NO_CUT, context=context)
    events = s.feed(audio) + s.finalize()
    done = [e for e in events if e.kind == "completed"]
    return join_texts([e.text for e in done]), next((e.language for e in done if e.language), None)


# --- runtime ---

_engine: Engine | None = None
_vad: VadFactory | None = None
_aligner: "Aligner | None" = None  # defined with the HTTP helpers below
_batcher: Batcher | None = None
_ready = False
_pool = ThreadPoolExecutor(max_workers=WORKER_THREADS, thread_name_prefix="r2t2")
_sessions = asyncio.Semaphore(MAX_SESSIONS)


def _load_runtime() -> None:
    """Load model and VAD, then warm up. Replaced wholesale in tests."""
    global _engine, _vad, _batcher, _aligner
    from r2t2 import R2T2ASRModel

    asr = R2T2ASRModel.LLM(model=MODEL_PATH, gpu_memory_utilization=GPU_MEMORY_UTILIZATION,
                           kv_cache_memory_bytes=KV_CACHE_BYTES, max_model_len=MAX_MODEL_LEN,
                           max_new_tokens=4)
    _batcher = Batcher(asr.model)
    asr.model = _batcher
    _engine = Engine(asr)
    _vad = VadFactory(VAD_MODEL_PATH)
    if ALIGNER_PATH and os.path.isdir(ALIGNER_PATH):
        # After vLLM, whose startup check needs the free memory this would take.
        _aligner = Aligner(ALIGNER_PATH, ALIGNER_DEVICE)
        _aligner.align(np.zeros(SR, dtype=np.float32), "你好", "Chinese")
    else:
        log.warning("forced aligner not found at %r; segments stay pause-cut", ALIGNER_PATH)
    # First inference JIT-compiles kernels (cached on EFS via CUDA_CACHE_PATH);
    # absorb it before the pod is Ready, on both block sizes.
    started = time.monotonic()
    silence = np.zeros(SR, dtype=np.float32)
    transcribe_span(_engine, silence, "Chinese")
    s = Session(_engine, _vad, None, REALTIME_STEP_MS)
    s.feed(silence)
    s.finalize()
    log.info("R2T2 warm-up took %.1fs", time.monotonic() - started)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Loads synchronously: uvicorn accepts connections only after this, which
    # is fine because the startup probe gates liveness until /ready passes.
    global _ready
    _load_runtime()
    _ready = True
    yield


app = FastAPI(
    title="R2T2 ASR",
    description=("OpenAI-compatible speech recognition backed by Confucius4-R2T2.\n\n"
                 "**`/v1/realtime` is a WebSocket.** OpenAPI cannot describe one, so the "
                 "entry under `paths` is a plain GET that returns the event protocol."),
    lifespan=_lifespan,
)


def _alive() -> bool:
    return _batcher is None or _batcher.dead is None


def _unavailable() -> JSONResponse:
    return JSONResponse(status_code=503, headers={"Retry-After": str(RETRY_AFTER_S)},
                        content={"error": {"message": "Transcription engine is unavailable, "
                                                      f"retry after {RETRY_AFTER_S}s.",
                                           "type": "server_error", "param": None,
                                           "code": "backend_unavailable"}})


async def _run(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_pool, fn, *args)


def _language(value) -> str | None:
    """ISO 639-1/639-3 or zh-* locale code -> the R2T2 language name; None for auto."""
    if not value or not isinstance(value, str):
        return None
    code = value.strip().lower()
    return LANGUAGES.get(code) or LANGUAGES.get(code.split("-")[0])


def _iso(language: str | None) -> str | None:
    """R2T2's detected language ('chinese') -> ISO 639-1 ('zh')."""
    return ISO_CODES.get((language or "").lower())


class ApiError(Exception):
    """An OpenAI-shaped error: {"error": {message, type, param, code}}."""

    def __init__(self, message: str, code: str, param: str | None = None,
                 kind: str = "invalid_request_error", status: int = 400):
        super().__init__(message)
        self.body = {"message": message, "type": kind, "param": param, "code": code}
        self.status = status


def _error_response(e: ApiError, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=e.status, content={"error": e.body}, headers=headers)


# --- HTTP ---

def decode_audio(data: bytes) -> np.ndarray | None:
    """Any container ffmpeg reads -> 16 kHz mono float32."""
    if shutil.which("ffmpeg"):
        p = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le",
                            "-ac", "1", "-ar", str(SR), "pipe:1"],
                           input=data, capture_output=True, timeout=600)
        if p.returncode != 0:
            return None
        return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    import io
    import soundfile as sf
    try:
        wav, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=False)
    except Exception:
        return None
    if wav.ndim > 1:
        wav = wav.mean(axis=1)
    if sr != SR:
        import librosa
        wav = librosa.resample(wav, orig_sr=sr, target_sr=SR)
    return wav.astype(np.float32)


def segment_bounds(duration: float, spans: list[tuple[float, float]],
                   policy: SegmentPolicy = None) -> list[tuple[int, int]]:
    """Contiguous sample ranges covering [0, duration), cut by `policy`.

    The same rule a live Session applies: a segment ends at the first pause
    once it is at least pause_cut_s long, and at hard_s regardless. Pauses are
    the gaps between VAD speech spans, cut at their midpoint so neither the
    previous word's decay nor the next word's onset is clipped. Contiguous on
    purpose: VAD only chooses cut points, so a missed word shifts a cut
    instead of losing audio.
    """
    policy = policy or FILE_POLICY
    gaps = sorted((a[1] + b[0]) / 2 for a, b in zip(spans, spans[1:]) if b[0] > a[1])
    bounds, start = [], 0.0
    for g in gaps + [duration]:
        while g - start > policy.hard_s:
            bounds.append((start, start + policy.hard_s))
            start += policy.hard_s
        if g < duration and g - start >= policy.pause_cut_s:
            bounds.append((start, g))
            start = g
    bounds.append((start, duration))
    return [(int(a * SR), int(b * SR)) for a, b in bounds if b > a]


def _tuned_policy(raw, default: SegmentPolicy) -> SegmentPolicy:
    """[pause_cut_s, hard_s] from a request, honoured only with ASR_ALLOW_TUNING."""
    if not ALLOW_TUNING or raw in (None, ""):
        return default
    if isinstance(raw, str):
        raw = json.loads(raw)
    return SegmentPolicy(float(raw[0]), float(raw[1]))


RESPONSE_FORMATS = ("json", "text", "verbose_json", "srt", "vtt")
TIMESTAMP_GRANULARITIES = ("word", "segment")


# --- forced alignment: word times, and sentence segments built from them ---

# Qwen3-ForcedAligner-0.6B. Upload segments are cut at pauses for recognition
# and stay 10-30 s long, which is right for accuracy and wrong for subtitles:
# a 7 s clip with three sentences came back as one cue. The aligner puts a
# time on every word of the recognised text; sentences are then cut at the
# text's own punctuation. Measured on a 32 GB card: ~30 ms per 12 s segment on
# GPU (0.15 s per minute of audio) for 2.4 GiB of memory; ~1.4 s per segment on
# CPU. Disabled when the path is empty or missing.
ALIGNER_PATH = os.getenv("ASR_ALIGNER_PATH", "/models/Qwen3-ForcedAligner-0.6B")
ALIGNER_DEVICE = os.getenv("ASR_ALIGNER_DEVICE", "cuda")
# PyTorch keeps freed blocks cached: after a few 150 s windows this process
# held 5.6 GiB of GPU memory against 2.4 GiB at load, on a card shared with
# other tenants and not isolated. Cache above this is returned after a call.
ALIGNER_KEEP_GIB = float(os.getenv("ASR_ALIGNER_KEEP_GIB", "2.5"))
# Sentence ends: CJK and ASCII terminal marks, optionally followed by closing
# quotes or brackets, so "3.5" and "e.g" stay whole.
# ASCII marks count only before whitespace or the end: inside a token
# ("10:00-11:00", "a,b") they would split one aligned word in two.
_CLOSE = "”’\"'）)】」』》"
_SENTENCE_END = re.compile(rf"[。！？；…]+[{_CLOSE}]*|[.!?;]+[{_CLOSE}]*(?=\s|$)")
_CLAUSE_END = re.compile(rf"[，、：—]+[{_CLOSE}]*|[,:]+[{_CLOSE}]*(?=\s|$)")
# Within an over-long clause, a gap this long between words also ends a cue.
CUE_PAUSE_SECONDS = 0.4
# "St. Petersburg" is not two sentences.
_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "prof", "vs", "etc", "no", "mt"}
# R2T2 often punctuates a run of sentences with commas, so a "sentence" can be
# 30-50 s. Past this a cue is split again at clause marks; ~7 s is the usual
# ceiling for one subtitle.
MAX_CUE_SECONDS = float(os.getenv("ASR_MAX_CUE_SECONDS", "7"))


class Aligner:
    """Qwen3-ForcedAligner behind one lock: one GPU forward at a time, ~30 ms each."""

    def __init__(self, path: str, device: str):
        import torch
        from qwen_asr import Qwen3ForcedAligner

        self._torch = torch
        dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32
        self._fa = Qwen3ForcedAligner.from_pretrained(path, dtype=dtype, device_map=device)
        self._lock = threading.Lock()

    def tokenize(self, text: str) -> list[str]:
        """The units align() returns times for: CJK characters and Latin words."""
        return self._fa.aligner_processor.tokenize_space_lang(text)

    def align(self, audio: np.ndarray, text: str, language: str) -> list[tuple[str, float, float]]:
        with self._lock, self._torch.inference_mode():
            try:
                result = self._fa.align(audio=(audio, SR), text=text, language=language)[0]
            finally:
                cuda = self._torch.cuda
                if cuda.is_available() and cuda.memory_reserved() > ALIGNER_KEEP_GIB * 2**30:
                    cuda.empty_cache()
        return [(i.text, float(i.start_time), float(i.end_time)) for i in result.items]


def clip_to_speech(words: list[dict], spans: list[tuple[float, float]]) -> list[dict]:
    """End a word at the pause it runs into.

    The aligner places no silence between sentences: a sentence-final word
    ends where the next sentence starts ("terms" 14.56-16.16 s across a 0.6 s
    gap), so its cue overlapped the pause. A word whose end lies past the end
    of the VAD speech span it starts in, and past the start of the next span,
    is cut back to that span's end.
    """
    out = []
    for w in words:
        end = w["end"]
        for (a, b), nxt in zip(spans, spans[1:] + [(float("inf"), 0.0)]):
            if a - 0.05 <= w["start"] < b and end > nxt[0] and end > b:
                end = round(max(w["start"], b), 3)
                break
        out.append({**w, "end": end})
    return out


def fix_zero_spans(words: list[dict]) -> list[dict]:
    """Place the aligner's zero-length words right after the word before them.

    The aligner gives some words a zero-length span (upstream Qwen3-ASR #197),
    and a sentence-final one can land seconds late, next to the following
    sentence -- "officers" at 8.48 s after "police" ended at 4.08 s -- which
    stretched the cue across the pause. Such a word gets the typical word
    length, starting where the previous one ended.
    """
    lengths = sorted(w["end"] - w["start"] for w in words if w["end"] > w["start"])
    typical = lengths[len(lengths) // 2] if lengths else 0.25
    out: list[dict] = []
    for k, w in enumerate(words):
        if w["end"] <= w["start"]:
            start = out[-1]["end"] if out else w["start"]
            nxt = next((x["start"] for x in words[k + 1:] if x["end"] > x["start"]), None)
            end = start + typical if nxt is None else min(start + typical, max(start, nxt))
            w = {**w, "start": round(start, 3), "end": round(end, 3)}
        out.append(w)
    return out


def tidy_words(words: list[dict], spans: list[tuple[float, float]]) -> list[dict]:
    return fix_zero_spans(clip_to_speech(words, spans))


def _split_at(pattern: re.Pattern, text: str) -> list[str]:
    out, start = [], 0
    for m in pattern.finditer(text):
        out.append(text[start:m.end()])
        start = m.end()
    if text[start:].strip():
        out.append(text[start:])
    elif out:
        out[-1] += text[start:]
    return out


def split_sentences(text: str) -> list[str]:
    """Sentences of `text` with their punctuation; concatenated they give `text` back."""
    out: list[str] = []
    for part in _split_at(_SENTENCE_END, text):
        last = re.findall(r"([A-Za-z]+)\.\s*$", part)
        if out and out[-1].endswith("\x00"):
            out[-1] = out[-1][:-1] + part
        else:
            out.append(part)
        if last and last[0].lower() in _ABBREVIATIONS:
            out[-1] += "\x00"  # join the next part onto this one
    return [p.rstrip("\x00") for p in out]


def _split_at_pauses(clause: str, span: list[dict], tokenize) -> list[tuple[str, list[dict]]]:
    """Cut a clause where its words pause; text is cut between the words."""
    cuts = [k for k in range(1, len(span))
            if span[k]["start"] - span[k - 1]["end"] >= CUE_PAUSE_SECONDS]
    if not cuts:
        return [(clause, span)]
    # Cut each piece just before the first character of its first word, so
    # punctuation and spaces stay with the words before them.
    starts, p = [], 0
    for k in cuts:
        while p < len(clause) and len(tokenize(clause[:p + 1])) <= k:
            p += 1
        starts.append(p)
    bounds = [0] + starts + [len(clause)]
    return [(clause[bounds[j]:bounds[j + 1]], span[([0] + cuts)[j]:(cuts + [len(span)])[j]])
            for j in range(len(bounds) - 1)]


def sentence_segments(text: str, words: list[dict], tokenize,
                      max_cue: float = MAX_CUE_SECONDS) -> list[dict] | None:
    """Subtitle-sized segments of `text`, timed by their first and last word.

    A segment is a sentence, or, for a sentence longer than `max_cue`, a run of
    its clauses packed up to that length. Words are the aligner's units in
    order; each piece of text takes as many as it tokenizes into. None if the
    two do not line up, so the caller keeps the pause-cut segments instead of
    mislabelling every cue after a mismatch.
    """
    pieces = []  # (text, start, end, ends_sentence)
    i = 0
    for sentence in split_sentences(text):
        clauses = _split_at(_CLAUSE_END, sentence)
        for k, clause in enumerate(clauses):
            n = len(tokenize(clause))
            last = k == len(clauses) - 1
            if n == 0:
                if pieces:  # stray punctuation belongs to the piece before
                    t, a, b, e = pieces[-1]
                    pieces[-1] = (t + clause, a, b, e or last)
                continue
            if i + n > len(words):
                return None
            span = words[i:i + n]
            i += n
            subs = _split_at_pauses(clause, span, tokenize) \
                if span[-1]["end"] - span[0]["start"] > max_cue else [(clause, span)]
            for k2, (t, sp) in enumerate(subs):
                pieces.append((t, sp[0]["start"], max(w["end"] for w in sp),
                               last and k2 == len(subs) - 1))
    if i != len(words):
        return None

    segments: list[dict] = []
    cue = None
    for text_, start, end, ends_sentence in pieces:
        if cue is not None and end - cue["start"] > max_cue:
            segments.append(cue)
            cue = None
        if cue is None:
            cue = {"id": len(segments), "start": start, "end": end, "text": text_}
        else:
            cue["text"] += text_
            cue["end"] = max(cue["end"], end)
        if ends_sentence:
            segments.append(cue)
            cue = None
    if cue is not None:
        segments.append(cue)
    for k, seg in enumerate(segments):
        seg["id"], seg["text"] = k, seg["text"].strip()
    return segments


async def align_words(audio: np.ndarray, ranges: list[tuple[int, int]],
                      texts: list[str], languages: list[str | None]) -> list[dict]:
    """Word times across the whole upload, one aligner call per recognised segment."""
    words: list[dict] = []
    for (a, b), text, lang in zip(ranges, texts, languages):
        if not text:
            continue
        items = await _run(_aligner.align, audio[a:b], text, lang or "Chinese")
        offset, end = a / SR, b / SR
        words += [{"word": w, "start": round(min(offset + s, end), 3),
                   "end": round(min(offset + e, end), 3)} for w, s, e in items]
    return words


def _timestamp(seconds: float, sep: str) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _subtitles(segments: list[dict], fmt: str) -> str:
    sep = "," if fmt == "srt" else "."
    cues = []
    for i, s in enumerate(segments, 1):
        times = f"{_timestamp(s['start'], sep)} --> {_timestamp(s['end'], sep)}"
        cues.append(f"{i}\n{times}\n{s['text']}" if fmt == "srt" else f"{times}\n{s['text']}")
    body = "\n\n".join(cues)
    return (body + "\n") if fmt == "srt" else f"WEBVTT\n\n{body}\n"


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/v1/audio/transcriptions")
async def transcriptions(file: UploadFile = File(...), model: str = Form(""),
                         language: str = Form(""), prompt: str = Form(""),
                         response_format: str = Form("json"), stream: bool = Form(False),
                         temperature: float = Form(0.0),
                         timestamp_granularities: list[str] | None = Form(
                             None, alias="timestamp_granularities[]"),
                         x_policy: str = Form(""), x_step_ms: str = Form("")):
    """OpenAI /v1/audio/transcriptions.

    `model` and `temperature` are accepted and ignored: one model, greedy
    decoding. `prompt` is passed to R2T2 as context. With the aligner loaded,
    verbose_json / srt / vtt segments are sentences, and
    timestamp_granularities[]=word adds `words`. `x_*` fields are tuning
    knobs honoured only with ASR_ALLOW_TUNING=1.
    """
    if not (_ready and _alive()):
        return _unavailable()
    if response_format not in RESPONSE_FORMATS:
        return _error_response(ApiError(
            f"response_format must be one of {', '.join(RESPONSE_FORMATS)}",
            "invalid_value", "response_format"))
    if stream and response_format not in ("json", "text"):
        return _error_response(ApiError(
            "stream is only supported with response_format json or text",
            "invalid_value", "stream"))
    granularities = set(timestamp_granularities or ["segment"])
    if timestamp_granularities is not None:
        if response_format != "verbose_json":
            return _error_response(ApiError(
                "timestamp_granularities requires response_format verbose_json",
                "invalid_value", "timestamp_granularities"))
        if not granularities <= set(TIMESTAMP_GRANULARITIES):
            return _error_response(ApiError(
                f"timestamp_granularities values must be {' or '.join(TIMESTAMP_GRANULARITIES)}",
                "invalid_value", "timestamp_granularities"))
    if "word" in granularities and _aligner is None:
        return _error_response(ApiError(
            "Word timestamps need the forced aligner, which this deployment does not load",
            "unsupported_value", "timestamp_granularities"))
    audio = await _run(decode_audio, await file.read())
    if audio is None or len(audio) == 0:
        return _error_response(ApiError("Could not decode the uploaded audio.",
                                        "invalid_audio", "file"))
    duration = len(audio) / SR
    policy = _tuned_policy(x_policy, FILE_POLICY)
    step_ms = int(x_step_ms) if ALLOW_TUNING and x_step_ms else FILE_STEP_MS
    spans = await _run(_vad.speech_spans, audio)
    ranges = segment_bounds(duration, spans, policy)
    lang = _language(language)
    gate = asyncio.Semaphore(FILE_SEGMENT_CONCURRENCY)

    async def one(a: int, b: int):
        async with gate:
            return await _run(transcribe_span, _engine, audio[a:b], lang, step_ms, prompt)

    tasks = [asyncio.ensure_future(one(a, b)) for a, b in ranges]
    usage = {"type": "duration", "seconds": int(np.ceil(duration))}

    if stream:
        async def events():
            # Segments run concurrently; deltas are released in audio order.
            parts = []
            try:
                for t in tasks:
                    text, _ = await t
                    text = to_simplified(text)
                    if text:
                        delta = (" " if parts and not (_is_cjk(parts[-1][-1]) or _is_cjk(text[0]))
                                 else "") + text
                        parts.append(text)
                        yield _sse({"type": "transcript.text.delta", "delta": delta})
                yield _sse({"type": "transcript.text.done", "text": join_texts(parts),
                            "usage": usage})
            except EngineDead:
                yield _sse({"type": "error", "error": ApiError(
                    "Transcription engine is unavailable.", "backend_unavailable",
                    kind="server_error", status=503).body})
            finally:
                for t in tasks:
                    t.cancel()

        return StreamingResponse(events(), media_type="text/event-stream")

    try:
        results = await asyncio.gather(*tasks)
    except EngineDead:
        return _unavailable()
    texts = [to_simplified(t) for t, _ in results]
    text = join_texts(texts)
    detected = next((l for _, l in results if l), None)
    if response_format == "text":
        return PlainTextResponse(text)
    if response_format == "json":
        return {"text": text, "usage": usage}
    segments = [{"id": i, "start": round(a / SR, 2), "end": round(b / SR, 2), "text": t}
                for i, ((a, b), t) in enumerate(zip(ranges, texts)) if t]
    words = None
    if _aligner is not None:
        try:
            words = tidy_words(await align_words(audio, ranges, texts, [l for _, l in results]),
                               spans)
        except Exception:
            log.exception("forced alignment failed")
            if "word" in granularities:
                return _error_response(ApiError(
                    "Word alignment failed.", "alignment_failed",
                    kind="server_error", status=500))
        if words is not None:
            sentences = sentence_segments(text, words, _aligner.tokenize)
            if sentences is not None:
                segments = sentences
            else:
                log.warning("sentence split did not match aligned words; keeping pause cuts")
    if response_format in ("srt", "vtt"):
        return PlainTextResponse(_subtitles(segments, response_format))
    body = {"task": "transcribe", "language": (detected or "").lower() or None,
            "duration": round(duration, 2), "text": text}
    if "word" in granularities:
        body["words"] = words
    if "segment" in granularities:
        body["segments"] = segments
    body["usage"] = usage
    return body


@app.post("/v1/audio/alignments")
async def alignments(file: UploadFile = File(...), text: str = Form(...),
                     language: str = Form(""), response_format: str = Form("json"),
                     timestamp_granularities: list[str] | None = Form(
                         None, alias="timestamp_granularities[]")):
    """Forced alignment of a known script (an extension; OpenAI has no equivalent).

    The words returned are the script's, never re-recognised. Audio longer than
    ALIGN_WINDOW_S is aligned in windows cut at pauses; recognition is used
    only to decide which part of the script falls in which window.
    """
    if not (_ready and _alive()):
        return _unavailable()
    if _aligner is None:
        return _error_response(ApiError("Forced aligner is not loaded in this deployment",
                                        "aligner_unavailable", kind="server_error", status=503))
    if response_format not in ("json", "srt", "vtt"):
        return _error_response(ApiError("response_format must be json, srt or vtt",
                                        "invalid_value", "response_format"))
    granularities = set(timestamp_granularities or ["segment"])
    if not granularities <= set(TIMESTAMP_GRANULARITIES):
        return _error_response(ApiError(
            f"timestamp_granularities values must be {' or '.join(TIMESTAMP_GRANULARITIES)}",
            "invalid_value", "timestamp_granularities"))
    script = text.strip()
    tokens = _aligner.tokenize(script)
    if not tokens:
        return _error_response(ApiError("text has nothing to align", "invalid_value", "text"))
    audio = await _run(decode_audio, await file.read())
    if audio is None or len(audio) == 0:
        return _error_response(ApiError("Could not decode the uploaded audio.",
                                        "invalid_audio", "file"))
    duration = len(audio) / SR
    lang = _language(language) or ("Chinese" if any(_is_cjk(c) for c in script) else "English")
    try:
        windows = await _script_windows(audio, tokens, lang)
        words = []
        for (a, b), (i, j) in windows:
            if i == j:
                continue
            items = await _run(_aligner.align, audio[a:b], " ".join(tokens[i:j]), lang)
            words += [{"word": w, "start": round(min(a / SR + s, b / SR), 3),
                       "end": round(min(a / SR + e, b / SR), 3)} for w, s, e in items]
        words = tidy_words(words, await _run(_vad.speech_spans, audio))
    except EngineDead:
        return _unavailable()
    segments = sentence_segments(script, words, _aligner.tokenize)
    if segments is None:  # cannot happen when words come from tokens; guard anyway
        return _error_response(ApiError("Alignment did not cover the script.",
                                        "alignment_failed", kind="server_error", status=500))
    if response_format in ("srt", "vtt"):
        return PlainTextResponse(_subtitles(segments, response_format))
    body: dict = {"duration": round(duration, 2), "language": _iso(lang), "text": script}
    if "segment" in granularities:
        body["segments"] = segments
    if "word" in granularities:
        body["words"] = words
    return body


# Whole-file alignment held to within 0.1 s up to 144 s of audio and drifted by
# up to 9 s at 217 s (the model's stated limit is 5 minutes). Activation memory
# grows with the window (2.25 GiB peak at 72 s, 2.78 at 144 s), so it is kept
# well inside the accurate range.
ALIGN_WINDOW_S = float(os.getenv("ASR_ALIGN_WINDOW_SECONDS", "90"))


def _norm_token(t: str) -> str:
    return to_simplified(t).lower()


async def _script_windows(audio: np.ndarray, tokens: list[str], lang: str
                          ) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """[(sample range, script token range)] covering the audio and the script.

    Up to ALIGN_WINDOW_S: one window holding everything. Longer: windows cut
    at pauses, each recognised; the recognised words are matched against the
    script (difflib) and each window boundary maps to the script position
    of the nearest matched word before it.
    """
    n = len(audio)
    if n <= ALIGN_WINDOW_S * SR:
        return [((0, n), (0, len(tokens)))]
    import difflib

    spans = await _run(_vad.speech_spans, audio)
    ranges = segment_bounds(n / SR, spans, FILE_POLICY)  # <= 30 s, for recognition
    texts = await asyncio.gather(*[_run(transcribe_span, _engine, audio[a:b], lang)
                                   for a, b in ranges])
    # Group recognition ranges into windows of at most ALIGN_WINDOW_S.
    windows, cur, rec_tokens, cuts = [], None, [], []
    for (a, b), (t, _) in zip(ranges, texts):
        if cur is not None and b - cur[0] > ALIGN_WINDOW_S * SR:
            windows.append(cur)
            cuts.append(len(rec_tokens))
            cur = None
        cur = (cur[0] if cur else a, b)
        rec_tokens += [_norm_token(x) for x in _aligner.tokenize(to_simplified(t))]
    windows.append(cur)
    matcher = difflib.SequenceMatcher(None, rec_tokens, [_norm_token(x) for x in tokens],
                                      autojunk=False)
    blocks = matcher.get_matching_blocks()

    def script_pos(rec_pos: int) -> int:
        best = 0
        for blk in blocks:
            if blk.a + blk.size <= rec_pos:
                best = blk.b + blk.size
            elif blk.a < rec_pos:
                best = blk.b + (rec_pos - blk.a)
        return best

    bounds = [0] + [script_pos(c) for c in cuts] + [len(tokens)]
    bounds = [max(bounds[:k + 1]) for k in range(len(bounds))]  # monotonic
    return [(w, (bounds[k], bounds[k + 1])) for k, w in enumerate(windows)]


@app.get("/v1/models")
async def models():
    return {"object": "list",
            "data": [{"id": MODEL_ID, "object": "model", "owned_by": "netease-youdao"}]}


# --- WebSocket: OpenAI Realtime transcription session (GA) ---

REALTIME_PROTOCOL = {
    "protocol": "websocket",
    "url": "ws(s)://<host>/v1/realtime",
    "spec": "OpenAI Realtime API (GA), transcription session",
    "session": {
        "type": "transcription",
        "audio.input.format": "{type: 'audio/pcm', rate: 24000 (default) | 16000}",
        "audio.input.transcription": "{model (ignored), language | languages, prompt}",
        "audio.input.turn_detection": "{type: 'server_vad', threshold, silence_duration_ms "
                                      "(500), prefix_padding_ms} "
                                      "(default) or null for client commits",
    },
    "client_events": ["session.update", "input_audio_buffer.append",
                      "input_audio_buffer.commit", "input_audio_buffer.clear"],
    "server_events": ["session.created", "session.updated", "input_audio_buffer.speech_started",
                      "input_audio_buffer.speech_stopped", "input_audio_buffer.committed",
                      "input_audio_buffer.cleared",
                      "conversation.item.input_audio_transcription.delta",
                      "conversation.item.input_audio_transcription.completed", "error"],
    "notes": [
        "Deltas are append-only and arrive while speech is in progress, before the "
        "turn is committed; match them to turns by item_id.",
        "With turn_detection null a turn the client does not commit is still committed "
        "by the server at a pause after 15 s, or at 30 s regardless.",
    ],
}


@app.get("/v1/realtime")
async def realtime_info():
    """Describe the WebSocket protocol served at this same path."""
    return REALTIME_PROTOCOL


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:22]}"


class Resampler:
    """Streaming PCM resampler to the model's 16 kHz (identity at 16 kHz)."""

    def __init__(self, rate: int):
        self.rate = rate
        self._stream = None
        if rate != SR:
            import soxr
            self._stream = soxr.ResampleStream(rate, SR, 1, dtype="float32")

    def __call__(self, pcm: np.ndarray, last: bool = False) -> np.ndarray:
        if self._stream is None:
            return pcm
        return self._stream.resample_chunk(pcm, last=last)


@dataclass
class RealtimeConfig:
    language: str | None = None
    prompt: str = ""
    server_vad: bool = True  # GA default for transcription sessions
    rate: int = 24000  # GA default for audio/pcm
    vad_threshold: float | None = None
    silence_ms: int = VAD_SILENCE_MS
    prefix_padding_ms: int = VAD_PREFIX_PADDING_MS
    stream_policy: SegmentPolicy = STREAM_POLICY
    vad_policy: SegmentPolicy = VAD_POLICY

    def session_json(self, sid: str) -> dict:
        td = None
        if self.server_vad:
            td = {"type": "server_vad", "silence_duration_ms": self.silence_ms,
                  "prefix_padding_ms": self.prefix_padding_ms}
            if self.vad_threshold is not None:
                td["threshold"] = self.vad_threshold
        transcription = {"model": MODEL_ID, "prompt": self.prompt}
        transcription["language"] = _iso(self.language)
        return {"type": "transcription", "id": sid, "object": "realtime.transcription_session",
                "audio": {"input": {"format": {"type": "audio/pcm", "rate": self.rate},
                                    "transcription": transcription,
                                    "turn_detection": td}},
                "include": []}


def apply_session_update(msg: dict, cfg: RealtimeConfig) -> RealtimeConfig:
    """Apply a GA session.update to `cfg`. Raises ApiError on invalid input."""
    sess = msg.get("session")
    if not isinstance(sess, dict):
        raise ApiError("session must be an object", "invalid_value", "session")
    if sess.get("type", "transcription") != "transcription":
        raise ApiError("This server only supports transcription sessions",
                       "invalid_value", "session.type")
    new = RealtimeConfig(**cfg.__dict__)
    audio_in = ((sess.get("audio") or {}).get("input") or {})
    if not isinstance(audio_in, dict):
        raise ApiError("session.audio.input must be an object", "invalid_value",
                       "session.audio.input")
    fmt = audio_in.get("format")
    if fmt is not None:
        if not isinstance(fmt, dict) or fmt.get("type") != "audio/pcm":
            raise ApiError("Only audio/pcm input is supported", "invalid_value",
                           "session.audio.input.format.type")
        rate = int(fmt.get("rate", 24000))
        if rate not in (16000, 24000):
            raise ApiError("audio/pcm rate must be 24000 or 16000", "invalid_value",
                           "session.audio.input.format.rate")
        new.rate = rate
    tr = audio_in.get("transcription")
    if isinstance(tr, dict):
        if "language" in tr and "languages" in tr:
            raise ApiError("Send language or languages, not both", "invalid_value",
                           "session.audio.input.transcription")
        codes = tr.get("languages") if "languages" in tr else (
            [tr["language"]] if tr.get("language") else [])
        if codes is not None and not isinstance(codes, list):
            raise ApiError("languages must be an array", "invalid_value",
                           "session.audio.input.transcription.languages")
        names = [_language(c) for c in (codes or [])]
        if any(n is None for n in names):
            raise ApiError("Supported languages are zh and en", "unsupported_language",
                           "session.audio.input.transcription.language")
        # One language forces it; several or none leave detection to the model.
        new.language = names[0] if len(set(names)) == 1 else None
        if "prompt" in tr:
            new.prompt = str(tr.get("prompt") or "")
    if "turn_detection" in audio_in:
        td = audio_in["turn_detection"]
        if td is None:
            new.server_vad = False
        elif isinstance(td, dict) and td.get("type") == "server_vad":
            new.server_vad = True
            if "threshold" in td:
                new.vad_threshold = float(td["threshold"])
            if "silence_duration_ms" in td:
                new.silence_ms = int(td["silence_duration_ms"])
                if new.silence_ms < 10:
                    raise ApiError("silence_duration_ms must be at least 10", "invalid_value",
                                   "session.audio.input.turn_detection.silence_duration_ms")
            if "prefix_padding_ms" in td:
                # Satisfied whatever the value: each turn's audio starts where
                # the previous turn's ended, so no audio before speech is dropped.
                new.prefix_padding_ms = int(td["prefix_padding_ms"])
        else:
            raise ApiError("turn_detection must be {type: 'server_vad'} or null",
                           "invalid_value", "session.audio.input.turn_detection")
    tuning = msg.get("x_tuning") if isinstance(msg.get("x_tuning"), dict) else {}
    new.stream_policy = _tuned_policy(tuning.get("stream"), new.stream_policy)
    new.vad_policy = _tuned_policy(tuning.get("vad"), new.vad_policy)
    return new


class Turn:
    """The item a stream of segment events belongs to, and its commit state."""

    def __init__(self):
        self.item_id = _new_id("item")
        self.committed = False


async def _close_unavailable(ws: WebSocket, message: str) -> None:
    await ws.send_json({"type": "error", "event_id": _new_id("event"),
                        "error": ApiError(message, "backend_unavailable",
                                          kind="server_error", status=503).body})
    await ws.close(code=1013)


@app.websocket("/v1/realtime")
async def realtime(ws: WebSocket):
    await ws.accept()
    if not (_ready and _alive()):
        await _close_unavailable(ws, f"Transcription engine is unavailable, retry after {RETRY_AFTER_S}s.")
        return
    if _sessions.locked():
        await _close_unavailable(ws, f"Server is at its limit of {MAX_SESSIONS} sessions.")
        return
    async with _sessions:
        await _realtime_session(ws)


async def _realtime_session(ws: WebSocket) -> None:
    sid = _new_id("sess")
    cfg = RealtimeConfig()
    out_lock = asyncio.Lock()

    async def send(event: dict) -> None:
        event = {"event_id": _new_id("event"), **event}
        async with out_lock:
            await ws.send_json(event)

    async def send_error(e: ApiError, client_event_id=None) -> None:
        body = dict(e.body, event_id=client_event_id)
        await send({"type": "error", "error": body})

    await send({"type": "session.created", "session": cfg.session_json(sid)})

    work: asyncio.Queue = asyncio.Queue()
    state = {"session": None, "resampler": Resampler(cfg.rate), "turn": Turn(),
             "previous_item_id": None, "buffered": False}

    def make_session() -> Session:
        overrides = {}
        if cfg.vad_threshold is not None:
            overrides["speech_threshold"] = cfg.vad_threshold
        if cfg.server_vad:
            overrides["min_silence_frame"] = max(1, cfg.silence_ms // 10)
        return Session(_engine, _vad, cfg.language, REALTIME_STEP_MS, cfg.server_vad,
                       cfg.stream_policy, cfg.vad_policy, cfg.prompt, overrides)

    async def commit_turn() -> None:
        turn = state["turn"]
        turn.committed = True
        await send({"type": "input_audio_buffer.committed", "item_id": turn.item_id,
                    "previous_item_id": state["previous_item_id"]})
        state["previous_item_id"] = turn.item_id

    async def emit(events: list[Event]) -> None:
        for e in events:
            turn = state["turn"]
            if e.kind == "speech_started":
                await send({"type": "input_audio_buffer.speech_started",
                            "audio_start_ms": e.ms, "item_id": turn.item_id})
            elif e.kind == "speech_stopped":
                await send({"type": "input_audio_buffer.speech_stopped",
                            "audio_end_ms": e.ms, "item_id": turn.item_id})
            elif e.kind == "delta":
                await send({"type": "conversation.item.input_audio_transcription.delta",
                            "item_id": turn.item_id, "content_index": 0,
                            "delta": to_simplified(e.text)})
            elif e.kind == "completed":
                if not turn.committed:  # closed by server VAD or the length rules
                    await commit_turn()
                event = {"type": "conversation.item.input_audio_transcription.completed",
                         "item_id": turn.item_id, "content_index": 0,
                         "transcript": to_simplified(e.text)}
                code = _iso(e.language)
                event["languages"] = [{"code": code}] if code else []
                await send(event)
                state["turn"] = Turn()

    async def processor() -> None:
        while (item := await work.get()) is not None:
            kind, payload = item
            if state["session"] is None:
                state["session"] = await _run(make_session)
            session = state["session"]
            if kind == "config":
                session.configure(cfg.language, cfg.server_vad, cfg.stream_policy,
                                  cfg.vad_policy, cfg.prompt)
            elif kind == "audio":
                await emit(await _run(session.feed, payload))
            elif kind == "commit":
                tail = state["resampler"](np.zeros(0, dtype=np.float32), last=True)
                state["resampler"] = Resampler(cfg.rate)
                if len(tail):
                    await emit(await _run(session.feed, tail))
                await commit_turn()
                await emit(await _run(session.finalize))
            elif kind == "clear":
                await _run(session.clear)
                state["turn"] = Turn()
                state["buffered"] = False
                await send({"type": "input_audio_buffer.cleared"})

    task = asyncio.create_task(processor())
    try:
        while not task.done():
            receive = asyncio.create_task(ws.receive_text())
            await asyncio.wait({receive, task}, return_when=asyncio.FIRST_COMPLETED)
            if not receive.done():
                receive.cancel()
                break
            try:
                msg = json.loads(receive.result())
            except ValueError:
                msg = None
            if not isinstance(msg, dict):
                await send_error(ApiError("Events must be JSON objects", "invalid_json"))
                continue
            kind, client_id = msg.get("type"), msg.get("event_id")
            try:
                if kind == "session.update":
                    new = apply_session_update(msg, cfg)
                    if new.rate != cfg.rate and state["buffered"]:
                        raise ApiError("Cannot change the input rate with audio buffered; "
                                       "commit or clear first", "invalid_value",
                                       "session.audio.input.format.rate")
                    if new.rate != cfg.rate:
                        state["resampler"] = Resampler(new.rate)
                    cfg = new
                    await work.put(("config", None))
                    await send({"type": "session.updated", "session": cfg.session_json(sid)})
                elif kind == "input_audio_buffer.append":
                    try:
                        raw = base64.b64decode(msg.get("audio", ""), validate=True)
                    except (ValueError, TypeError):
                        raise ApiError("audio must be base64-encoded PCM16", "invalid_value",
                                       "audio")
                    if len(raw) % 2:
                        raise ApiError("audio must be whole 16-bit samples", "invalid_value",
                                       "audio")
                    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                    if len(pcm):
                        state["buffered"] = True
                        await work.put(("audio", state["resampler"](pcm)))
                elif kind == "input_audio_buffer.commit":
                    if not state["buffered"]:
                        raise ApiError("Error committing input audio buffer: the buffer is empty.",
                                       "input_audio_buffer_commit_empty")
                    state["buffered"] = False
                    await work.put(("commit", None))
                elif kind == "input_audio_buffer.clear":
                    await work.put(("clear", None))
                else:
                    raise ApiError(f"Unsupported event type {kind!r}", "invalid_event", "type")
            except ApiError as e:
                await send_error(e, client_id)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await work.put(None)
        try:
            await asyncio.wait_for(task, timeout=30)
        except (asyncio.TimeoutError, WebSocketDisconnect, RuntimeError):
            task.cancel()
        except EngineDead:
            try:
                await _close_unavailable(ws, "Transcription engine failed; the server is restarting.")
            except RuntimeError:
                pass
        except Exception:
            log.exception("realtime session %s failed", sid)
            try:
                await send_error(ApiError("Session failed; reconnect.", "server_error",
                                          kind="server_error", status=500))
                await ws.close(code=1011)
            except RuntimeError:
                pass


# --- health ---

@app.get("/health")
async def health():
    if not _alive():
        return JSONResponse(status_code=503, content={"status": "engine dead"})
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    if not (_ready and _alive()):
        return JSONResponse(status_code=503, content={"status": "not ready"})
    return {"status": "ready"}
