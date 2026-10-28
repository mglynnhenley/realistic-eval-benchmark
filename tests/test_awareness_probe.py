from __future__ import annotations

import random

import numpy as np
import pytest

from refund_eval.awareness.probe import Probe, windows


def test_score_and_save_load_round_trip(tmp_path):
    probe = Probe("Qwen/Qwen3-32B", 62, np.array([2.0, 0.0]), 1.0, 1.0, accuracy=0.95, pool="mean", source="u")
    acts = np.array([[1.0, 5.0], [0.0, 5.0]])
    np.testing.assert_allclose(probe.score(acts), [1.0, -1.0])
    path = tmp_path / "probe.json"
    probe.save(path)
    back = Probe.load(path)
    assert back.to_dict() == probe.to_dict() and (back.pool, back.source) == ("mean", "u")
    np.testing.assert_allclose(back.score(acts), probe.score(acts))
    old = {k: v for k, v in probe.to_dict().items() if k not in ("pool", "source")}
    assert Probe.from_dict({**old, "accuracy": None}).pool == "last"


def _check(n_tokens: int, positions: list[int], window: int) -> list[tuple[int, int, list[int]]]:
    plan = windows(n_tokens, positions, window)
    seen = [p for _, _, read in plan for p in read]
    assert seen == sorted(set(positions))
    for i, (start, end, read) in enumerate(plan):
        assert read and read == sorted(read)
        assert 0 <= start < end <= n_tokens and end - start <= window
        for p in read:
            assert start <= p < end
            assert p - start >= min(p, window // 2)
        if i:
            assert start > plan[i - 1][0]
    return plan


def test_windows_invariants_random():
    rng = random.Random(0)
    for _ in range(2000):
        n_tokens = rng.randint(1, 400)
        window = rng.randint(2, 120)
        k = rng.randint(0, n_tokens)
        positions = rng.choices(range(n_tokens), k=k) if rng.random() < 0.3 else rng.sample(range(n_tokens), k)
        _check(n_tokens, positions, window)


def test_windows_dense_chunk_count():
    for n_tokens, window in [(10_000, 512), (8192 * 5, 8192), (1000, 7), (999, 2)]:
        plan = _check(n_tokens, list(range(n_tokens)), window)
        assert abs(len(plan) - n_tokens / (window // 2)) <= 2  # stride window // 2: ~2 * n_tokens / window


def test_windows_edges():
    assert windows(100, [], 10) == []
    assert windows(5, [0, 1, 4], 64) == [(0, 5, [0, 1, 4])]
    assert windows(100, [3, 0], 10) == [(0, 4, [0, 3])]
    with pytest.raises(ValueError):
        windows(10, [1], 1)
    with pytest.raises(ValueError):
        windows(10, [10], 4)
    with pytest.raises(ValueError):
        windows(10, [-1], 4)
