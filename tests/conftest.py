"""Test-wide fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _offline_launch_model_check(monkeypatch):
    """Launch-time model checks never reach OpenRouter in tests: without an explicit catalog
    they see an empty one and allow the launch. The catalog itself is tested on its own."""
    from refund_eval.dashboard import models

    original = models.unknown_models
    monkeypatch.setattr(models, "unknown_models", lambda roles, catalog=None: original(
        roles, catalog if catalog is not None else {"models": [], "all_ids": []}))


@pytest.fixture(autouse=True)
def _no_reply_jitter(monkeypatch):
    """Exact reply times in tests; the jitter itself is tested on its own."""
    monkeypatch.setattr("refund_eval.sim.open_world.random.uniform", lambda a, b: 1.0)
