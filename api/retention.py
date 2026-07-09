"""Retention sweep: delete run partitions older than a cutoff.

Date-partitioning (api.runs_io) makes this a file operation -- drop whole `<stream>-<day>.jsonl`
files past the retention window -- rather than rewriting an append-only log. Run it on a
schedule (cron, a Fly scheduled machine, a GitHub Action); it's a no-op until data actually ages
past the window.

CRITICAL GUARD: retention MUST outlast your experiment horizon plus your outcome-reporting lag.
Outcomes arrive at/after the horizon (e.g. 30 days) via POST /outcomes and join back to the
resolution row; prune resolutions before their outcomes land and the causal readout silently
loses those cells. So the sweep refuses a window shorter than `min_days` (default 45, comfortably
past the 30d default horizon) unless explicitly forced. Default retention is 90 days.

    OFFBOARD_RUNS_DIR=/data/runs python -m api.retention [--days 90] [--force]
"""

import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from .runs_io import STREAMS, _SUFFIX

DEFAULT_RETENTION_DAYS = 90
DEFAULT_MIN_DAYS = 45  # floor: must clear the 30d horizon + reporting lag before we allow a prune


def _partition_date(name: str, stem: str) -> Optional[date]:
    """Parse the day out of `<stem>-YYYY-MM-DD.jsonl`, or None if it isn't such a partition
    (e.g. a legacy monolithic `<stem>.jsonl`, which carries no date and is never auto-pruned)."""
    prefix = f"{stem}-"
    if not name.startswith(prefix) or not name.endswith(_SUFFIX):
        return None
    try:
        return date.fromisoformat(name[len(prefix): -len(_SUFFIX)])
    except ValueError:
        return None


def prune(
    directory: str,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    now: Optional[date] = None,
    min_days: int = DEFAULT_MIN_DAYS,
    force: bool = False,
    streams=STREAMS,
) -> list[Path]:
    """Delete partitions whose day is older than `retention_days` before `now`. Returns the
    deleted paths. Raises ValueError if `retention_days < min_days` and not `force` -- the guard
    that stops a too-tight window from dropping resolutions before their outcomes arrive."""
    if retention_days < min_days and not force:
        raise ValueError(
            f"retention_days={retention_days} is below the {min_days}-day floor; a window this "
            "short can prune resolution rows before their outcomes arrive and break the causal "
            "readout. Raise it, or pass force=True if you truly mean it."
        )
    today = now or datetime.now(timezone.utc).date()
    cutoff = today.toordinal() - retention_days
    d = Path(directory)
    if not d.exists():
        return []
    deleted: list[Path] = []
    for stem in streams:
        for path in sorted(d.glob(f"{stem}-*{_SUFFIX}")):
            day = _partition_date(path.name, stem)
            if day is not None and day.toordinal() < cutoff:
                path.unlink()
                deleted.append(path)
    return deleted


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Delete run partitions older than the retention window.")
    ap.add_argument("directory", nargs="?", default=os.getenv("OFFBOARD_RUNS_DIR", "runs"))
    ap.add_argument(
        "--days", type=int, default=int(os.getenv("OFFBOARD_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)),
        help=f"retention window in days (default {DEFAULT_RETENTION_DAYS})",
    )
    ap.add_argument("--force", action="store_true", help=f"allow a window below the {DEFAULT_MIN_DAYS}-day floor")
    args = ap.parse_args()

    try:
        deleted = prune(args.directory, retention_days=args.days, force=args.force)
    except ValueError as e:
        print(f"retention refused: {e}")
        return 1
    for p in deleted:
        print(f"deleted {p.name}")
    print(f"done: {len(deleted)} partition(s) older than {args.days}d removed from {args.directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
