# Shared producer

`widget.py` is an unchanged copy of project 03's `widget.py`. Project 04 imports its `Service`, `fetch_profile` and `validate_profile` for GitHub Actions. This avoids a second LeetCode fetcher. Local capture can instead read the running widget's `/api/status`.

`provenance.json` records the source path and SHA-256. To update this copy, inspect project 03's current status contract, copy the source here without editing it, update the provenance, and run the integration tests before redeploying. No automatic synchronization writes into project 03.
