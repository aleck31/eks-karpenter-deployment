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
        # Samples the VAD has seen since its last reset, to place its frames.
        self.vad_samples = 0
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
                events.append(Event("speech_started", ms=self.vad_base_ms + max(0, r.speech_start_frame - 1) * 10))
            if r.is_speech_end:
                events.append(Event("speech_stopped", ms=self.vad_base_ms + max(0, r.speech_end_frame - 1) * 10))
                # speech_end_frame is where the end was *detected*, after
                # min_silence frames of silence; the smoothing window lags it
                # by a few frames more, so step back by that margin.
                at = (max(0, r.speech_end_frame - 1) * 10 - VAD_CUT_MARGIN_MS) * SR // 1000
                cut = min(len(seg), max(0, at - start))
                if cut < MIN_DECODE_SAMPLES:
                    cut = 0  # too little to decode alone; it goes to the next turn
        return events, cut

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
            try:
                events = [e for e in self.feed(np.zeros(int(pad_s * SR), dtype=np.float32))
                          if e.kind == "delta"]
            finally:
                self._closing = False
                self.audio_ms -= int(pad_s * 1000)  # padding is not client audio
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
_batcher: Batcher | None = None
_ready = False
_pool = ThreadPoolExecutor(max_workers=WORKER_THREADS, thread_name_prefix="r2t2")
_sessions = asyncio.Semaphore(MAX_SESSIONS)


def _load_runtime() -> None:
    """Load model and VAD, then warm up. Replaced wholesale in tests."""
    global _engine, _vad, _batcher
    from r2t2 import R2T2ASRModel

    asr = R2T2ASRModel.LLM(model=MODEL_PATH, gpu_memory_utilization=GPU_MEMORY_UTILIZATION,
                           kv_cache_memory_bytes=KV_CACHE_BYTES, max_model_len=MAX_MODEL_LEN,
                           max_new_tokens=4)
    _batcher = Batcher(asr.model)
    asr.model = _batcher
    _engine = Engine(asr)
    _vad = VadFactory(VAD_MODEL_PATH)
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
                         x_policy: str = Form(""), x_step_ms: str = Form("")):
    """OpenAI /v1/audio/transcriptions.

    `model` and `temperature` are accepted and ignored: one model, greedy
    decoding. `prompt` is passed to R2T2 as context. `x_*` fields are tuning
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
    audio = await _run(decode_audio, await file.read())
    if audio is None or len(audio) == 0:
        return _error_response(ApiError("Could not decode the uploaded audio.",
                                        "invalid_audio", "file"))
    duration = len(audio) / SR
    policy = _tuned_policy(x_policy, FILE_POLICY)
    step_ms = int(x_step_ms) if ALLOW_TUNING and x_step_ms else FILE_STEP_MS
    ranges = segment_bounds(duration, await _run(_vad.speech_spans, audio), policy)
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
    if response_format in ("srt", "vtt"):
        return PlainTextResponse(_subtitles(segments, response_format))
    return {"task": "transcribe", "language": (detected or "").lower() or None,
            "duration": round(duration, 2), "text": text, "segments": segments,
            "usage": usage}


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
