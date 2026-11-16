"""Opaque ids the world hands out, derived from the trial seed so a replay gets the same ones.

Each purpose ("interception", "message", ...) has its own counter in the trial store
under ID_DRAWS_KEY, so ids are part of a trial snapshot. An id is the first `length`
hex digits of sha256("{seed}:{purpose}:{n}"), the same alphabet and length as the
uuid4 hex ids used before. Worlds without a seed (saved before trials had one), or
calls with no world, keep drawing random uuid4 ids.
"""

from __future__ import annotations

import hashlib
import uuid

from refund_eval.world.state import WORLD_KEY
from refund_eval.world.store import world_store

ID_DRAWS_KEY = "world_id_draws"


def new_hex(purpose: str, length: int = 16) -> str:
    store = world_store()
    world = store.get(WORLD_KEY)
    seed = world.get("seed") if isinstance(world, dict) else None
    if seed is None:
        return uuid.uuid4().hex[:length]
    draws = dict(store.get(ID_DRAWS_KEY) or {})
    n = draws.get(purpose, 0)
    draws[purpose] = n + 1
    store.set(ID_DRAWS_KEY, draws)
    return hashlib.sha256(f"{seed}:{purpose}:{n}".encode()).hexdigest()[:length]
