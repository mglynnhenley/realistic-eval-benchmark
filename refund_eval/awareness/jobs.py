"""Device-agnostic scoring job: a pure function shared by local and Modal runs (torch is imported lazily)."""

from __future__ import annotations

import numpy as np

from refund_eval.awareness.probe import Probe


def score_job(probe: dict, conversations: list[dict], *, window: int = 8192, device=None) -> list[list[dict]]:
    """Per conversation, `[{"index", "pos", "score"}]` for each assistant message the reader located."""
    from refund_eval.awareness.model import Reader

    p = Probe.from_dict(probe)
    reader = Reader(p.model, p.layer, device=device, window=window)
    out: list[list[dict]] = []
    for conv in conversations:
        messages = conv["messages"]
        pairs = reader.conversation_activations(messages, pool=p.pool)
        scores = p.score(np.stack([vec for _, vec in pairs])) if pairs else []
        out.append([{"index": messages[pos]["index"], "pos": pos, "score": float(s)}
                    for (pos, _), s in zip(pairs, scores)])
    return out
