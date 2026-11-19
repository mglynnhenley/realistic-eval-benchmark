"""Durable per-trial reservations for world-model requests."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

LEDGER_FILE = "world-spend-ledger.json"
LEDGER_VERSION = 1
UNRESOLVED_STATUSES = {"reserved", "unknown", "settlement_pending"}
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class WorldSpendLedgerError(RuntimeError):
    """The durable world-spend ledger is invalid or cannot safely be changed."""


def _amount(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise WorldSpendLedgerError(f"Invalid {field} in world-spend ledger")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise WorldSpendLedgerError(f"Invalid {field} in world-spend ledger") from error
    if not result.is_finite() or result < 0:
        raise WorldSpendLedgerError(f"Invalid {field} in world-spend ledger")
    return result


def _decimal_string(value: Any, field: str) -> Decimal:
    if not isinstance(value, str):
        raise WorldSpendLedgerError(f"Invalid {field} in world-spend ledger")
    return _amount(value, field)


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key {key!r}")
        result[key] = value
    return result


class WorldSpendLedger:
    """A locked ledger whose file location identifies one world-service trial."""

    def __init__(self, directory: str | Path | None, limit: Decimal | str | float,
                 scope: Mapping[str, Any], *, lock_timeout: float = 10.0) -> None:
        self.limit = _amount(limit, "limit")
        if self.limit <= 0:
            raise WorldSpendLedgerError("World-spend ledger limit must be positive")
        self.scope = self._scope(scope)
        self.directory = None if directory is None else Path(directory)
        self.path = None if self.directory is None else self.directory / LEDGER_FILE
        self.lock_path = None if self.path is None else self.path.with_suffix(self.path.suffix + ".lock")
        self.lock_timeout = lock_timeout
        self._memory_lock = threading.RLock()
        self._memory_state: dict[str, Any] | None = None
        self._file_lock = None if self.lock_path is None else FileLock(str(self.lock_path), timeout=lock_timeout)

        if self.directory is None:
            self._memory_state = self._empty_state()
            return

        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self._locked():
                if self.path.exists():
                    self._read_state()
                else:
                    self._reject_historical_output()
                    self._write_state(self._empty_state())
        except FileLockTimeout as error:
            raise WorldSpendLedgerError(f"Timed out locking world-spend ledger {self.path}") from error

    @classmethod
    def in_memory(cls, limit: Decimal | str | float, scope: Mapping[str, Any] | None = None) -> WorldSpendLedger:
        return cls(None, limit, scope or {"scenario": "unit-test", "models": {"model": "unit-test"}})

    @staticmethod
    def _scope(scope: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(scope, Mapping):
            raise WorldSpendLedgerError("World-spend ledger scope must be an object")
        scenario, models = scope.get("scenario"), scope.get("models")
        if not isinstance(scenario, str) or not scenario or not isinstance(models, Mapping) or not models:
            raise WorldSpendLedgerError("World-spend ledger scope needs a scenario and model mapping")
        if any(not isinstance(role, str) or not role or not isinstance(model, str) or not model
               for role, model in models.items()):
            raise WorldSpendLedgerError("World-spend ledger scope has an invalid model mapping")
        try:
            copied = json.loads(json.dumps({"scenario": scenario, "models": dict(models)}, sort_keys=True))
        except (TypeError, ValueError) as error:
            raise WorldSpendLedgerError("World-spend ledger scope is not JSON serializable") from error
        return copied

    def _empty_state(self) -> dict[str, Any]:
        return {"version": LEDGER_VERSION, "limit_usd": str(self.limit), "scope": self.scope, "attempts": {}}

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if self._file_lock is None:
            with self._memory_lock:
                yield
            return
        with self._file_lock:
            yield

    def _reject_historical_output(self) -> None:
        if self.directory is None:
            return
        artifacts = ("state.json", "state.json.tmp")
        if any((self.directory / name).exists() for name in artifacts):
            raise WorldSpendLedgerError(
                f"World-spend ledger is missing from existing trial output {self.directory}; "
                "refusing to replace historical state. Restore the ledger or choose a new output directory."
            )
        events = self.directory / "events.jsonl"
        if events.exists() and events.stat().st_size:
            raise WorldSpendLedgerError(
                f"World-spend ledger is missing from existing trial output {self.directory}; "
                "refusing to replace historical events. Restore the ledger or choose a new output directory."
            )

    def _validate_state(self, state: Any) -> dict[str, Any]:
        if not isinstance(state, dict) or set(state) != {"version", "limit_usd", "scope", "attempts"}:
            raise WorldSpendLedgerError(f"Malformed world-spend ledger at {self.path}")
        if type(state["version"]) is not int or state["version"] != LEDGER_VERSION:
            raise WorldSpendLedgerError(f"Unsupported world-spend ledger version at {self.path}")
        limit = _decimal_string(state["limit_usd"], "limit")
        if limit != self.limit:
            raise WorldSpendLedgerError(
                f"World-spend ledger limit mismatch at {self.path}; refusing to reset or reuse it"
            )
        if state["scope"] != self.scope:
            raise WorldSpendLedgerError(
                f"World-spend ledger scenario/model scope mismatch at {self.path}; refusing to reuse it"
            )
        attempts = state["attempts"]
        if not isinstance(attempts, dict):
            raise WorldSpendLedgerError(f"Malformed attempt map in world-spend ledger at {self.path}")
        for attempt_id, record in attempts.items():
            try:
                if str(uuid.UUID(attempt_id)) != attempt_id:
                    raise ValueError("non-canonical UUID")
            except (ValueError, TypeError, AttributeError) as error:
                raise WorldSpendLedgerError(f"Invalid attempt ID in world-spend ledger at {self.path}") from error
            required = {"request_hash", "reserved_usd", "status", "charged_usd", "provenance"}
            if not isinstance(record, dict) or set(record) != required:
                raise WorldSpendLedgerError(f"Malformed attempt {attempt_id} in world-spend ledger at {self.path}")
            if not isinstance(record["request_hash"], str) or not _HASH.fullmatch(record["request_hash"]):
                raise WorldSpendLedgerError(f"Invalid request hash in world-spend ledger at {self.path}")
            if _decimal_string(record["reserved_usd"], "reservation") > self.limit:
                raise WorldSpendLedgerError(f"Invalid reservation in world-spend ledger at {self.path}")
            status = record["status"]
            if not isinstance(status, str) or status not in UNRESOLVED_STATUSES | {"settled"}:
                raise WorldSpendLedgerError(f"Invalid attempt status in world-spend ledger at {self.path}")
            if status in {"settled", "settlement_pending"} or (
                    status == "unknown" and record["charged_usd"] is not None):
                _decimal_string(record["charged_usd"], "charge")
                if not isinstance(record["provenance"], str) or not record["provenance"]:
                    raise WorldSpendLedgerError(f"Invalid settlement provenance in world-spend ledger at {self.path}")
            elif record["charged_usd"] is not None or record["provenance"] is not None:
                raise WorldSpendLedgerError(f"Unexpected settlement in world-spend ledger at {self.path}")
        return state

    def _read_state(self) -> dict[str, Any]:
        if self.path is None:
            return self._validate_state(_copy(self._memory_state))
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        except (OSError, UnicodeError, ValueError) as error:
            raise WorldSpendLedgerError(f"Cannot read world-spend ledger at {self.path}; evidence was preserved") from error
        return self._validate_state(value)

    def _write_state(self, state: dict[str, Any]) -> None:
        self._validate_state(state)
        if self.path is None:
            self._memory_state = _copy(state)
            return
        encoded = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                             allow_nan=False).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp",
                                                 dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _totals(state: Mapping[str, Any], limit: Decimal) -> dict[str, Any]:
        spent = Decimal(0)
        reserved = Decimal(0)
        unresolved = 0
        for record in state["attempts"].values():
            status = record["status"]
            bound = _decimal_string(record["reserved_usd"], "reservation")
            if status == "settled":
                spent += _decimal_string(record["charged_usd"], "charge")
            else:
                unresolved += 1
                if status == "unknown" and record["charged_usd"] is not None:
                    charge = _decimal_string(record["charged_usd"], "charge")
                    spent += charge
                    reserved += max(Decimal(0), bound - charge)
                elif status == "settlement_pending":
                    charge = _decimal_string(record["charged_usd"], "charge")
                    reserved += max(bound, charge)
                else:
                    reserved += bound
        return {"limit_usd": str(limit), "spent_usd": str(spent), "reserved_usd": str(reserved),
                "committed_usd": str(spent + reserved), "unresolved_attempts": unresolved}

    def snapshot(self) -> dict[str, Any]:
        try:
            with self._locked():
                return self._totals(self._read_state(), self.limit)
        except FileLockTimeout as error:
            raise WorldSpendLedgerError(f"Timed out reading world-spend ledger {self.path}") from error

    def has_unresolved(self) -> bool:
        return self.snapshot()["unresolved_attempts"] > 0

    def reserve(self, attempt_id: str, request_hash: str, amount: Decimal | str | float) -> dict[str, Any]:
        try:
            canonical_id = str(uuid.UUID(attempt_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise WorldSpendLedgerError("Invalid world-spend attempt ID") from error
        if canonical_id != attempt_id or not isinstance(request_hash, str) or not _HASH.fullmatch(request_hash):
            raise WorldSpendLedgerError("Invalid world-spend request identity")
        bound = _amount(amount, "reservation")
        try:
            with self._locked():
                state = _copy(self._read_state())
                existing = state["attempts"].get(attempt_id)
                if existing is not None:
                    raise WorldSpendLedgerError(f"Conflicting world-spend attempt ID {attempt_id}")
                totals = self._totals(state, self.limit)
                if totals["unresolved_attempts"]:
                    raise WorldSpendLedgerError("World-spend ledger has an unresolved hold; refusing another request")
                spent = _decimal_string(totals["spent_usd"], "spent amount")
                reserved = _decimal_string(totals["reserved_usd"], "reserved amount")
                if spent + reserved + bound > self.limit:
                    raise WorldSpendLedgerError("World-spend ledger cannot cover the next request")
                state["attempts"][attempt_id] = {"request_hash": request_hash, "reserved_usd": str(bound),
                                                  "status": "reserved", "charged_usd": None, "provenance": None}
                self._write_state(state)
                return self._totals(state, self.limit)
        except FileLockTimeout as error:
            raise WorldSpendLedgerError(f"Timed out reserving world spend in {self.path}") from error

    def mark_unknown(self, attempt_id: str, charged: Decimal | str | float | None = None,
                     provenance: str | None = None) -> dict[str, Any]:
        amount = None if charged is None else _amount(charged, "charge")
        if ((amount is None) != (provenance is None)
                or (provenance is not None and (not isinstance(provenance, str) or not provenance.strip()))):
            raise WorldSpendLedgerError("An unknown attempt's charge and provenance must be supplied together")
        try:
            with self._locked():
                state = _copy(self._read_state())
                record = state["attempts"].get(attempt_id)
                if record is None:
                    raise WorldSpendLedgerError(f"Unknown world-spend attempt ID {attempt_id}")
                if record["status"] == "reserved":
                    record.update(status="unknown", charged_usd=None if amount is None else str(amount),
                                  provenance=provenance)
                    self._write_state(state)
                elif record["status"] == "unknown" and amount is not None:
                    if record["charged_usd"] is None:
                        record.update(charged_usd=str(amount), provenance=provenance)
                        self._write_state(state)
                    elif record["charged_usd"] != str(amount) or record["provenance"] != provenance:
                        raise WorldSpendLedgerError(f"Conflicting unknown charge for attempt {attempt_id}")
                return self._totals(state, self.limit)
        except FileLockTimeout as error:
            raise WorldSpendLedgerError(f"Timed out updating world-spend attempt {attempt_id}") from error

    def settle(self, attempt_id: str, charged: Decimal | str | float, provenance: str) -> dict[str, Any]:
        amount = _amount(charged, "charge")
        if not isinstance(provenance, str) or not provenance.strip():
            raise WorldSpendLedgerError("World-spend settlement needs provenance")
        try:
            with self._locked():
                state = _copy(self._read_state())
                record = state["attempts"].get(attempt_id)
                if record is None:
                    raise WorldSpendLedgerError(f"Unknown world-spend attempt ID {attempt_id}")
                if record["status"] == "settled":
                    if (record["charged_usd"] == str(amount) and record["provenance"] == provenance):
                        return self._totals(state, self.limit)
                    raise WorldSpendLedgerError(f"Conflicting settlement for world-spend attempt {attempt_id}")
                if record["status"] == "settlement_pending" or (
                        record["status"] == "unknown" and record["charged_usd"] is not None):
                    if record["charged_usd"] != str(amount) or record["provenance"] != provenance:
                        raise WorldSpendLedgerError(f"Conflicting settlement for world-spend attempt {attempt_id}")
                else:
                    record.update(status="settlement_pending", charged_usd=str(amount), provenance=provenance)
                    self._write_state(state)
                pending = _copy(state)
                state["attempts"][attempt_id]["status"] = "settled"
                try:
                    self._write_state(state)
                except BaseException as error:
                    try:
                        self._write_state(pending)
                    except BaseException as restore_error:
                        raise WorldSpendLedgerError(
                            f"Settlement for attempt {attempt_id} could not be finalized; "
                            f"ledger evidence remains at {self.path}"
                        ) from restore_error
                    raise WorldSpendLedgerError(
                        f"Settlement for attempt {attempt_id} failed; its reservation remains held"
                    ) from error
                return self._totals(state, self.limit)
        except FileLockTimeout as error:
            raise WorldSpendLedgerError(f"Timed out settling world-spend attempt {attempt_id}") from error
