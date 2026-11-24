"""Private, best-effort startup diagnostics for the local inprocess parity path."""

from __future__ import annotations

import json
import os
import re
import stat
import traceback
from collections.abc import Awaitable, Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

DIAGNOSTICS_FILENAME = "startup-diagnostics.jsonl"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SPECIAL_FUNCTIONS = {"<lambda>", "<module>", "<listcomp>", "<dictcomp>", "<setcomp>", "<genexpr>"}
_TRACEBACK_FRAME = re.compile(r'^  File "([^"]+)", line (\d+), in (.+)$')
_EVAL_EXCEPTION_TYPE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)\s*(?::|$)")


class StartupMarker(StrEnum):
    SERVER_READY = "server ready"
    TASK_RUNNER_ENTERED = "task runner entered"
    FIRST_REQUEST_RECEIVED = "first request received"


def exception_type_name(exception: BaseException) -> str:
    name = type(exception).__name__
    return name if _IDENTIFIER.fullmatch(name) else "<unknown>"


class StartupDiagnostics:
    def __init__(self, directory: Path, repo_root: Path | None = None):
        self.directory = Path(directory).absolute()
        self.repo_root = Path(repo_root) if repo_root is not None else Path(__file__).parents[2]

    def prepare(self) -> None:
        try:
            directory_fd = self._open_directory()
            if directory_fd is None:
                return
            try:
                os.fchmod(directory_fd, 0o700)
            finally:
                os.close(directory_fd)
        except BaseException:  # noqa: BLE001, S110
            pass

    def marker(self, marker: StartupMarker) -> None:
        if not isinstance(marker, StartupMarker):
            return
        try:
            self._append({"kind": "marker", "marker": marker.value})
        except BaseException:  # noqa: BLE001, S110
            pass

    def record_exception(self, exception: BaseException) -> None:
        try:
            frames = traceback.extract_tb(exception.__traceback__, limit=64)
            self._append({"kind": "exception", "exception_type": exception_type_name(exception),
                          "frames": [self._frame(frame.filename, frame.lineno, frame.name) for frame in frames]})
        except BaseException:  # noqa: BLE001, S110
            pass

    def record_eval_error(self, error: Any) -> None:
        try:
            formatted_traceback = getattr(error, "traceback", None)
            frames = []
            exception_type = "<unknown>"
            if isinstance(formatted_traceback, str):
                in_traceback = False
                for line in formatted_traceback.splitlines():
                    if not in_traceback:
                        if line == "Traceback (most recent call last):":
                            in_traceback = True
                        continue

                    match = _TRACEBACK_FRAME.fullmatch(line)
                    if match:
                        if len(frames) < 64:
                            frames.append(self._frame(match.group(1), int(match.group(2)), match.group(3)))
                        continue

                    if line and not line[0].isspace():
                        match = _EVAL_EXCEPTION_TYPE.match(line)
                        if match:
                            name = match.group(1).rsplit(".", 1)[-1]
                            if _IDENTIFIER.fullmatch(name):
                                exception_type = name
                        break

            self._append({"kind": "exception", "exception_type": exception_type, "frames": frames})
        except BaseException:  # noqa: BLE001, S110
            pass

    def _frame(self, filename: str, line: int, function: str) -> dict[str, str | int]:
        safe_function = function if _IDENTIFIER.fullmatch(function) or function in _SPECIAL_FUNCTIONS else "<unknown>"
        return {"file": self._repo_relative_file(filename), "line": line if isinstance(line, int) and line > 0 else 0,
                "function": safe_function}

    def _repo_relative_file(self, filename: str) -> str:
        try:
            if not isinstance(filename, str) or not filename or filename.startswith("<"):
                return "<external>"
            root = self.repo_root.resolve()
            path = Path(filename)
            if not path.is_absolute():
                path = root / path
            relative = path.resolve(strict=False).relative_to(root)
            value = relative.as_posix()
            if not value or value == "." or any(part in {"", ".", ".."} for part in value.split("/")):
                return "<external>"
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                return "<external>"
            return value
        except Exception:  # noqa: BLE001
            return "<external>"

    def _open_directory(self) -> int | None:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        if not nofollow or not directory or not hasattr(os, "geteuid"):
            return None
        flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(self.directory, flags)
        keep_fd = False
        try:
            details = os.fstat(fd)
            if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid():
                return None
            keep_fd = True
            return fd
        finally:
            if not keep_fd:
                try:
                    os.close(fd)
                except BaseException:  # noqa: BLE001, S110
                    pass

    def _append(self, record: dict[str, Any]) -> None:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            return
        directory_fd = self._open_directory()
        if directory_fd is None:
            return
        file_fd = None
        try:
            os.fchmod(directory_fd, 0o700)
            try:
                existing = os.stat(DIAGNOSTICS_FILENAME, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1 or existing.st_uid != os.geteuid():
                    return
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | nofollow | getattr(os, "O_NONBLOCK", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            file_fd = os.open(DIAGNOSTICS_FILENAME, flags, 0o600, dir_fd=directory_fd)
            details = os.fstat(file_fd)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_uid != os.geteuid():
                return
            os.fchmod(file_fd, 0o600)
            payload = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
            remaining = memoryview(payload)
            while remaining:
                written = os.write(file_fd, remaining)
                if written <= 0:
                    return
                remaining = remaining[written:]
        finally:
            try:
                if file_fd is not None:
                    os.close(file_fd)
            finally:
                os.close(directory_fd)


class FirstRequestMiddleware:
    def __init__(self, app: Callable[..., Awaitable[None]], on_first_request: Callable[[], None]):
        self.app = app
        self.on_first_request = on_first_request
        self._recorded = False

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Awaitable[Any]],
                       send: Callable[..., Awaitable[Any]]) -> None:
        if scope.get("type") == "http" and not self._recorded:
            self._recorded = True
            try:
                self.on_first_request()
            except BaseException:  # noqa: BLE001, S110
                pass
        await self.app(scope, receive, send)
