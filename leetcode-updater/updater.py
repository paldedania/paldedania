#!/usr/bin/env python3
"""Validate a saved snapshot and replace one marked README section. No network calls."""

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import difflib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

START = b"<!-- leetcode-stats:start -->"
END = b"<!-- leetcode-stats:end -->"


class InvalidInput(ValueError):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidInput(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)


def keys(value, required, optional=()):
    if not isinstance(value, dict):
        raise InvalidInput("Expected a JSON object")
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    if missing or unknown:
        raise InvalidInput(f"Missing keys: {sorted(missing)}; unknown keys: {sorted(unknown)}")


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise InvalidInput(f"{name} must be an integer >= {minimum}")
    return value


def solved_counts(counts):
    keys(counts, ("total", "easy", "medium", "hard"))
    values = [integer(counts[k], k) for k in ("total", "easy", "medium", "hard")]
    if values[0] != sum(values[1:]):
        raise InvalidInput("total must equal easy + medium + hard")
    return values


def username(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise InvalidInput("username must contain 1-64 letters, digits, underscores or hyphens")
    return value


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise InvalidInput("observed_at must be an ISO 8601 timestamp with seconds and timezone")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as error:
        raise InvalidInput("observed_at is not a valid date") from error


@dataclass(frozen=True)
class Config:
    username: str
    weekly_goal: int
    snapshot: Path
    readme: Path
    max_age_hours: int
    allow_sample: bool
    daily_goal: int | None = None
    snapshot_format: str = "snapshot"


def read_config(path):
    data = load_json(path)
    keys(data, ("username", "weekly_goal", "snapshot", "readme"),
         ("max_age_hours", "allow_sample", "daily_goal", "snapshot_format"))
    paths = []
    for name in ("snapshot", "readme"):
        if not isinstance(data[name], str) or not data[name].strip():
            raise InvalidInput(f"{name} must be a nonempty path")
        paths.append((path.parent / data[name]).resolve())
    allow = data.get("allow_sample", False)
    if type(allow) is not bool:
        raise InvalidInput("allow_sample must be true or false")
    weekly = integer(data["weekly_goal"], "weekly_goal", 1)
    daily = integer(data["daily_goal"], "daily_goal", 1) if "daily_goal" in data else None
    if daily is not None and weekly != daily * 7:
        raise InvalidInput("weekly_goal must equal daily_goal * 7 when a daily goal is supplied")
    snapshot_format = data.get("snapshot_format", "snapshot")
    if snapshot_format not in ("snapshot", "project3-status"):
        raise InvalidInput("snapshot_format must be snapshot or project3-status")
    return Config(username(data["username"]), weekly, *paths,
                  integer(data.get("max_age_hours", 48), "max_age_hours", 1), allow, daily,
                  snapshot_format)


@dataclass(frozen=True)
class Snapshot:
    username: str
    observed_at: datetime
    source: str
    total: int
    easy: int
    medium: int
    hard: int
    producer_stale: bool = False
    producer_mode: str | None = None
    refresh_failed: bool = False


def refresh_result_path(path):
    return path.with_name(path.name + ".refresh.json")


def refresh_failed(path):
    result = refresh_result_path(path)
    if not result.exists():
        return False
    data = load_json(result)
    keys(data, ("failed",))
    if type(data["failed"]) is not bool:
        raise InvalidInput("refresh result failed must be true or false")
    return data["failed"]


def read_project3_status(data, config, now):
    """Consume Service.view()/GET /api/status without inventing an observation time."""
    keys(data, ("schema_version", "username", "mode", "source", "counts", "last_success",
                "last_attempt", "stale", "error", "refresh_seconds", "timezone",
                "next_refresh", "weekly"))
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise InvalidInput("Unsupported project 3 status schema")
    name = username(data["username"])
    if name.casefold() != config.username.casefold():
        raise InvalidInput("Producer username does not match configured username")
    mode = data["mode"]
    sources = {"live": "https://leetcode.com/graphql", "manual": "user-reported counts",
               "sample": "fictional fixture"}
    if not isinstance(mode, str) or mode not in sources or data["source"] != sources[mode]:
        raise InvalidInput("Unsupported project 3 source/mode")
    if mode == "sample" and not config.allow_sample:
        raise InvalidInput("Sample data requires allow_sample=true")
    if type(data["stale"]) is not bool or (data["error"] is not None and
                                             not isinstance(data["error"], str)):
        raise InvalidInput("Invalid producer freshness/error fields")
    if data["error"] and not data["stale"]:
        raise InvalidInput("A failed producer must report stale data")
    observed = timestamp(data["last_success"])
    if observed > now:
        raise InvalidInput("Producer observation timestamp is in the future")
    values = solved_counts(data["counts"])
    # Widget goals and weekly deltas are separate from this profile's targets.
    return Snapshot(name, observed, "project3", *values, data["stale"], mode)


def read_snapshot(path, config, now):
    data = load_json(path)
    if config.snapshot_format == "project3-status":
        return replace(read_project3_status(data, config, now), refresh_failed=refresh_failed(path))
    keys(data, ("schema_version", "username", "observed_at", "source", "solved"))
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise InvalidInput("Unsupported schema_version; expected 1")
    name = username(data["username"])
    if name != config.username:
        raise InvalidInput("Snapshot username does not match configured username")
    source = data["source"]
    if source not in ("sample", "manual", "project3"):
        raise InvalidInput("source must be sample, manual or project3")
    if source == "sample" and not config.allow_sample:
        raise InvalidInput("Sample data requires allow_sample=true; never relabel it as real data")
    observed = timestamp(data["observed_at"])
    if observed > now:
        raise InvalidInput("Snapshot timestamp is in the future")
    values = solved_counts(data["solved"])
    return Snapshot(name, observed, source, *values, refresh_failed=refresh_failed(path))


def render(snapshot, config, now):
    date = snapshot.observed_at.isoformat().replace("+00:00", "Z")
    source = {"sample": "SAMPLE DATA, fictional counts; not Pal's progress.",
              "manual": "Manually supplied snapshot; not verified against LeetCode by this updater.",
              "project3": "Project 3 snapshot; validated locally by this updater."}[snapshot.source]
    if snapshot.producer_mode is not None:
        source = {"live": "Project 3 live LeetCode response; validated by the shared producer.",
                  "manual": "Project 3 user-reported counts; not a live LeetCode observation.",
                  "sample": "SAMPLE DATA from project 3; fictional counts."}[snapshot.producer_mode]
    stale = now - snapshot.observed_at > timedelta(hours=config.max_age_hours)
    freshness = (f"STALE: older than {config.max_age_hours} hours. Last known counts retained."
                 if stale else f"Within the configured {config.max_age_hours}-hour freshness window.")
    if snapshot.refresh_failed:
        freshness = "STALE: latest refresh failed. Last known counts and observation time retained."
    elif snapshot.producer_stale:
        freshness = "STALE: project 3 reports stale data. Last known counts and observation time retained."
    daily = f"Daily goal: {config.daily_goal} problems. " if config.daily_goal is not None else ""
    return (f"### LeetCode progress\n\n{source}\n\n"
            f"Profile: [{snapshot.username}](https://leetcode.com/u/{snapshot.username}/)\n\n"
            "| Total solved | Easy | Medium | Hard |\n| ---: | ---: | ---: | ---: |\n"
            f"| {snapshot.total} | {snapshot.easy} | {snapshot.medium} | {snapshot.hard} |\n\n"
            f"{daily}Weekly goal: {config.weekly_goal} problems. These are targets; daily and weekly completions are not measured by this card.\n\n"
            f"Data observed at: {date}\n\n{freshness}\n\n"
            "Periodically updated from a saved snapshot. The timestamp is the data time, not the render time.\n")


def replace_section(original, summary):
    """Operate on bytes so CRLF, BOM and all bytes outside the markers survive."""
    original.decode("utf-8")
    if original.count(START) != 1 or original.count(END) != 1:
        raise InvalidInput("README needs exactly one start marker and one end marker")
    start, end = original.index(START), original.index(END)
    if start >= end:
        raise InvalidInput("README markers are reversed")
    for pos, marker in ((start, START), (end, END)):
        if pos and original[pos - 1:pos] != b"\n":
            raise InvalidInput("README markers must occupy their own lines")
        tail = original[pos + len(marker):]
        if tail and not (tail.startswith(b"\n") or tail.startswith(b"\r\n")):
            raise InvalidInput("README markers must occupy their own lines")
    start_end = start + len(START)
    newline = b"\r\n" if original[start_end:].startswith(b"\r\n") else b"\n"
    body = summary.encode("utf-8").replace(b"\n", newline)
    return original[:start_end] + newline + body + original[end:]


def atomic_write(path, content, expected=None):
    """Skip identical writes and refuse a README changed since it was read."""
    current = path.read_bytes() if path.exists() else None
    if expected is not None and current != expected:
        raise InvalidInput("README changed during generation; rerun and review the new diff")
    if current == content:
        return False
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        temporary.chmod(mode)
        if expected is not None and path.read_bytes() != expected:
            raise InvalidInput("README changed during generation; rerun and review the new diff")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="Apply to the configured local README")
    mode.add_argument("--draft", type=Path, help="Save a full candidate README without changing the source")
    args = parser.parse_args(argv)
    try:
        config = read_config(args.config.resolve())
        now = datetime.now(timezone.utc)
        snapshot = read_snapshot(config.snapshot, config, now)
        original = config.readme.read_bytes()
        proposed = replace_section(original, render(snapshot, config, now))
        if args.draft:
            target = args.draft.resolve()
            if target in (config.readme, config.snapshot, args.config.resolve()):
                raise InvalidInput("Draft must not overwrite the source README, snapshot or config")
            changed = atomic_write(target, proposed)
            print(f"{'Saved' if changed else 'Unchanged'} draft: {target}")
        elif args.write:
            changed = atomic_write(config.readme, proposed, expected=original)
            print("Updated marked section" if changed else "Unchanged; no write needed")
        else:
            diff = difflib.unified_diff(original.decode("utf-8").splitlines(keepends=True),
                                        proposed.decode("utf-8").splitlines(keepends=True),
                                        fromfile=str(config.readme), tofile=str(config.readme) + " (proposed)")
            sys.stdout.writelines(diff)
            if proposed == original:
                print("Unchanged; no write needed")
        return 0
    except (OSError, ValueError, TypeError, OverflowError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
