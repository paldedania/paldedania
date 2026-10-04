# LeetCode profile updater

Project 04 consumes project 03's status contract. producer.py imports the unchanged shared producer in vendor/widget.py. It retrieves and validates a new LeetCode response before each scheduled render.

Run from the repository root:

```bash
python3 leetcode-updater/producer.py --config leetcode-updater/config.json
python3 leetcode-updater/updater.py --config leetcode-updater/config.json
python3 leetcode-updater/updater.py --config leetcode-updater/config.json --write
```

The default render prints a diff. Only the marked README section changes. Invalid/unavailable input keeps the previous usable snapshot, sets status.json.refresh.json to failed, and exits nonzero. Rendering that saved input labels it stale. With no usable snapshot, the README stays unchanged and rendering fails. Scheduled jobs report failure even after publishing a retained stale summary.

The Actions workflow runs daily at 02:23 UTC and offers Run workflow for manual refresh. The default obtains new data. Set refresh=false only for an identical saved-input verification; this does not check LeetCode. GitHub schedules can be delayed. Targets are 3 daily and 21 weekly, separate from measured total solves. No daily/weekly completion count is claimed.

The commit guard accepts only tracked README.md, leetcode-updater/status.json and leetcode-updater/status.json.refresh.json. It refuses pre-existing staged work and skips a commit for identical outputs. A new observation time is new data even if counts are unchanged.
