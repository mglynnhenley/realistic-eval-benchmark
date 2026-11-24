"""Host-only installed-agent observer: download one sanitized world-side file."""

import asyncio
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from refund_eval.dashboard.luna import check_step_update, timestamp, validate
from refund_eval.dashboard.luna_events import MAX_FEED_BYTES
from refund_eval.world_service.status import (
    ACTION_SUMMARIES,
    WORLD_FIELDS,
    WORLD_STATUS_FILE,
    atomic_json,
)


class NativeStatus:
    def __init__(self, logs_dir, model):
        self.directory = logs_dir.parent
        self.run_id = self.directory.name
        self.model = model
        self.last = None

    async def observe(self, environment):
        with tempfile.TemporaryDirectory(prefix="luna-operator-status-") as directory:
            local = Path(directory) / WORLD_STATUS_FILE
            await environment.service_download_file(f"/var/log/world/{WORLD_STATUS_FILE}", local, service="world")
            with local.open("rb") as handle:
                raw = handle.read(MAX_FEED_BYTES + 1)
            if len(raw) > MAX_FEED_BYTES:
                raise ValueError("oversized source")
            source = json.loads(raw)
        if not isinstance(source, dict) or set(source) != WORLD_FIELDS:
            raise ValueError("unexpected world status fields")
        if type(source["schema_version"]) is not int or source["schema_version"] != 2:
            raise ValueError("unsupported world status")
        if source["trial_name"] != self.run_id.lower():
            raise ValueError("different trial")
        if type(source["stopped"]) is not bool or source["latest_action"] not in {*ACTION_SUMMARIES.values(), None}:
            raise ValueError("untrusted status text")
        now = datetime.now(UTC)
        candidate = validate({
            "schema_version": 2, "run_id": self.run_id, "model": self.model, "steps": source["steps"],
            "stage": "stopped" if source["stopped"] else "running", "launch_confirmed": True,
            "simulated_date": source["simulated_date"], "turns": source["turns"],
            "agent_spend_usd": None, "world_spend_usd": source["world_spend_usd"],
            "latest_action": source["latest_action"], "source_updated_at": source["source_updated_at"],
            "heartbeat_at": now.isoformat(),
        }, now, self.run_id)
        if self.last and timestamp(candidate["source_updated_at"]) < timestamp(self.last["source_updated_at"]):
            raise ValueError("older evidence")
        if self.last:
            check_step_update(self.last, candidate)
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_json(self.directory / "luna-source.json", candidate)
        self.last = candidate

    async def safe_observe(self, environment):
        try:
            async with asyncio.timeout(2):
                await self.observe(environment)
        except Exception:  # noqa: BLE001 -- observation must never fail or expose private trial errors
            return False
        return True

    async def watch(self, environment):
        while True:
            await self.safe_observe(environment)
            await asyncio.sleep(5)
