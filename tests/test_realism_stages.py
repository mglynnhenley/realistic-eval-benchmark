"""Paid work reserves before starting and never disappears after analysis fails."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from refund_eval.realism import stages


def test_reservations_cover_every_paid_stage_and_unknown_usage_stays_held(tmp_path):
    ledger = tmp_path / "spend.json"
    stages.reserve(ledger, 20, "trials", "trials", 10)
    stages.finish(ledger, 20, "trials", "complete", 8)
    stages.reserve(ledger, 20, "tells", "tells", 5)
    stages.finish(ledger, 20, "tells", "failed")
    stages.reserve(ledger, 20, "check", "interaction", 3)
    stages.finish(ledger, 20, "check", "unknown")
    result = stages.show(ledger, 20)
    assert (result["actual_usd"], result["held_usd"], result["remaining_usd"]) == (8, 8, 4)
    assert result["stages"]["tells"]["status"] == "failed"
    # Unpaid postprocessing can reconcile cost without permitting another paid start.
    result = stages.finish(ledger, 20, "tells", "failed", 2)
    assert result["held_usd"] == 3 and result["actual_usd"] == 10
    with pytest.raises(ValueError, match="already started"):
        stages.reserve(ledger, 20, "tells", "tells", 5)


def test_overrun_is_recorded_even_when_it_exceeds_budget(tmp_path):
    ledger = tmp_path / "spend.json"
    stages.reserve(ledger, 10, "one", "trials", 10)
    result = stages.finish(ledger, 10, "one", "complete", 12)
    assert result["remaining_usd"] == -2
    with pytest.raises(ValueError, match="Insufficient"):
        stages.reserve(ledger, 10, "two", "tells", 1)


def test_concurrent_reservations_cannot_overspend_or_start_same_stage_twice(tmp_path):
    ledger = tmp_path / "spend.json"

    def start(stage):
        try:
            stages.reserve(ledger, 10, stage, "trials", 7)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=4) as workers:
        assert sum(workers.map(start, ["one", "one", "two", "three"])) == 1
    assert stages.show(ledger, 10)["held_usd"] == 7
    assert len(json.loads(ledger.read_text())["stages"]) == 1


@pytest.mark.parametrize("amount", [-1, 0, float("inf"), float("nan")])
def test_invalid_reservations_are_rejected(tmp_path, amount):
    with pytest.raises(ValueError):
        stages.reserve(tmp_path / "s.json", 20, "one", "tells", amount)


def test_budget_and_recorded_actual_are_immutable(tmp_path):
    ledger = tmp_path / "s.json"
    stages.reserve(ledger, 20, "one", "trials", 10)
    with pytest.raises(ValueError, match="Budget differs"):
        stages.show(ledger, 30)
    stages.finish(ledger, 20, "one", "complete", 4)
    with pytest.raises(ValueError, match="immutable"):
        stages.finish(ledger, 20, "one", "complete", 0)
    assert stages.finish(ledger, 20, "one", "complete", 4)["actual_usd"] == 4


def test_cli_outputs_accounting_and_duplicate_start_fails(tmp_path, capsys):
    common = ["--ledger", str(tmp_path / "s.json"), "--budget", "20"]
    action = ["reserve", "--stage", "one", "--kind", "tells", "--reserve", "3"]
    assert stages.main(common + action) == 0
    assert json.loads(capsys.readouterr().out)["held_usd"] == 3
    with pytest.raises(SystemExit) as error:
        stages.main(common + action)
    assert error.value.code == 1
