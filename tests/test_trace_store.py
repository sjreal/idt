"""Tests for the local JSONL trace buffer."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from llm_app.trace_store import TraceStore


def _write_day(root: Path, day: str, records: list[dict[str, Any]]) -> Path:
    path = root / f"{day}.jsonl"
    path.write_text(
        "".join(json.dumps(record, default=str) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def test_append_get_round_trip_serializes_non_json_values(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "traces")
    record = {
        "trace_id": "abc123",
        "arm": "baseline",
        "created": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "nested": {"tokens": 7},
    }

    assert store.append(record) == "abc123"

    stored = store.get("abc123")
    assert stored is not None
    assert stored["trace_id"] == "abc123"
    assert stored["arm"] == "baseline"
    assert stored["nested"] == {"tokens": 7}
    assert isinstance(stored["created"], str)
    assert store.dropped_total == 0


def test_list_is_most_recent_first_and_honors_limit(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "traces")
    for index in range(3):
        store.append({"trace_id": f"trace-{index}", "index": index})

    listed = store.list()
    assert [record["trace_id"] for record in listed] == ["trace-2", "trace-1", "trace-0"]
    assert [record["trace_id"] for record in store.list(limit=2)] == ["trace-2", "trace-1"]
    assert store.list(limit=0) == []


def test_list_orders_across_day_files(tmp_path: Path) -> None:
    root = tmp_path / "traces"
    store = TraceStore(root)
    today = datetime.now(UTC).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    _write_day(root, yesterday, [{"trace_id": "older"}])

    store.append({"trace_id": "today"})

    assert [record["trace_id"] for record in store.list()] == ["today", "older"]


def test_get_missing_trace_id_returns_none(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "traces")
    store.append({"trace_id": "present"})

    assert store.get("absent") is None


def test_prune_removes_files_older_than_retention(tmp_path: Path) -> None:
    root = tmp_path / "traces"
    store = TraceStore(root, retention_days=7)
    today = datetime.now(UTC).date()
    old_day = (today - timedelta(days=30)).isoformat()
    recent_day = (today - timedelta(days=1)).isoformat()
    _write_day(root, old_day, [{"trace_id": "too-old"}])
    _write_day(root, recent_day, [{"trace_id": "keep-me"}])

    store.prune()

    assert not (root / f"{old_day}.jsonl").exists()
    assert (root / f"{recent_day}.jsonl").exists()
    assert store.get("too-old") is None
    assert store.get("keep-me") is not None


def test_byte_cap_drops_record_that_cannot_fit(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "traces", max_bytes=10)
    trace_id = store.append({"trace_id": "huge", "payload": "x" * 100})

    assert trace_id == "huge"
    assert store.dropped_total == 1
    assert store.get("huge") is None
    assert store.list() == []


def test_byte_cap_deletes_oldest_day_file_to_make_room(tmp_path: Path) -> None:
    root = tmp_path / "traces"
    store = TraceStore(root, max_bytes=50_000_000)
    today = datetime.now(UTC).date()
    old_day = (today - timedelta(days=3)).isoformat()
    old_record = {"trace_id": "old", "payload": "x" * 200}
    old_line = _write_day(root, old_day, [old_record]).stat().st_size
    new_record = {"trace_id": "new", "payload": "y" * 100}
    new_line = len(json.dumps(new_record) + "\n")
    store.max_bytes = old_line + new_line - 1

    assert store.append(new_record) == "new"

    assert not (root / f"{old_day}.jsonl").exists()
    assert store.get("new") is not None
    assert store.get("old") is None
    assert store.dropped_total == 0


def test_malformed_lines_are_skipped(tmp_path: Path) -> None:
    root = tmp_path / "traces"
    store = TraceStore(root)
    day = datetime.now(UTC).date().isoformat()
    path = root / f"{day}.jsonl"
    valid = {"trace_id": "valid"}
    path.write_text(
        "this is not json\n\n[]\n" + json.dumps(valid) + "\n",
        encoding="utf-8",
    )

    assert store.list() == [valid]
    assert store.get("valid") == valid
    assert store.get("missing") is None


def test_append_requires_trace_id(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "traces")

    with pytest.raises(ValueError, match="trace_id"):
        store.append({"arm": "baseline"})
