"""Liveness signal for the processes that have no HTTP server.

The worker and ingestor are loops, not servers. Kubernetes already restarts a container whose
process exits, so what a probe adds here is detection of a process that is *stuck*: alive, but no
longer going round its loop.

Each pass touches a file; the probe checks how old it is. Run as a command so the probe needs no
shell in the image:

    python -m app.heartbeat --max-age 120

Milestone 7 adds a metrics endpoint, at which point this same freshness check is also exposed
over HTTP.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

DEFAULT_PATH = Path(os.environ.get("HEARTBEAT_PATH", "/tmp/convoscore-heartbeat"))  # noqa: S108


def touch(path: Path = DEFAULT_PATH) -> None:
    """Record that the loop is still turning. Never raises: a full disk must not kill the worker."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(time.time()))
    except OSError:
        pass


def age_seconds(path: Path = DEFAULT_PATH) -> float | None:
    """Seconds since the last heartbeat, or None if there has never been one."""
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def is_fresh(max_age: float, path: Path = DEFAULT_PATH) -> bool:
    age = age_seconds(path)
    return age is not None and age <= max_age


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the heartbeat file is recent.")
    parser.add_argument("--max-age", type=float, default=120.0)
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args(argv)

    age = age_seconds(args.path)
    if age is None:
        print(f"no heartbeat at {args.path}", file=sys.stderr)
        return 1
    if age > args.max_age:
        print(f"heartbeat is {age:.0f}s old, limit is {args.max_age:.0f}s", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
