"""Diarizer sidecar logic that needs no model. Runs with numpy, soundfile, fastapi, pydantic."""

import importlib.util
from pathlib import Path

import numpy as np
import pytest

SERVER = Path(__file__).resolve().parents[1] / "base" / "diarizer-server.py"


@pytest.fixture(scope="module")
def ds():
    spec = importlib.util.spec_from_file_location("diarizer_server", SERVER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_cap_speakers_folds_the_quietest_channel_into_the_likeliest_kept_one(ds):
    p = np.zeros((10, 3), dtype=np.float32)
    p[0:5, 0] = 0.9           # speaker 0: 5 frames
    p[5:9, 1] = 0.9           # speaker 1: 4 frames
    p[9, 2] = 0.8             # speaker 2: 1 frame, closer to speaker 1 there
    p[9, 1] = 0.3
    out = ds.cap_speakers(p, 2)
    assert (out[:, 2] == 0).all()
    assert out[9, 1] == pytest.approx(0.8) and out[9, 0] == 0
    assert np.array_equal(out[:9], p[:9])
    assert ds.cap_speakers(p, 3) is p
