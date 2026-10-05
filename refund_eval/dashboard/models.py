"""Public tool-capable model catalog for the dashboard picker; no model calls."""

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
            for row in data["data"]:
                model_id = row.get("id", "")
                if (not re.fullmatch(r"[A-Za-z0-9._:/-]+", model_id)
                        or "tools" not in row.get("supported_parameters", [])
                        or "text" not in row.get("architecture", {}).get("output_modalities", [])):
                    continue
                models[model_id] = {"id": f"openrouter/{model_id}", "name": row.get("name") or model_id}
            if not models:
                raise ValueError("No tool-capable models returned")
            _cached = {"models": sorted(models.values(), key=lambda item: item["name"].lower()), "error": None}
            _expires = time.monotonic() + 300
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            _cached = {"models": (_cached or {}).get("models", []),
                       "error": "Could not refresh the model catalog. You can still add an OpenRouter model ID."}
            _expires = time.monotonic() + 30
        return _cached
