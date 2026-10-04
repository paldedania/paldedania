#!/usr/bin/env bash
# Run in an isolated profile-repository checkout after producer.py and updater.py.
set -euo pipefail
if [[ "$(git rev-parse --show-prefix)" != "" ]]; then
  echo "Run this script at the profile repository root" >&2
  exit 2
fi
if ! git diff --cached --quiet; then
  echo "Refusing to commit with pre-existing staged changes" >&2
  exit 2
fi
if ! git ls-files --error-unmatch README.md >/dev/null 2>&1; then
  echo "README.md must already be tracked" >&2
  exit 2
fi
outputs=(README.md)
for path in leetcode-updater/status.json leetcode-updater/status.json.refresh.json; do
  if git ls-files --error-unmatch "$path" >/dev/null 2>&1; then
    outputs+=("$path")
  fi
done
if git diff --quiet -- "${outputs[@]}"; then
  echo "README and producer outputs unchanged; no commit"
  exit 0
fi
git add -- "${outputs[@]}"
git commit -m "Update LeetCode profile summary" -- "${outputs[@]}"
# The workflow performs the push separately; this script never pushes.
