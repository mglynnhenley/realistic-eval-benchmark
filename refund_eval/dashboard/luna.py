"""Read-only Luna status viewer. Consumes only an operator-sanitized JSON feed.

No benchmark modules are imported and no trial controls are exposed. Deploy
behind the authenticated Devin App proxy, not on an unauthenticated public host.
"""

import argparse
import json
import math
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

STATIC = Path(__file__).with_name("luna_static")
FIELDS = {
    "schema_version", "run_id", "model", "stage", "launch_confirmed",
    "simulated_date", "turns", "agent_spend_usd", "world_spend_usd",
    "latest_action", "source_updated_at", "heartbeat_at",
}
STAGES = {"preparation", "running", "paused", "completed", "failed", "stopped"}


def timestamp(value):
    if not isinstance(value, str):
        raise TypeError("timestamp required")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result


def validate(value, now, run_id=None):
    """Reject non-contract fields rather than risking a raw-log response."""
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("unexpected feed fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported schema")
    if value["stage"] not in STAGES or type(value["launch_confirmed"]) is not bool:
        raise ValueError("invalid stage")
    for key, limit in (("run_id", 180), ("model", 100), ("simulated_date", 60), ("latest_action", 400)):
        item = value[key]
        if item is not None and (not isinstance(item, str) or len(item) > limit):
            raise ValueError("invalid text")
    if not value["run_id"] or not value["model"]:
        raise ValueError("operator identity required")
    if run_id and value["run_id"] != run_id:
        raise ValueError("different trial")
    if not value["launch_confirmed"] and value["stage"] != "preparation":
        raise ValueError("launch not confirmed")
    if value["turns"] is not None and (type(value["turns"]) is not int or value["turns"] < 0):
        raise ValueError("invalid turns")
    for key in ("agent_spend_usd", "world_spend_usd"):
        item = value[key]
        if item is not None and (type(item) not in (int, float) or not math.isfinite(item) or item < 0):
            raise ValueError("invalid spend")
    result = dict(value)
    for key in ("source_updated_at", "heartbeat_at"):
        if key == "source_updated_at" and value[key] is None:
            continue
        parsed = timestamp(value[key])
        if (parsed - now).total_seconds() > 30:
            raise ValueError("future timestamp")
        # Python accepts ISO forms (e.g. decimal commas) that browsers do not.
        result[key] = parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return result


class Feed:
    def __init__(self, path, run_id=None):
        self.path = path
        self.run_id = run_id
        self.last = None
        self.lock = threading.Lock()

    def read(self, now=None):
        now = now or datetime.now(UTC)
        with self.lock:
            error = None
            try:
                with self.path.open("rb") as handle:
                    raw = handle.read(8193)
                if len(raw) > 8192:
                    raise ValueError("oversized feed")
                candidate = validate(json.loads(raw), now, self.run_id)
                if self.last and timestamp(candidate["heartbeat_at"]) < timestamp(self.last["heartbeat_at"]):
                    raise ValueError("older heartbeat")
                self.last = candidate
                self.run_id = candidate["run_id"]
            except FileNotFoundError:
                error = "Feed unconnected" if self.last is None else "Feed unavailable"
            except (OSError, ValueError, TypeError):
                error = "Feed unavailable or invalid"
            payload = dict(self.last) if self.last else {
                key: None for key in FIELDS
            }
            if not self.last:
                payload.update(schema_version=1, stage="preparation", launch_confirmed=False, model="GPT-6 Luna")
            def age(key):
                return max(0, (now - timestamp(payload[key])).total_seconds()) if payload[key] else None
            heartbeat_age = age("heartbeat_at")
            source_age = age("source_updated_at")
            return {
                **payload, "connected": self.last is not None, "feed_error": error,
                "heartbeat_age_seconds": heartbeat_age, "source_age_seconds": source_age,
                "stale": error is not None or heartbeat_age is None or heartbeat_age > 90,
                "source_stale": source_age is None or source_age > 90,
                "server_time": now.isoformat(), "agent_cap_usd": 50, "world_cap_usd": 100,
            }


def make_server(host, port, feed):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/api/status":
                return self.respond(json.dumps(feed.read()).encode(), "application/json")
            files = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                     "/style.css": ("style.css", "text/css"), "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
                     "/icon.png": ("icon.png", "image/png"), "/icon-512.png": ("icon-512.png", "image/png")}
            if path not in files:
                return self.respond(b"Not found", "text/plain", 404)
            name, mime = files[path]
            return self.respond((STATIC / name).read_bytes(), mime)

        def respond(self, body, mime, status=200):
            self.send_response(status)
            self.send_header("Content-Type", mime + ("; charset=utf-8" if not mime.startswith("image/") else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--feed-file", type=Path, required=True)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    server = make_server(args.host, args.port, Feed(args.feed_file, args.run_id))
    print(f"Luna read-only status viewer listening on {args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
