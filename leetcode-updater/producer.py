#!/usr/bin/env python3
"""Capture project 03 status, or refresh with its unchanged fetching/validation code."""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from urllib.request import urlopen

import updater

ROOT = Path(__file__).resolve().parent


def load_widget(path):
    # Importing a producer must not write into project 03's working folder.
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("shared_leetcode_widget", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def live_status(config, module_path):
    widget = load_widget(module_path)
    # Isolated temporary service state. No local widget config/cache is changed.
    with tempfile.TemporaryDirectory(prefix="leetcode-profile-") as directory:
        folder = Path(directory)
        producer_config = {"username": config.username, "mode": "live",
                           "weekly_goal": config.weekly_goal, "refresh_seconds": 86400,
                           "timezone": "Asia/Kolkata"}
        widget.write_json(folder / "config.json", widget.validate_config(producer_config))
        return widget.Service(folder / "config.json", folder / "state.json").refresh()


def url_status(url):
    with urlopen(url, timeout=20) as response:
        payload = response.read(1_000_001)
    if len(payload) > 1_000_000:
        raise ValueError("Producer response exceeds one megabyte")
    return json.loads(payload, object_pairs_hook=updater.unique_object)


def save_json(path, value):
    updater.atomic_write(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode())


def capture(config, fetch):
    """Keep the last usable input on failure; persist a separate failure flag."""
    result_path = updater.refresh_result_path(config.snapshot)
    try:
        data = fetch()
        now = datetime.now(timezone.utc)
        candidate = updater.read_project3_status(data, config, now)
        if config.snapshot.exists():
            try:
                previous = updater.read_project3_status(updater.load_json(config.snapshot), config, now)
            except (OSError, ValueError, TypeError, OverflowError):
                previous = None
            if previous and (candidate.observed_at < previous.observed_at or
                             (candidate.observed_at == previous.observed_at and
                              (candidate.total, candidate.easy, candidate.medium, candidate.hard) !=
                              (previous.total, previous.easy, previous.medium, previous.hard))):
                raise ValueError("Producer observation moved backwards or changed without a new timestamp")
        save_json(config.snapshot, data)
        failed = candidate.producer_stale
        save_json(result_path, {"failed": failed})
        if failed:
            print("Producer reports stale data; retained its last successful observation.", file=sys.stderr)
            return 1
        print(f"Producer observation: {data['last_success']}; counts: {data['counts']}")
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
        save_json(result_path, {"failed": True})
        print(f"Refresh failed: {error}. Previous snapshot retained.", file=sys.stderr)
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--status-url", help="Read project 03's existing /api/status response")
    source.add_argument("--widget-module", type=Path, default=ROOT / "vendor/widget.py",
                        help="Use project 03's fetching/validation module for a new live observation")
    args = parser.parse_args(argv)
    try:
        config = updater.read_config(args.config.resolve())
        if config.snapshot_format != "project3-status":
            raise ValueError("Producer requires snapshot_format=project3-status")
        outputs = {config.snapshot, updater.refresh_result_path(config.snapshot)}
        if outputs & {config.readme, args.config.resolve(), args.widget_module.resolve()}:
            raise ValueError("Producer outputs must not overwrite the README, config or module")
        fetch = (lambda: url_status(args.status_url)) if args.status_url else (
            lambda: live_status(config, args.widget_module.resolve()))
        return capture(config, fetch)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        print(f"Producer error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
