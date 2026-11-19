import builtins
import sys

import pytest

from refund_eval.models.evidence import screened_error
from refund_eval.published import REDACTED_SCENARIO_VALUE


def test_screened_error_returns_none_without_loading_privacy_helpers(monkeypatch):
    original_import = builtins.__import__

    def import_without_privacy(name, *args, **kwargs):
        if name == "refund_eval.published.privacy":
            raise AssertionError("privacy helper imported for an empty diagnostic")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_privacy)

    assert screened_error(None) is None


def test_screened_error_uses_full_runtime_privacy_check():
    from refund_eval.sim.common import current_scenario

    scenario = current_scenario()
    assert screened_error(f"Provider diagnostic for {scenario.user.name}") == REDACTED_SCENARIO_VALUE
    assert screened_error("provider unavailable") == "provider unavailable"


@pytest.mark.parametrize("module_name", [
    "refund_eval.published.privacy",
    "refund_eval.sim",
    "refund_eval.sim.common",
])
def test_screened_error_fails_closed_when_reduced_package_omits_helpers(monkeypatch, module_name):
    if module_name == "refund_eval.sim":
        monkeypatch.delitem(sys.modules, "refund_eval.sim.common", raising=False)
    monkeypatch.setitem(sys.modules, module_name, None)

    assert screened_error("provider diagnostic with private detail") == REDACTED_SCENARIO_VALUE


def test_screened_error_reraises_unrelated_missing_dependency(monkeypatch):
    original_import = builtins.__import__

    def import_with_unrelated_failure(name, *args, **kwargs):
        if name == "refund_eval.published.privacy":
            raise ModuleNotFoundError("missing unrelated dependency", name="example_dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_with_unrelated_failure)

    with pytest.raises(ModuleNotFoundError, match="missing unrelated dependency"):
        screened_error("provider diagnostic")
