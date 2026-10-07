"""Public tool-capable model catalog for the dashboard picker; no model calls."""

import difflib
import json
import re
import threading
import time
import urllib.error
import urllib.request

_lock = threading.Lock()
_cached: dict | None = None
_expires = 0.0


def model_catalog() -> dict:
    global _cached, _expires
    with _lock:
        if _cached is not None and time.monotonic() < _expires:
            return _cached
        try:
            request = urllib.request.Request("https://openrouter.ai/api/v1/models", headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=8) as response:
                data = json.load(response)
            models = {}
            every = sorted(f"openrouter/{row.get('id', '')}" for row in data["data"] if row.get("id"))
            for row in data["data"]:
                model_id = row.get("id", "")
                if (not re.fullmatch(r"[A-Za-z0-9._:/-]+", model_id)
                        or "tools" not in row.get("supported_parameters", [])
                        or "text" not in row.get("architecture", {}).get("output_modalities", [])):
                    continue
                models[model_id] = {"id": f"openrouter/{model_id}", "name": row.get("name") or model_id}
            if not models:
                raise ValueError("No tool-capable models returned")
            _cached = {"models": sorted(models.values(), key=lambda item: item["name"].lower()), "error": None,
                       "all_ids": every}
            _expires = time.monotonic() + 300
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            _cached = {"models": (_cached or {}).get("models", []), "all_ids": (_cached or {}).get("all_ids", []),
                       "error": "Could not refresh the model catalog. You can still add an OpenRouter model ID."}
            _expires = time.monotonic() + 30
        return _cached


def unknown_models(roles: dict[str, str], catalog: dict | None = None) -> list[str]:
    """One message per role whose OpenRouter model ID is not in the catalog, with the
    closest IDs as suggestions. Empty when the catalog can't be read (never blocks offline)."""
    catalog = catalog if catalog is not None else model_catalog()
    known = catalog.get("all_ids") or []
    if not known:
        return []
    tool_capable = {m["id"] for m in catalog.get("models", [])}
    problems = []
    for role, model in roles.items():
        if not model.startswith("openrouter/"):
            continue
        if model in known:
            if role == "target" and tool_capable and model not in tool_capable:
                problems.append(f"{role}: {model} can't call tools, which the agent needs.")
            continue
        close = difflib.get_close_matches(model, known, n=2, cutoff=0.6)
        hint = f" Did you mean {' or '.join(close)}?" if close else ""
        problems.append(f"{role}: {model} is not an OpenRouter model.{hint}")
    return problems
