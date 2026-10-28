"""Probe scoring and its JSON round trip."""

from __future__ import annotations

import numpy as np

from refund_eval.awareness.probe import Probe


def test_score_and_save_load_round_trip(tmp_path):
    probe = Probe("Qwen/Qwen3-32B", 62, np.array([2.0, 0.0]), 1.0, accuracy=0.95, source="u")
    acts = np.array([[1.0, 5.0], [0.0, 5.0]])
    np.testing.assert_allclose(probe.score(acts), [1.0, -1.0])
    path = tmp_path / "probe.json"
    probe.save(path)
    back = Probe.load(path)
    assert back.to_dict() == probe.to_dict() and back.source == "u"
    np.testing.assert_allclose(back.score(acts), probe.score(acts))
    assert Probe.from_dict({**probe.to_dict(), "accuracy": None}).accuracy is None
