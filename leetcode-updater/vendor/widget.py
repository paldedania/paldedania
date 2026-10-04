#!/usr/bin/env python3
"""Local LeetCode progress service. Python 3.11+, standard library only."""
import argparse
import copy
import fcntl
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
ENDPOINT = "https://leetcode.com/graphql"
QUERY = """query WidgetProgress($username: String!) {
  matchedUser(username: $username) {
    username
    submitStats { acSubmissionNum { difficulty count } }
  }
}"""


def utc_now():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("Timestamp must be an ISO date and time with a timezone")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return result


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    """Replace one complete document so interruption cannot leave half a cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".widget-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    name = config.get("username", "")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError("Username must contain 1–64 letters, numbers, underscores or hyphens")
    if name == "YOUR_LEETCODE_USERNAME":
        raise ValueError("Replace YOUR_LEETCODE_USERNAME with your profile username")
    if config.get("mode") not in ("live", "sample", "manual"):
        raise ValueError("Mode must be live, sample or manual")
    if config["mode"] == "sample" and name != "sample-pal":
        raise ValueError("Sample mode uses sample-pal to keep fictional data separate")
    if config["mode"] == "manual" and not isinstance(config.get("manual_file"), str):
        raise ValueError("Manual mode needs a manual_file path")
    for key, low, high in (("weekly_goal", 1, 10000), ("refresh_seconds", 60, 86400)):
        if type(config.get(key)) is not int or not low <= config[key] <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}")
    if not isinstance(config.get("timezone"), str):
        raise ValueError("timezone must name an IANA timezone, such as Asia/Kolkata")
    try:
        ZoneInfo(config["timezone"])
    except KeyError as error:
        raise ValueError("Unknown timezone; use an IANA name such as Asia/Kolkata") from error
    return config


def validate_profile(raw, username):
    if not isinstance(raw, dict) or raw.get("errors"):
        raise ValueError("LeetCode returned an error response")
    try:
        user = raw["data"]["matchedUser"]
        if user is None:
            raise ValueError("LeetCode username was not found")
        if user["username"].casefold() != username.casefold():
            raise ValueError("Response belongs to another username")
        counts = {}
        for entry in user["submitStats"]["acSubmissionNum"]:
            difficulty, count = entry["difficulty"], entry["count"]
            if difficulty not in ("All", "Easy", "Medium", "Hard") or difficulty in counts:
                raise ValueError("Unexpected or duplicate difficulty")
            if type(count) is not int or count < 0:
                raise ValueError("Solved counts must be nonnegative integers")
            counts[difficulty] = count
        if set(counts) != {"All", "Easy", "Medium", "Hard"}:
            raise ValueError("Response is missing a difficulty count")
        if counts["All"] != counts["Easy"] + counts["Medium"] + counts["Hard"]:
            raise ValueError("Total solved does not equal the difficulty counts")
        return {"total": counts["All"], "easy": counts["Easy"],
                "medium": counts["Medium"], "hard": counts["Hard"]}
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("LeetCode response has an unsupported shape") from error


def fetch_profile(config):
    if config["mode"] == "sample":
        return read_json(ROOT / "samples/profile.json")
    if config["mode"] == "manual":
        try:
            manual = read_json(Path(config["manual_file"]))
            counts = manual["counts"]
            return {"data": {"matchedUser": {"username": manual["username"],
                    "submitStats": {"acSubmissionNum": [
                        {"difficulty": difficulty, "count": counts[key]}
                        for difficulty, key in (("All", "total"), ("Easy", "easy"),
                                                ("Medium", "medium"), ("Hard", "hard"))]}}},
                    "manual_reported_at": manual["reported_at"]}
        except (KeyError, TypeError) as error:
            raise ValueError("Manual file needs username, reported_at and four solved counts") from error
    body = json.dumps({"query": QUERY, "variables": {"username": config["username"]}}).encode()
    request = Request(ENDPOINT, data=body, headers={
        "Content-Type": "application/json", "Referer": "https://leetcode.com",
        "User-Agent": "LocalLeetCodeWidget/1.0"})
    with urlopen(request, timeout=20) as response:
        payload = response.read(1_000_001)
    if len(payload) > 1_000_000:
        raise ValueError("Response exceeds the one-megabyte limit")
    return json.loads(payload)


def week_id(moment, tz):
    local = moment.astimezone(ZoneInfo(tz))
    return (local.date() - timedelta(days=local.weekday())).isoformat()


class Service:
    def __init__(self, config_path, state_path, fetch=fetch_profile, now=utc_now):
        self.config_path, self.state_path = Path(config_path), Path(state_path)
        self.fetch, self.now = fetch, now
        self.mutex = threading.RLock()

    def config(self):
        return validate_config(read_json(self.config_path))

    @contextmanager
    def locked(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.mutex, open(str(self.state_path) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def state(self):
        if not self.state_path.exists():
            return {"schema_version": 1, "profiles": {}}
        state = read_json(self.state_path)
        if not isinstance(state, dict) or state.get("schema_version") != 1 or not isinstance(state.get("profiles"), dict):
            raise ValueError("Unsupported cache schema; preserve the file before recovering")
        return state

    def identity(self, config):
        return config["mode"] + ":" + config["username"].casefold()

    def refresh(self, offline=False, only_if_due=False):
        with self.locked():
            config, state = self.config(), self.state()
            record = state["profiles"].setdefault(self.identity(config), {
                "snapshots": {}, "weeks": {}, "current": None,
                "last_attempt": None, "error": None})
            now = self.now()
            if only_if_due and record["last_attempt"]:
                age = (now - parse_time(record["last_attempt"])).total_seconds()
                if 0 <= age < config["refresh_seconds"]:
                    return self.view(config, record, now)
            record["last_attempt"] = stamp(now)
            try:
                if offline:
                    raise OSError("Offline simulation: network request skipped")
                raw = self.fetch(config)
                counts = validate_profile(raw, config["username"])
                now = self.now()
                observed = now
                if config["mode"] == "manual":
                    observed = parse_time(raw["manual_reported_at"])
                    if observed > now:
                        raise ValueError("Manual reported_at cannot be in the future")
                    previous = record["current"]
                    if previous and observed < parse_time(previous["updated_at"]):
                        raise ValueError("Manual reported_at cannot move backwards")
                    if previous and stamp(observed) == previous["updated_at"] and counts != previous["counts"]:
                        raise ValueError("Update reported_at when you change manual counts")
                snapshot = {"updated_at": stamp(observed), "counts": counts}
                # One latest snapshot per local date, one fixed baseline per week.
                day = observed.astimezone(ZoneInfo(config["timezone"])).date().isoformat()
                record["snapshots"][config["timezone"] + ":" + day] = snapshot
                week_key = config["timezone"] + ":" + week_id(observed, config["timezone"])
                record["weeks"].setdefault(week_key, copy.deepcopy(snapshot))
                record["current"] = {**snapshot, "raw_response": raw}
                record["error"] = None
            except (OSError, ValueError) as error:
                # Retain the previous successful counts, raw response and history.
                record["error"] = str(error)
            write_json(self.state_path, state)
            return self.view(config, record, now)

    def status(self):
        with self.locked():
            config, state = self.config(), self.state()
            return self.view(config, state["profiles"].get(self.identity(config), {}), self.now())

    def view(self, config, record, now):
        current = record.get("current")
        updated = current["updated_at"] if current else None
        stale = bool(record.get("error")) or not updated
        if updated:
            age = (now - parse_time(updated)).total_seconds()
            stale = stale or age >= config["refresh_seconds"] or age < 0
        start = week_id(now, config["timezone"])
        baseline = record.get("weeks", {}).get(config["timezone"] + ":" + start)
        delta = current["counts"]["total"] - baseline["counts"]["total"] if current and baseline else None
        attempted = record.get("last_attempt")
        next_refresh = parse_time(attempted) + timedelta(seconds=config["refresh_seconds"]) if attempted else now
        return {"schema_version": 1, "username": config["username"], "mode": config["mode"],
                "source": {"sample": "fictional fixture", "manual": "user-reported counts", "live": ENDPOINT}[config["mode"]],
                "counts": current["counts"] if current else None,
                "last_success": updated, "last_attempt": record.get("last_attempt"),
                "stale": stale, "error": record.get("error"),
                "refresh_seconds": config["refresh_seconds"], "timezone": config["timezone"],
                "next_refresh": stamp(next_refresh),
                "weekly": {"start": start, "goal": config["weekly_goal"],
                           "observed_change": delta,
                           "baseline_at": baseline["updated_at"] if baseline else None,
                           "note": "Net change since the first observation this week. Earlier solves are unknown."}}


class WidgetServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, service):
        self.service = service
        super().__init__(("127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, kind="application/json"):
        data = json.dumps(body).encode() if kind == "application/json" else body
        self.send_response(code)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def allowed_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        if not self.allowed_host():
            return self.reply(403, {"error": "Only localhost requests are accepted"})
        path = urlparse(self.path).path
        if path == "/api/status":
            try:
                return self.reply(200, self.server.service.status())
            except (OSError, ValueError, KeyError) as error:
                return self.reply(500, {"error": str(error)})
        files = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                 "/style.css": ("style.css", "text/css")}
        if path in files:
            name, kind = files[path]
            return self.reply(200, (ROOT / "web" / name).read_bytes(), kind)
        self.reply(404, {"error": "Not found"})

    def do_POST(self):
        if not self.allowed_host():
            return self.reply(403, {"error": "Only localhost requests are accepted"})
        origin = self.headers.get("Origin")
        if self.headers.get("X-Widget-Action") != "refresh" or (origin and origin != "http://" + self.headers.get("Host", "")):
            return self.reply(403, {"error": "Refresh must come from this widget"})
        if self.path != "/api/refresh":
            return self.reply(404, {"error": "Not found"})
        try:
            status = self.server.service.refresh()
            return self.reply(200 if not status["error"] else 502, status)
        except (OSError, ValueError, KeyError) as error:
            self.reply(500, {"error": str(error)})


def scheduler(service, stop):
    while not stop.is_set():
        try:
            service.refresh(only_if_due=True)
        except (OSError, ValueError, KeyError) as error:
            print(f"Scheduled refresh failed: {error}", flush=True)
        stop.wait(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "runtime/config.json")
    parser.add_argument("--state", type=Path, default=ROOT / "runtime/state.json")
    sub = parser.add_subparsers(dest="action", required=True)
    configure = sub.add_parser("configure", help="Create or update configuration; preserve cached profiles")
    configure.add_argument("--username", default="sample-pal")
    configure.add_argument("--mode", choices=["sample", "live", "manual"], default="sample")
    configure.add_argument("--manual-file", type=Path, default=ROOT / "runtime/manual-input.json")
    configure.add_argument("--goal", type=int, default=5)
    configure.add_argument("--interval", type=int, default=60)
    configure.add_argument("--timezone", default="Asia/Kolkata")
    refresh = sub.add_parser("refresh")
    refresh.add_argument("--offline", action="store_true", help="Exercise cache preservation without a network request")
    sub.add_parser("status")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8763)
    args = parser.parse_args()
    try:
        if args.action == "configure":
            config = validate_config({"username": args.username, "mode": args.mode,
                                     "weekly_goal": args.goal, "refresh_seconds": args.interval,
                                     "timezone": args.timezone, **({"manual_file": str(args.manual_file.resolve())} if args.mode == "manual" else {})})
            write_json(args.config, config)
            print(f"Saved {args.mode} configuration to {args.config}")
            return 0
        service = Service(args.config, args.state)
        service.config()
        if args.action in ("refresh", "status"):
            result = service.refresh(offline=args.offline) if args.action == "refresh" else service.status()
            print(json.dumps(result, indent=2))
            return 1 if args.action == "refresh" and result["error"] else 0
        server = WidgetServer(args.port, service)
        stop = threading.Event()
        worker = threading.Thread(target=scheduler, args=(service, stop), daemon=True)
        worker.start()
        print(f"Widget: http://127.0.0.1:{server.server_port} (Ctrl+C to stop)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            server.server_close()
        return 0
    except (OSError, ValueError, KeyError) as error:
        parser.exit(2, f"Widget error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
