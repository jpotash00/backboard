"""Layout of the append-only runs directory: date-partitioned JSONL, one file per (stream, day).

Partitioning is what makes retention and erasure possible. A monolithic append-only log has no
delete path -- you can't drop last quarter's data or one user's rows without rewriting the whole
file. With `sessions-2026-07-09.jsonl` (etc.), a day is a single file: retention is `rm` of old
partitions, and erasure is a bounded row-filter over recent ones.

Writers append to *today's* partition; readers glob *every* partition (plus any legacy
monolithic `<stem>.jsonl` from before this change) so the flywheel sees one continuous stream
and nothing is stranded during migration. Dependency-free (pathlib/json only) so both `api` and
`learning` share one definition of the layout instead of drifting.
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Union

# The three append-only streams the API produces. Retention/erasure iterate these by name.
STREAMS = ("sessions", "resolutions", "outcomes")

_SUFFIX = ".jsonl"


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def partition_path(directory: Union[str, Path], stem: str, day: Optional[date] = None) -> Path:
    """The file today's records for `stem` append to: <directory>/<stem>-<YYYY-MM-DD>.jsonl."""
    day = day or _utc_today()
    return Path(directory) / f"{stem}-{day.isoformat()}{_SUFFIX}"


def stream_files(directory: Union[str, Path], stem: str) -> list[Path]:
    """Every partition for a stream, oldest first. Includes a legacy monolithic `<stem>.jsonl`
    (first) if one exists, so pre-partition data keeps being read."""
    d = Path(directory)
    if not d.exists():
        return []
    files: list[Path] = []
    legacy = d / f"{stem}{_SUFFIX}"
    if legacy.exists():
        files.append(legacy)
    files.extend(sorted(d.glob(f"{stem}-*{_SUFFIX}")))
    return files


def read_stream(directory: Union[str, Path], stem: str) -> list[dict]:
    """All records across a stream's partitions, skipping blank/corrupt lines. This is the one
    reader the learning flywheel goes through, so it transparently spans every day's file."""
    out: list[dict] = []
    for f in stream_files(directory, stem):
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def stem_of(path: Union[str, Path]) -> str:
    """Recover a stream stem from a legacy file path like `runs/resolutions.jsonl` -> `resolutions`,
    so the file-path reader APIs in `learning` can resolve to the partitioned layout."""
    name = Path(path).name
    return name[: -len(_SUFFIX)] if name.endswith(_SUFFIX) else name
