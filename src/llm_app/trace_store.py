"""Local JSONL trace buffer used by the trace viewer when OTLP is stopped."""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DAY_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")


def _utc_today() -> date:
    """Return the current UTC date used for per-day file names."""
    return datetime.now(UTC).date()


class TraceStore:
    """Append-only, per-day JSONL store with retention and a byte cap.

    Reads and writes are guarded by a re-entrant lock so a single process can
    use the store from multiple threads without interleaved lines.
    """

    def __init__(
        self,
        root: Path,
        retention_days: int = 7,
        max_bytes: int = 50_000_000,
    ) -> None:
        self.root = Path(root)
        self.retention_days = retention_days
        self.max_bytes = max_bytes
        self._dropped = 0
        self._lock = threading.RLock()
        # The directory is created lazily. The local trace buffer is a
        # convenience layer, so an unwritable path must not stop the API from
        # starting; append() creates it and reports failures without raising.
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create the trace directory %s: %s", self.root, exc)

    @property
    def dropped_total(self) -> int:
        """Number of records rejected because they could not be stored."""
        return self._dropped

    def _day_files(self) -> list[Path]:
        """Return day files ordered from oldest to newest by file name."""
        if not self.root.exists():
            return []
        return sorted(
            (
                path
                for path in self.root.iterdir()
                if path.is_file() and _DAY_FILE_RE.match(path.name)
            ),
            key=lambda path: path.name,
        )

    @staticmethod
    def _parse(line: str) -> dict[str, Any] | None:
        """Parse one JSON line, returning None for blank or malformed lines."""
        stripped = line.strip()
        if not stripped:
            return None
        try:
            record = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            return None
        return record if isinstance(record, dict) else None

    def append(self, record: dict[str, Any]) -> str:
        """Store one record in today's JSONL file and return its trace id."""
        trace_id = record.get("trace_id")
        if not isinstance(trace_id, str) or not trace_id:
            raise ValueError("record must contain a non-empty 'trace_id'")

        line = json.dumps(record, default=str) + "\n"
        line_size = len(line.encode("utf-8"))
        with self._lock:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._dropped += 1
                logger.warning("Could not create the trace directory %s: %s", self.root, exc)
                raise OSError(f"Could not create the trace directory {self.root}") from exc
            self.prune()
            files = self._day_files()
            total = sum(path.stat().st_size for path in files)

            while files and total + line_size > self.max_bytes:
                oldest = files.pop(0)
                try:
                    removed_bytes = oldest.stat().st_size
                    oldest.unlink()
                except OSError as exc:
                    logger.warning("Could not remove old trace file %s: %s", oldest, exc)
                    break
                total -= removed_bytes

            if total + line_size > self.max_bytes:
                self._dropped += 1
                logger.debug("Dropped trace %s: byte cap reached", trace_id)
                return trace_id

            target = self.root / f"{_utc_today().isoformat()}.jsonl"
            with target.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
            return trace_id

    def get(self, trace_id: str) -> dict[str, Any] | None:
        """Return the newest record with the given trace id, or None."""
        with self._lock:
            for path in reversed(self._day_files()):
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeDecodeError) as exc:
                    logger.warning("Could not read trace file %s: %s", path, exc)
                    continue
                for raw in reversed(lines):
                    record = self._parse(raw)
                    if record is not None and record.get("trace_id") == trace_id:
                        return record
            return None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return up to ``limit`` records, most recent first."""
        if limit <= 0:
            return []
        results: list[dict[str, Any]] = []
        with self._lock:
            for path in reversed(self._day_files()):
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeDecodeError) as exc:
                    logger.warning("Could not read trace file %s: %s", path, exc)
                    continue
                for raw in reversed(lines):
                    record = self._parse(raw)
                    if record is not None:
                        results.append(record)
                        if len(results) >= limit:
                            return results
            return results

    def prune(self) -> None:
        """Delete day files older than the configured retention window."""
        cutoff = _utc_today() - timedelta(days=self.retention_days)
        with self._lock:
            for path in self._day_files():
                try:
                    day = date.fromisoformat(path.name[:10])
                except ValueError:
                    continue
                if day < cutoff:
                    try:
                        path.unlink()
                    except OSError as exc:
                        logger.warning("Could not prune trace file %s: %s", path, exc)
