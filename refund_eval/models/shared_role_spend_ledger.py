"""One durable allowance shared by target and world model requests."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from refund_eval.models.world_spend_ledger import (
    WorldSpendBudgetExhausted,
    WorldSpendLedgerError,
)

LEDGER_DIRECTORY = Path("/var/lib/refund-eval/role-budget")
LEDGER_FILE = "role-spend-ledger.json"
CONFIG_ENV = "REFUND_EVAL_SHARED_ROLE_BUDGET_CONFIG"
ROLES = ("target", "world")
_VERSION = 1


class SharedRoleSpendLedgerError(WorldSpendLedgerError):
    """The shared-role ledger is invalid or cannot safely be updated."""


class SharedRoleBudgetExhausted(WorldSpendBudgetExhausted):
    """A role or aggregate reservation does not fit its allowance."""


def _amount(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise SharedRoleSpendLedgerError(f"Invalid {name} in shared-role ledger")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise SharedRoleSpendLedgerError(f"Invalid {name} in shared-role ledger") from None
    if not result.is_finite() or result < 0:
        raise SharedRoleSpendLedgerError(f"Invalid {name} in shared-role ledger")
    return result


def _serialized_amount(value: Any, name: str) -> Decimal:
    if not isinstance(value, str):
        raise SharedRoleSpendLedgerError(f"Invalid serialized {name} in shared-role ledger")
    return _amount(value, name)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class SharedRoleBudgetConfig:
    def __init__(self, *, allowance_usd: Any, role_limits_usd: Mapping[str, Any],
                 scenario: str, models: Mapping[str, str]) -> None:
        allowance = _amount(allowance_usd, "shared allowance")
        limits = {role: _amount(role_limits_usd.get(role), f"{role} limit") for role in ROLES}
        if set(role_limits_usd) != set(ROLES) or any(value <= 0 for value in limits.values()):
            raise SharedRoleSpendLedgerError("Shared-role caps must define positive target and world limits")
        if allowance <= 0 or allowance > sum(limits.values()) or allowance > Decimal(150):
            raise SharedRoleSpendLedgerError("Shared allowance must be positive and no greater than the role caps or $150")
        expected_models = {"target", "simulator", "grader"}
        if (not isinstance(scenario, str) or not scenario or set(models) != expected_models
                or any(not isinstance(value, str) or not value.startswith("openrouter/") for value in models.values())):
            raise SharedRoleSpendLedgerError("Shared-role ledger scope is invalid")
        if limits["target"] > Decimal(50) or limits["world"] > Decimal(100):
            raise SharedRoleSpendLedgerError("Shared-role caps exceed their $50/$100 ceilings")
        self.allowance_usd = allowance
        self.role_limits_usd = limits
        self.scenario = scenario
        self.models = dict(models)

    @property
    def scope(self) -> dict[str, Any]:
        return {"scenario": self.scenario, "models": dict(sorted(self.models.items())),
                "role_limits_usd": {role: str(self.role_limits_usd[role]) for role in ROLES},
                "allowance_usd": str(self.allowance_usd)}

    @classmethod
    def from_json(cls, raw: str) -> SharedRoleBudgetConfig:
        try:
            value = json.loads(raw, object_pairs_hook=_unique_object)
        except (ValueError, TypeError):
            raise SharedRoleSpendLedgerError("Shared-role budget configuration is malformed") from None
        if not isinstance(value, dict) or set(value) != {
            "schema_version", "shared_allowance_usd", "role_limits_usd", "scenario", "models"
        } or value.get("schema_version") != 1 or type(value.get("schema_version")) is not int:
            raise SharedRoleSpendLedgerError("Shared-role budget configuration is malformed")
        if not isinstance(value["role_limits_usd"], dict) or not isinstance(value["models"], dict):
            raise SharedRoleSpendLedgerError("Shared-role budget configuration is malformed")
        return cls(allowance_usd=value["shared_allowance_usd"], role_limits_usd=value["role_limits_usd"],
                   scenario=value["scenario"], models=value["models"])

    @classmethod
    def from_environment(cls) -> SharedRoleBudgetConfig:
        raw = os.environ.get(CONFIG_ENV)
        if raw is None:
            raise SharedRoleSpendLedgerError("Shared-role budget configuration is missing")
        return cls.from_json(raw)


class SharedRoleSpendLedger:
    def __init__(self, directory: str | Path, config: SharedRoleBudgetConfig, *, lock_timeout: float = 10.0):
        if not isinstance(config, SharedRoleBudgetConfig):
            raise SharedRoleSpendLedgerError("Shared-role budget configuration is invalid")
        self.config = config
        self.directory = Path(directory)
        self.path = self.directory / LEDGER_FILE
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.marker_path = self.directory / ".initialized"
        self.lock = FileLock(str(self.lock_path), timeout=lock_timeout)
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.lock:
                if self.path.exists():
                    self._read_state()
                elif self.marker_path.exists() or any(
                    path not in {self.lock_path, self.marker_path} for path in self.directory.iterdir()
                ):
                    raise SharedRoleSpendLedgerError("Shared-role ledger state is missing; refusing to reset it")
                else:
                    self._write_state(self._empty_state())
                    self.marker_path.write_text("initialized\n", encoding="ascii")
        except FileLockTimeout as error:
            raise SharedRoleSpendLedgerError("Timed out locking shared-role spend ledger") from error
        except OSError as error:
            raise SharedRoleSpendLedgerError("Could not initialize shared-role spend ledger") from error

    def _empty_state(self) -> dict[str, Any]:
        return {"version": _VERSION, "scope": self.config.scope, "attempts": {}}

    def _validate_state(self, state: Any) -> dict[str, Any]:
        if not isinstance(state, dict) or set(state) != {"version", "scope", "attempts"}:
            raise SharedRoleSpendLedgerError("Malformed shared-role spend ledger")
        if type(state["version"]) is not int or state["version"] != _VERSION or state["scope"] != self.config.scope:
            raise SharedRoleSpendLedgerError("Shared-role ledger scope or caps changed; refusing to reuse it")
        attempts = state["attempts"]
        if not isinstance(attempts, dict):
            raise SharedRoleSpendLedgerError("Malformed shared-role attempt map")
        required = {"role", "request_hash", "reserved_usd", "status", "charged_usd", "provenance"}
        for attempt_id, item in attempts.items():
            try:
                if str(uuid.UUID(attempt_id)) != attempt_id:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise SharedRoleSpendLedgerError("Invalid shared-role attempt ID") from None
            if (not isinstance(item, dict) or set(item) != required
                    or not isinstance(item["role"], str) or item["role"] not in ROLES):
                raise SharedRoleSpendLedgerError("Malformed shared-role attempt")
            if (not isinstance(item["request_hash"], str) or len(item["request_hash"]) != 64
                    or any(char not in "0123456789abcdef" for char in item["request_hash"])):
                raise SharedRoleSpendLedgerError("Invalid shared-role request hash")
            reserved = _serialized_amount(item["reserved_usd"], "reservation")
            if reserved > self.config.role_limits_usd[item["role"]] or reserved > self.config.allowance_usd:
                raise SharedRoleSpendLedgerError("Shared-role reservation exceeds its configured cap")
            status = item["status"]
            if not isinstance(status, str) or status not in {"reserved", "unknown", "settlement_pending", "settled"}:
                raise SharedRoleSpendLedgerError("Invalid shared-role attempt status")
            if status in {"settlement_pending", "settled"} or (status == "unknown" and item["charged_usd"] is not None):
                charged = _serialized_amount(item["charged_usd"], "charge")
                if status in {"settlement_pending", "settled"} and charged > reserved:
                    raise SharedRoleSpendLedgerError("Shared-role settled charge exceeds its reservation")
                if not isinstance(item["provenance"], str) or not item["provenance"].strip():
                    raise SharedRoleSpendLedgerError("Invalid shared-role settlement provenance")
            elif item["charged_usd"] is not None or item["provenance"] is not None:
                raise SharedRoleSpendLedgerError("Unexpected shared-role settlement")
        return state

    def _read_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        except (OSError, UnicodeError, ValueError):
            raise SharedRoleSpendLedgerError("Cannot read shared-role ledger; evidence was preserved") from None
        return self._validate_state(state)

    def _write_state(self, state: dict[str, Any]) -> None:
        self._validate_state(state)
        encoded = json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _totals(self, state: dict[str, Any]) -> dict[str, dict[str, Decimal | int]]:
        totals = {role: {"spent_usd": Decimal(0), "reserved_usd": Decimal(0), "unresolved_attempts": 0}
                  for role in ROLES}
        for item in state["attempts"].values():
            role_totals = totals[item["role"]]
            status = item["status"]
            reserved = _amount(item["reserved_usd"], "reservation")
            charged = None if item["charged_usd"] is None else _amount(item["charged_usd"], "charge")
            if status == "settled":
                role_totals["spent_usd"] += charged or Decimal(0)
            else:
                role_totals["reserved_usd"] += max(reserved, charged or Decimal(0))
                role_totals["unresolved_attempts"] += 1
        return totals

    def snapshot(self, role: str | None = None) -> dict[str, Any]:
        if role is not None and role not in ROLES:
            raise SharedRoleSpendLedgerError("Invalid shared-role ledger view")
        try:
            with self.lock:
                state = self._read_state()
        except FileLockTimeout as error:
            raise SharedRoleSpendLedgerError("Timed out reading shared-role spend ledger") from error
        return self._snapshot_from_state(state, role)

    def _snapshot_from_state(self, state: dict[str, Any], role: str | None = None) -> dict[str, Any]:
        totals = self._totals(state)
        aggregate_spent = sum((value["spent_usd"] for value in totals.values()), Decimal(0))
        aggregate_reserved = sum((value["reserved_usd"] for value in totals.values()), Decimal(0))
        aggregate_unresolved = sum(value["unresolved_attempts"] for value in totals.values())
        if role is None:
            spent, reserved, limit = aggregate_spent, aggregate_reserved, self.config.allowance_usd
        else:
            spent, reserved, limit = totals[role]["spent_usd"], totals[role]["reserved_usd"], self.config.role_limits_usd[role]
        return {"limit_usd": str(limit), "spent_usd": str(spent), "reserved_usd": str(reserved),
                "committed_usd": str(spent + reserved), "unresolved_attempts": aggregate_unresolved,
                "role_unresolved_attempts": 0 if role is None else totals[role]["unresolved_attempts"],
                "aggregate_limit_usd": str(self.config.allowance_usd),
                "aggregate_spent_usd": str(aggregate_spent), "aggregate_reserved_usd": str(aggregate_reserved)}

    def has_unresolved(self) -> bool:
        return self.snapshot()["unresolved_attempts"] > 0

    def view(self, role: str) -> SharedRoleSpendLedgerView:
        if role not in ROLES:
            raise SharedRoleSpendLedgerError("Invalid shared-role ledger view")
        return SharedRoleSpendLedgerView(self, role)

    def reserve(self, role: str, attempt_id: str, request_hash: str, amount: Any) -> dict[str, Any]:
        try:
            if str(uuid.UUID(attempt_id)) != attempt_id or not isinstance(request_hash, str) or len(request_hash) != 64:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise SharedRoleSpendLedgerError("Invalid shared-role request identity") from None
        if any(char not in "0123456789abcdef" for char in request_hash):
            raise SharedRoleSpendLedgerError("Invalid shared-role request identity")
        if role not in ROLES:
            raise SharedRoleSpendLedgerError("Invalid shared-role request role")
        bound = _amount(amount, "reservation")
        try:
            with self.lock:
                state = self._read_state()
                if attempt_id in state["attempts"]:
                    raise SharedRoleSpendLedgerError("Conflicting shared-role attempt ID")
                totals = self._totals(state)
                if sum((entry["unresolved_attempts"] for entry in totals.values()), 0):
                    raise SharedRoleSpendLedgerError("Shared-role ledger has an unresolved hold; refusing another request")
                role_totals = totals[role]
                if role_totals["spent_usd"] + role_totals["reserved_usd"] + bound > self.config.role_limits_usd[role]:
                    raise SharedRoleBudgetExhausted(f"Shared-role {role} cap cannot cover the next request")
                spent = sum((entry["spent_usd"] for entry in totals.values()), Decimal(0))
                reserved = sum((entry["reserved_usd"] for entry in totals.values()), Decimal(0))
                if spent + reserved + bound > self.config.allowance_usd:
                    raise SharedRoleBudgetExhausted("Shared aggregate allowance cannot cover the next request")
                state["attempts"][attempt_id] = {"role": role, "request_hash": request_hash,
                    "reserved_usd": str(bound), "status": "reserved", "charged_usd": None, "provenance": None}
                self._write_state(state)
                return self.snapshot(role)
        except FileLockTimeout as error:
            raise SharedRoleSpendLedgerError("Timed out reserving shared-role spend") from error

    def mark_unknown(self, attempt_id: str, charged: Any = None, provenance: str | None = None) -> dict[str, Any]:
        charge = None if charged is None else _amount(charged, "charge")
        if ((charge is None) != (provenance is None)
                or (provenance is not None and (not isinstance(provenance, str) or not provenance.strip()))):
            raise SharedRoleSpendLedgerError("Unknown shared-role charge and provenance must be supplied together")
        try:
            with self.lock:
                state = self._read_state()
                record = state["attempts"].get(attempt_id)
                if record is None:
                    raise SharedRoleSpendLedgerError("Unknown shared-role attempt ID")
                if record["status"] == "reserved":
                    record.update(status="unknown", charged_usd=None if charge is None else str(charge), provenance=provenance)
                    self._write_state(state)
                elif record["status"] == "unknown" and charge is not None:
                    if record["charged_usd"] is None:
                        record.update(charged_usd=str(charge), provenance=provenance)
                        self._write_state(state)
                    elif record["charged_usd"] != str(charge) or record["provenance"] != provenance:
                        raise SharedRoleSpendLedgerError("Conflicting unknown shared-role charge")
                return self.snapshot(record["role"])
        except FileLockTimeout as error:
            raise SharedRoleSpendLedgerError("Timed out recording shared-role hold") from error

    def settle(self, attempt_id: str, charged: Any, provenance: str) -> dict[str, Any]:
        charge = _amount(charged, "charge")
        if not isinstance(provenance, str) or not provenance.strip():
            raise SharedRoleSpendLedgerError("Shared-role settlement needs provenance")
        try:
            with self.lock:
                state = self._read_state()
                record = state["attempts"].get(attempt_id)
                if record is None:
                    raise SharedRoleSpendLedgerError("Unknown shared-role attempt ID")
                if record["status"] == "settled":
                    if record["charged_usd"] == str(charge) and record["provenance"] == provenance:
                        return self.snapshot(record["role"])
                    raise SharedRoleSpendLedgerError("Conflicting shared-role settlement")
                if (record["status"] in {"unknown", "settlement_pending"}
                        and record["charged_usd"] is not None
                        and (record["charged_usd"] != str(charge) or record["provenance"] != provenance)):
                    raise SharedRoleSpendLedgerError("Conflicting shared-role settlement")
                if charge > _amount(record["reserved_usd"], "reservation"):
                    record.update(status="unknown", charged_usd=str(charge), provenance=provenance)
                    self._write_state(state)
                    raise SharedRoleSpendLedgerError("Shared-role charge exceeded its reservation; hold retained")
                role = record["role"]
                record.update(status="settlement_pending", charged_usd=str(charge), provenance=provenance)
                self._write_state(state)
                pending = {"version": state["version"], "scope": state["scope"],
                           "attempts": {key: dict(value) for key, value in state["attempts"].items()}}
                record["status"] = "settled"
                try:
                    self._write_state(state)
                except BaseException as error:
                    try:
                        self._write_state(pending)
                    except BaseException as restore_error:
                        raise SharedRoleSpendLedgerError(
                            f"Settlement for attempt {attempt_id} could not be finalized; evidence remains at {self.path}"
                        ) from restore_error
                    raise SharedRoleSpendLedgerError(
                        f"Settlement for attempt {attempt_id} failed; its reservation remains held"
                    ) from error
                return self._snapshot_from_state(state, role)
        except FileLockTimeout as error:
            raise SharedRoleSpendLedgerError("Timed out settling shared-role spend") from error


class SharedRoleSpendLedgerView:
    def __init__(self, ledger: SharedRoleSpendLedger, role: str) -> None:
        self.ledger = ledger
        self.role = role
        self.path = ledger.path
        self.limit = ledger.config.role_limits_usd[role]

    def reserve(self, attempt_id: str, request_hash: str, amount: Any) -> dict[str, Any]:
        return self.ledger.reserve(self.role, attempt_id, request_hash, amount)

    def settle(self, attempt_id: str, charged: Any, provenance: str) -> dict[str, Any]:
        return self.ledger.settle(attempt_id, charged, provenance)

    def mark_unknown(self, attempt_id: str, charged: Any = None, provenance: str | None = None) -> dict[str, Any]:
        return self.ledger.mark_unknown(attempt_id, charged, provenance)

    def snapshot(self) -> dict[str, Any]:
        return self.ledger.snapshot(self.role)

    def has_unresolved(self) -> bool:
        return self.ledger.has_unresolved()
