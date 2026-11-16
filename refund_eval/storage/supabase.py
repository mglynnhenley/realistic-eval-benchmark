"""Upload saved Inspect logs to Supabase: run rows, message rows and the raw log.

Configured by `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. Uploading again
replaces a trial's row and messages, so a log can be re-sent safely.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import httpx
from inspect_ai.log import EvalLog

from refund_eval.config import SUPABASE_KEY_ENV as KEY_ENV
from refund_eval.config import SUPABASE_URL_ENV as URL_ENV
from refund_eval.runtime.trial_artifacts import is_harbor_trial, read_trial_log
from refund_eval.storage.rows import message_row, run_row

BUCKET = "eval-logs"
# Keep each message insert well under the API gateway's request size limit.
MAX_BATCH_BYTES = 4_000_000

logger = logging.getLogger(__name__)


class UploadError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get(URL_ENV, "").strip() and os.environ.get(KEY_ENV, "").strip())


def _client() -> httpx.AsyncClient:
    if not configured():
        raise UploadError(f"Set {URL_ENV} and {KEY_ENV} to upload transcripts to Supabase.")
    key = os.environ[KEY_ENV].strip()
    headers = {"apikey": key}
    if key.startswith("eyJ"):
        # Legacy JWT keys also authenticate as the bearer; new sb_secret_ keys go in apikey only.
        headers["Authorization"] = f"Bearer {key}"
    return httpx.AsyncClient(base_url=os.environ[URL_ENV].strip().rstrip("/"), headers=headers, timeout=120.0)


def _check(response: httpx.Response, action: str) -> httpx.Response:
    if response.is_error:
        raise UploadError(f"Supabase {action} failed ({response.status_code}): {response.text[:500]}")
    return response


def _batches(rows: list[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        row_size = len(json.dumps(row, ensure_ascii=False).encode())
        if batch and size + row_size > MAX_BATCH_BYTES:
            yield batch
            batch, size = [], 0
        batch.append(row)
        size += row_size
    if batch:
        yield batch


async def _upload_file(client: httpx.AsyncClient, content: bytes, object_path: str) -> None:
    _check(await client.post(
        f"/storage/v1/object/{BUCKET}/{object_path}", content=content,
        headers={"x-upsert": "true", "content-type": "application/octet-stream"},
    ), "log file upload")


async def upload_log(path: str | Path, log: EvalLog | None = None) -> list[str]:
    """Upload every sample in a saved .eval log or native Harbor trial directory; return the run row IDs.

    The raw log is stored first (for a Harbor trial, the agent's .eval; the rows hold the
    world from its artifacts). If the bucket rejects it (for example, over the plan's file
    size limit) the rows are still written, without a log path.
    """
    source = Path(path).expanduser().resolve()
    if log is None or not log.samples:
        log = await anyio.to_thread.run_sync(read_trial_log, source)
    if not log.samples:
        raise UploadError(f"{source} contains no samples to upload.")
    content = Path(log.location).read_bytes() if is_harbor_trial(source) else source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    # Inspect's file names carry timestamps with characters storage keys reject.
    object_path: str | None = f"{log.eval.eval_id}.eval"
    run_ids: list[str] = []
    async with _client() as client:
        try:
            await _upload_file(client, content, object_path)
        except (UploadError, httpx.HTTPError) as error:
            logger.warning("Could not store the raw log %s; uploading rows only: %s", source, error)
            object_path = None
        for sample in log.samples:
            row = run_row(log, sample, eval_log_path=object_path, eval_log_sha256=digest)
            response = _check(await client.post(
                "/rest/v1/runs", params={"on_conflict": "eval_id,sample_id,epoch", "select": "id"},
                json=row, headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            ), "run upsert")
            run_id = response.json()[0]["id"]
            _check(await client.delete("/rest/v1/run_messages", params={"run_id": f"eq.{run_id}"}),
                   "message replacement")
            messages = [message_row(run_id, index, message) for index, message in enumerate(sample.messages)]
            for batch in _batches(messages):
                _check(await client.post("/rest/v1/run_messages", json=batch,
                                         headers={"Prefer": "return=minimal"}), "message insert")
            run_ids.append(run_id)
    return run_ids
