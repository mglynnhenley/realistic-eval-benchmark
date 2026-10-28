"""Published probes: conversion of a two-class linear head into a `Probe` (no download)."""

from __future__ import annotations

import numpy as np
import pytest

from refund_eval.awareness.published import DEFAULT, PUBLISHED, convert


def test_convert_scores_the_aware_logit_margin():
    weight, bias = np.array([[0.5, -1.0, 2.0], [1.5, 1.0, 0.0]]), np.array([0.3, -0.2])
    probe = convert(weight, bias, model="Qwen/Qwen3-32B", hidden_index=63, accuracy=0.951, source="u")
    assert (probe.model, probe.layer, probe.accuracy, probe.source) == ("Qwen/Qwen3-32B", 62, 0.951, "u")
    acts = np.random.default_rng(0).normal(size=(5, 3))
    logits = acts @ weight.T + bias
    np.testing.assert_allclose(probe.score(acts), logits[:, 1] - logits[:, 0])
    with pytest.raises(ValueError, match="embeddings"):
        convert(weight, bias, model="m", hidden_index=0)


def test_catalogue():
    assert DEFAULT in PUBLISHED
    assert {model for model, *_ in PUBLISHED.values()} == {
        "Qwen/Qwen3-32B", "Qwen/QwQ-32B", "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"}
