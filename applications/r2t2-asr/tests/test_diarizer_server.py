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


def test_solo_windows_skip_overlap_and_short_pieces(ds):
    turns = [(0.0, 7.0, 0), (6.0, 8.0, 1), (10.0, 11.0, 2)]
    # speaker 0 alone 0-6 s -> two 3 s windows; 1 alone 7-8 s and 2's 1 s turn are too short
    assert ds.solo_windows(turns) == [(0.0, 3.0, 0), (3.0, 6.0, 0)]


def test_split_slots_moves_a_second_voice_to_unknown(ds):
    turns = [(0.0, 12.0, 0), (20.0, 23.0, 1)]
    win = [(0.0, 3.0, 0), (3.0, 6.0, 0), (6.0, 9.0, 0), (9.0, 12.0, 0), (20.0, 23.0, 1)]
    a, b = np.eye(192)[0], np.eye(192)[1]
    emb = np.stack([a, a, b, a, b])  # window 6-9 s on speaker 0 is another voice
    out = ds.split_slots(turns, win, emb)
    assert out == [(0.0, 6.0, 0), (6.0, 9.0, ds.UNKNOWN), (9.0, 12.0, 0), (20.0, 23.0, 1)]
    same = np.stack([a, a, a, a, b])
    assert ds.split_slots(turns, win, same) == turns
