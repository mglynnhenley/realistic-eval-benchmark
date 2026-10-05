"""A campaign launches only its three pilots and never duplicates ambiguous starts."""

import json

import pytest

from refund_eval.experiments import campaign, review


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    protocol = {
        "shared_settings": {"all_role_cost_limit": 13}, "budget_usd": 40,
        "run_order": ["B", "A", "C"], "rubric_version": "frozen",
        "variants": {"arms": {arm: {"manifest_path": arm, "manifest": {"arm": arm}} for arm in "ABC"}},
    }
    campaign.save(tmp_path / "campaign.json", protocol)
    monkeypatch.setattr(campaign, "load_variant", lambda path: {"arm": path})
    monkeypatch.setattr(review, "analyze_variant_run", lambda snapshot, manifest: {"controls": {"arm": manifest["arm"]}})
    calls = []

    def fake_request(path, payload=None):
        if path == "/api/behaviours":
            return {"version": "frozen"}
        if path == "/api/runs" and payload:
            calls.append(payload)
            return {"run": {"id": payload["experiment_manifest"], "status": "starting"}}
        if path == "/api/runs":
            return {"runs": [{"id": arm, "status": "completed", "turns": 5, "stop_reason": "max_turns"} for arm in "ABC"]}
        return {"snapshot": {"long_horizon": {"summary": {"tool_calls": 4}}}}

    monkeypatch.setattr(campaign, "request", fake_request)
    return tmp_path, calls


def test_three_pilots_then_review_gate_and_no_duplicate_resume(prepared):
    directory, calls = prepared
    result = campaign.run_pilots(directory)
    assert [call["experiment_manifest"] for call in calls] == ["B", "A", "C"]
    assert result["status"] == "pilots_complete_awaiting_evidence_review"
    assert (directory / "comparison.csv").exists()
    assert len(list(directory.glob("pilot-*.json"))) == 3
    campaign.run_pilots(directory)
    assert len(calls) == 3


def test_ambiguous_prior_start_blocks_all_further_launches(prepared):
    directory, calls = prepared
    campaign.save(directory / "progress.json", {"runs": {"A": {"id": None, "status": "launch_pending"}}})
    with pytest.raises(ValueError, match="no confirmed run ID"):
        campaign.run_pilots(directory)
    assert not calls


def test_insufficient_campaign_budget_blocks_launch(prepared):
    directory, calls = prepared
    protocol = json.loads((directory / "campaign.json").read_text())
    protocol["budget_usd"] = 38
    campaign.save(directory / "campaign.json", protocol)
    with pytest.raises(ValueError, match="exceed"):
        campaign.run_pilots(directory)
    assert not calls


def test_all_variants_validated_before_first_paid_run(prepared, monkeypatch):
    directory, calls = prepared

    def changed_variant(path):
        if path == "C":
            raise ValueError("C changed")

    monkeypatch.setattr(campaign, "load_variant", changed_variant)
    with pytest.raises(ValueError, match="C changed"):
        campaign.run_pilots(directory)
    assert not calls


def test_new_campaign_pins_simulated_gmail_and_live_web_without_starting_trials(tmp_path, monkeypatch):
    from refund_eval.experiments import budget

    monkeypatch.setattr(budget, "configure_budget_models", lambda models: {"offline": True})
    calls = []
    def public_read(path, payload=None):
        calls.append((path, payload))
        assert path == "/api/behaviours" and payload is None
        return {"version": "offline-rubrics"}
    monkeypatch.setattr(campaign, "request", public_read)
    result = campaign.prepare(tmp_path / "new-campaign", 40)
    assert result["shared_settings"]["data_mode"] == "hybrid"
    assert result["shared_settings"]["tool_profile"] == "published"
    assert result["shared_settings"]["world_mode"] == "friction"
    assert calls == [("/api/behaviours", None)]
    assert all(arm["manifest"]["startup_context"]["mode"] == "matched_thread_prefetch"
               for arm in result["variants"]["arms"].values())
