"""Speaker diarization sidecar for r2t2-asr: Nemotron 3 Diarization on CPU.

Runs next to asr-server.py in the same pod and is only called by it, over localhost. The ASR
server writes the decoded upload as a 16 kHz mono WAV to a volume both containers mount and
posts its path; this returns who spoke when. Words and their speakers are put together on the
ASR side, which has the word times.

Memory stays bounded however long the recording is: features are computed in pieces and fed to
the model's own streaming loop (speaker cache + FIFO), so a returning speaker keeps its label.
Whole-file diarize() ran the STFT over the entire recording, about 2.5 GiB per hour.

The model has 8 output channels. When all 8 are used, a channel may hold more than one voice, so
each channel's solo speech is embedded with CAM++ (3D-Speaker, Mandarin) and clustered; speech
outside the channel's main cluster is returned as speaker -1 (unknown). On AliMeeting meetings
joined to 9-12 speakers this caught 91-99% of the solo windows put on the wrong channel, and
marked none of an 8-speaker control. The ASR side also calls long speech outside every turn unknown.
"""

import asyncio
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

log = logging.getLogger("uvicorn.error")

SR = 16000
MODEL_PATH = os.getenv("DIARIZER_MODEL_PATH", "/models/Nemotron-3-Diarization/Nemotron-3-Diarization.nemo")
THREADS = int(os.getenv("DIARIZER_THREADS", "4"))
# Only files under this directory are read; it is the volume shared with the ASR container.
SHARED_DIR = Path(os.getenv("DIARIZER_SHARED_DIR", "/shared-audio")).resolve()
MAX_SPEAKERS = 8  # output channels of the model
# Feature extraction piece and the context kept on each side of it (features are frame-local).
PIECE_S = 300
PAD_S = 1.0
# "Very high latency (offline)" row of the model card: 30.4 s input buffer. Measured on
# AliMeeting Test: speaker confusion 0.4-0.6%, labels kept across a 3.4 h file of 7 speakers.
STREAMING = dict(spkcache_len=264, fifo_len=40, chunk_len=340, chunk_right_context=40,
                 spkcache_update_period=300)
# Speaker embedder for the all-channels-used check; its model code (speakerlab) is on PYTHONPATH.
EMBEDDER_PATH = os.getenv("DIARIZER_EMBEDDER_PATH", "/opt/campplus/campplus_cn_common.bin")
UNKNOWN = -1
# Solo speech shorter than this embeds poorly; longer pieces are cut into ~WIN_S windows.
MIN_S, WIN_S = 1.5, 3.0
# Average-linkage cut, cosine distance. 0.6 marked 0.5% of an 8-speaker control, 0.7 caught
# only 74% on 12 speakers.
SPLIT_TAU = 0.65

_model = None
_embedder = None
_pp = None
# One recording at a time: inference already uses every thread the container has.
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="diar")
_busy = asyncio.Lock()


def _load() -> None:
    global _model, _embedder, _pp
    import torch
    from nemo.collections.asr.models import SortformerEncLabelModel
    from nemo.collections.asr.parts.utils.vad_utils import PostProcessingParams
    from omegaconf import OmegaConf
    from speakerlab.models.campplus.DTDNN import CAMPPlus

    torch.set_num_threads(THREADS)
    m = SortformerEncLabelModel.restore_from(MODEL_PATH, map_location="cpu", strict=False)
    m.eval()
    for k, v in STREAMING.items():
        setattr(m.sortformer_modules, k, v)
    m._check_streaming_parameters()
    e = CAMPPlus(feat_dim=80, embedding_size=192)
    e.load_state_dict(torch.load(EMBEDDER_PATH, map_location="cpu"))
    _model, _embedder, _pp = m, e.eval(), OmegaConf.structured(PostProcessingParams())
    _diarize_file_samples(np.zeros(SR * 2, dtype=np.float32))  # warm-up
    _embed(np.zeros(SR * 2, dtype=np.float32))


def cap_speakers(preds: np.ndarray, k: int, threshold: float = 0.5) -> np.ndarray:
    """Keep the k channels with the most speech; give each dropped channel's frames to the kept
    channel the model rates most likely there. The caller says how many people can speak."""
    if k >= preds.shape[1]:
        return preds
    active = (preds > threshold).sum(axis=0)
    keep = np.argsort(-active, kind="stable")[:k]
    out = np.zeros_like(preds)
    out[:, keep] = preds[:, keep]
    for d in sorted(set(range(preds.shape[1])) - set(keep.tolist())):
        frames = np.nonzero(preds[:, d] > threshold)[0]
        if len(frames):
            target = keep[np.argmax(preds[frames][:, keep], axis=1)]
            out[frames, target] = np.maximum(out[frames, target], preds[frames, d])
    return out


def solo_windows(turns: list[tuple[float, float, int]]) -> list[tuple[float, float, int]]:
    """Where exactly one speaker is active, cut into ~WIN_S windows; pieces under MIN_S dropped."""
    events = sorted((t, d, s) for b, e, s in turns for t, d in ((b, 1), (e, -1)))
    active: dict[int, int] = {}
    pieces: list[list] = []
    prev = None
    for t, d, s in events:
        if prev is not None and t > prev and len(active) == 1:
            (only,) = active
            if pieces and pieces[-1][2] == only and pieces[-1][1] == prev:
                pieces[-1][1] = t
            else:
                pieces.append([prev, t, only])
        active[s] = active.get(s, 0) + d
        if not active[s]:
            del active[s]
        prev = t
    out = []
    for b, e, s in pieces:
        if e - b >= MIN_S:
            k = max(1, round((e - b) / WIN_S))
            step = (e - b) / k
            out += [(round(b + i * step, 2), round(b + (i + 1) * step, 2), s) for i in range(k)]
    return out


def split_slots(turns: list[tuple[float, float, int]], windows: list[tuple[float, float, int]],
                emb: np.ndarray, tau: float = SPLIT_TAU) -> list[tuple[float, float, int]]:
    """Per speaker, cluster its windows' embeddings; windows outside the longest cluster become
    UNKNOWN turns, carved out of the speaker's turns (solo windows overlap no other turn)."""
    from scipy.cluster.hierarchy import fcluster, linkage

    unknown = []
    for spk in sorted({w[2] for w in windows}):
        idx = [i for i, w in enumerate(windows) if w[2] == spk]
        if len(idx) < 2:
            continue
        labels = fcluster(linkage(emb[idx], "average", metric="cosine"), tau, "distance")
        dur: dict[int, float] = {}
        for i, lab in zip(idx, labels):
            dur[lab] = dur.get(lab, 0.0) + windows[i][1] - windows[i][0]
        main = max(dur, key=dur.get)
        unknown += [windows[i][:2] for i, lab in zip(idx, labels) if lab != main]
    if not unknown:
        return turns
    merged: list[list] = []
    for b, e in sorted(unknown):
        if merged and b <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([b, e])
    out = []
    for b, e, s in turns:
        pieces = [(b, e)]
        for ub, ue in merged:
            pieces = [p for x, y in pieces for p in ((x, min(y, ub)), (max(x, ue), y)) if p[1] > p[0]]
        out += [(x, y, s) for x, y in pieces]
    return sorted(out + [(b, e, UNKNOWN) for b, e in merged])


def _embed(x: np.ndarray) -> np.ndarray:
    """Unit-norm CAM++ embedding of 16 kHz mono float32 audio."""
    import torch
    import torchaudio.compliance.kaldi as kaldi

    with torch.inference_mode():
        f = kaldi.fbank(torch.from_numpy(x)[None], num_mel_bins=80, sample_frequency=SR, dither=0)
        e = _embedder((f - f.mean(0, keepdim=True))[None])[0].numpy()
    return e / np.linalg.norm(e)


def _features(read, n: int):
    """Log-mel features in PIECE_S pieces, each with PAD_S of context trimmed off again."""
    import torch

    m = _model
    hop = m.cfg.preprocessor.window_stride * SR
    total = math.floor(n / hop) + 1
    feats, pos = None, 0
    for start in range(0, n, PIECE_S * SR):
        a, b = max(0, start - int(PAD_S * SR)), min(n, start + PIECE_S * SR + int(PAD_S * SR))
        x = torch.from_numpy(read(a, b))[None]
        fx, _ = m.preprocessor(input_signal=x, length=torch.tensor([x.shape[1]]))
        skip = round((start - a) / hop)
        keep = round((min(n, start + PIECE_S * SR) - start) / hop)
        fx = fx[:, :, skip:skip + keep]
        if feats is None:
            feats = torch.empty((1, fx.shape[1], total), dtype=fx.dtype)
        k = min(fx.shape[2], total - pos)
        feats[:, :, pos:pos + k] = fx[:, :, :k]
        pos += k
    return feats[:, :, :pos]


def _diarize(read, n: int, max_speakers: int | None) -> tuple[list[tuple[float, float, int]], bool]:
    """Turns, and whether all channels were used (then some speech may be UNKNOWN). A caller-set
    max_speakers already says how many people there are, so no check runs then."""
    import torch
    from nemo.collections.asr.parts.utils.vad_utils import predlist_to_timestamps

    m = _model
    with torch.inference_mode():
        feats = _features(read, n)
        preds = m.forward_streaming(feats, torch.tensor([feats.shape[2]]))
    preds = preds[0, : math.ceil(feats.shape[2] / m.output_subsampling_factor)].numpy()
    if max_speakers:
        preds = cap_speakers(preds, max_speakers)
    stamps = predlist_to_timestamps([torch.from_numpy(preds)[None]],
                                    {"rec": {"offset": 0.0, "duration": n / SR}}, _pp,
                                    unit_10ms_frame_count=m.output_subsampling_factor)[0]
    turns = sorted((round(float(b), 2), round(float(e), 2), spk)
                   for spk, segs in enumerate(stamps) for b, e in segs if e > b)
    full = not max_speakers and len({s for _, _, s in turns}) == MAX_SPEAKERS
    if full:
        t = time.monotonic()
        win = solo_windows(turns)
        try:
            if win:
                emb = np.stack([_embed(read(int(b * SR), int(e * SR))) for b, e, _ in win])
                turns = split_slots(turns, win, emb)
        except Exception:
            log.exception("speaker check failed; returning turns unchecked")
        log.info("all %d channels used: embedded %d windows in %.1f s, %.0f s unknown",
                 MAX_SPEAKERS, len(win), time.monotonic() - t,
                 sum(e - b for b, e, s in turns if s == UNKNOWN))
    return turns, full


def _diarize_file_samples(x: np.ndarray):
    return _diarize(lambda a, b: x[a:b], len(x), None)


def _diarize_path(path: Path, max_speakers: int | None):
    with sf.SoundFile(path) as f:
        if f.samplerate != SR or f.channels != 1:
            raise ValueError(f"expected 16 kHz mono, got {f.samplerate} Hz x {f.channels}")

        def read(a, b):
            f.seek(a)
            return f.read(b - a, dtype="float32")

        return *_diarize(read, f.frames, max_speakers), f.frames / SR


@asynccontextmanager
async def _lifespan(_app):
    await asyncio.get_running_loop().run_in_executor(_pool, _load)
    yield


app = FastAPI(title="r2t2-asr diarizer", lifespan=_lifespan)


class DiarizeRequest(BaseModel):
    path: str
    max_speakers: int | None = Field(default=None, ge=1, le=MAX_SPEAKERS)


@app.post("/v1/diarize")
async def diarize(req: DiarizeRequest):
    """{"turns": [[start, end, speaker], ...], "speakers": n, "speaker_limit_reached": bool}.
    Speakers are numbered in order of first speech; -1 is speech that matched none of them."""
    path = Path(req.path).resolve()
    if SHARED_DIR not in path.parents or not path.is_file():
        return JSONResponse(status_code=400, content={"error": "path must be a file in the shared directory"})
    async with _busy:
        t = time.monotonic()
        try:
            turns, full, duration = await asyncio.get_running_loop().run_in_executor(
                _pool, _diarize_path, path, req.max_speakers)
        except ValueError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
    log.info("diarized %.0f s in %.1f s, %d turns", duration, time.monotonic() - t, len(turns))
    return {"turns": turns, "speakers": len({s for _, _, s in turns if s != UNKNOWN}),
            "speaker_limit_reached": full}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    if _model is None:
        return JSONResponse(status_code=503, content={"status": "loading"})
    return {"status": "ready"}
