"""An authorized increase changes a ceiling, never the existing paid history."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from refund_eval.realism import stages


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ledger(tmp_path):
    path = tmp_path / "spend.json"
    stages.reserve(path, 10.65, "old-trials", "trials", 3)
    stages.finish(path, 10.65, "old-trials", "complete", 2)
    stages.reserve(path, 10.65, "unknown-scan", "tells", 1)
    return path


def amend(path, **kwargs):
    return stages.amend_budget(path, previous=10.65, total=100,
                               expected_sha256=digest(path),
                               authorization="owner-message-100-total", reason="Owner raised total ceiling",
                               **kwargs)


def test_amendment_preserves_actuals_unknown_holds_and_paid_start_sentinels(tmp_path):
    path = ledger(tmp_path)
    before = json.loads(path.read_text())
    old_digest = digest(path)
    result = amend(path)
    assert result["stages"] == before["stages"]
    assert (result["actual_usd"], result["held_usd"], result["remaining_usd"]) == (2, 1, 97)
    event = result["budget_amendments"][0]
    assert event["previous_ledger_sha256"] == old_digest
    assert event["previous_budget_usd"] == 10.65 and event["budget_usd"] == 100
    assert len(event["id"]) == 64
    with pytest.raises(ValueError, match="Budget differs"):
        stages.show(path, 10.65)
    with pytest.raises(ValueError, match="already started"):
        stages.reserve(path, 100, "old-trials", "trials", 1)


@pytest.mark.parametrize("total", [True, False, 0, -1, 10.65, 5, float("nan"), float("inf")])
def test_invalid_total_does_not_touch_history(tmp_path, total):
    path = ledger(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        stages.amend_budget(path, previous=10.65, total=total, expected_sha256=digest(path),
                            authorization="owner", reason="increase")
    assert path.read_bytes() == before


def test_stale_digest_and_missing_authorization_fail_without_mutation(tmp_path):
    path = ledger(tmp_path)
    before = path.read_bytes()
    for expected, authorization in [("0" * 64, "owner"), (digest(path), " ")]:
        with pytest.raises(ValueError):
            stages.amend_budget(path, previous=10.65, total=100, expected_sha256=expected,
                                authorization=authorization, reason="increase")
        assert path.read_bytes() == before


def test_concurrent_amendments_cannot_apply_twice(tmp_path):
    path = ledger(tmp_path)
    expected = digest(path)

    def change(_):
        try:
            stages.amend_budget(path, previous=10.65, total=100, expected_sha256=expected,
                                authorization="owner", reason="increase")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(change, range(2))) == 1
    assert len(stages.show(path, 100)["budget_amendments"]) == 1


def test_cli_amendment_uses_old_total_as_compare_and_check(tmp_path, capsys):
    path = ledger(tmp_path)
    assert stages.main(["--ledger", str(path), "--budget", "10.65", "amend-budget",
                        "--total", "100", "--expected-sha256", digest(path),
                        "--authorization", "owner-message", "--reason", "explicit increase"]) == 0
    assert json.loads(capsys.readouterr().out)["budget_usd"] == 100
