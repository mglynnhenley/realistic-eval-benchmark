"""Upload each finished task's log to Supabase when it is configured.

Importing this module registers the hook; `refund_eval.task` imports it, so CLI,
dashboard and Harbor trials all upload. A failed upload is logged and never
changes the trial or its saved log; `python -m refund_eval.storage upload`
can send it again.
"""

from __future__ import annotations

import logging

from inspect_ai.hooks import Hooks, TaskEnd, hooks

from refund_eval.storage.supabase import configured, upload_log

logger = logging.getLogger(__name__)


@hooks(name="supabase_transcripts", description="Uploads finished run transcripts to Supabase.")
class SupabaseTranscripts(Hooks):
    def enabled(self) -> bool:
        return configured()

    async def on_task_end(self, data: TaskEnd) -> None:
        location = data.log.location
        if not location:
            return
        try:
            run_ids = await upload_log(location, data.log)
            logger.info("Uploaded %d transcript(s) from %s to Supabase", len(run_ids), location)
        except Exception:
            logger.warning("Could not upload %s to Supabase; the local log is unchanged. "
                           "Retry with: python -m refund_eval.storage upload %s",
                           location, location, exc_info=True)
