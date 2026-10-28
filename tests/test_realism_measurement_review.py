"""Independent integration review of exact-commit review receipts; no paid calls."""
# ruff: noqa: F811 -- shared pytest fixtures are injected by parameter name
import json

from test_realism_measurement import (  # noqa: F401 -- shared temporary-git and no-network fixtures
    adapters,
    job,
)

from refund_eval.realism import measurement


def test_symbolic_receipt_cannot_approve_a_moving_candidate(job, adapters):
    receipt = json.loads(job["review"].read_text())
    receipt.update(baseline="HEAD~1", candidate="HEAD")
    job["review"].write_text(json.dumps(receipt))
    result = measurement.run_measurement(**job)
    assert result["status"] == "rejected"
    assert not adapters["trials"]
