"""Speaker diarization sidecar for r2t2-asr: Nemotron 3 Diarization on CPU.

Runs next to asr-server.py in the same pod and is only called by it, over localhost. The ASR
server writes the decoded upload as a 16 kHz mono WAV to a volume both containers mount and
posts its path; this returns who spoke when. Words and their speakers are put together on the
ASR side, which has the word times.

Memory stays bounded however long the recording is: features are computed in pieces and fed to
the model's own streaming loop (speaker cache + FIFO), so a returning speaker keeps its label.
Whole-file diarize() ran the STFT over the entire recording, about 2.5 GiB per hour.
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

_model = None
_pp = None
# One recording at a time: inference already uses every thread the container has.
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="diar")
_busy = asyncio.Lock()


def _load() -> None:
    global _model, _pp
    import torch
    from nemo.collections.asr.models import SortformerEncLabelModel
    from nemo.collections.asr.parts.utils.vad_utils import PostProcessingParams
    from omegaconf import OmegaConf

    torch.set_num_threads(THREADS)
    m = SortformerEncLabelModel.restore_from(MODEL_PATH, map_location="cpu", strict=False)
    m.eval()
    for k, v in STREAMING.items():
        setattr(m.sortformer_modules, k, v)
    m._check_streaming_parameters()
    _model, _pp = m, OmegaConf.structured(PostProcessingParams())
    _diarize_file_samples(np.zeros(SR * 2, dtype=np.float32))  # warm-up


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


def _diarize(read, n: int, max_speakers: int | None) -> list[tuple[float, float, int]]:
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
    return sorted((round(float(b), 2), round(float(e), 2), spk)
                  for spk, segs in enumerate(stamps) for b, e in segs if e > b)


def _diarize_file_samples(x: np.ndarray):
    return _diarize(lambda a, b: x[a:b], len(x), None)


def _diarize_path(path: Path, max_speakers: int | None):
    with sf.SoundFile(path) as f:
        if f.samplerate != SR or f.channels != 1:
            raise ValueError(f"expected 16 kHz mono, got {f.samplerate} Hz x {f.channels}")

        def read(a, b):
            f.seek(a)
            return f.read(b - a, dtype="float32")

        return _diarize(read, f.frames, max_speakers), f.frames / SR


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
    """{"turns": [[start, end, speaker], ...]} with speakers numbered in order of first speech."""
    path = Path(req.path).resolve()
    if SHARED_DIR not in path.parents or not path.is_file():
        return JSONResponse(status_code=400, content={"error": "path must be a file in the shared directory"})
    async with _busy:
        t = time.monotonic()
        try:
            turns, duration = await asyncio.get_running_loop().run_in_executor(
                _pool, _diarize_path, path, req.max_speakers)
        except ValueError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
    log.info("diarized %.0f s in %.1f s, %d turns", duration, time.monotonic() - t, len(turns))
    return {"turns": turns, "speakers": len({s for _, _, s in turns})}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    if _model is None:
        return JSONResponse(status_code=503, content={"status": "loading"})
    return {"status": "ready"}
