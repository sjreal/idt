"""Simple durable JSON/JSONL result storage for local evaluation runs."""

import csv
import json
import os
import tempfile
from pathlib import Path
from threading import Lock
from typing import Any


class EvaluationStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._write_lock = Lock()

    def _run_dir(self, run_id: str) -> Path:
        if not run_id or any(char not in "0123456789abcdef-" for char in run_id.lower()):
            raise ValueError("Invalid evaluation run ID")
        return self.root / run_id

    def create_run(self, run_id: str, metadata: dict[str, Any]) -> None:
        run_dir = self._run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "results.jsonl").touch()
        self.write_metadata(run_id, metadata)

    def write_metadata(self, run_id: str, metadata: dict[str, Any]) -> None:
        run_dir = self._run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(metadata, indent=2, sort_keys=True)
        fd, temporary_path = tempfile.mkstemp(prefix="run-", suffix=".tmp", dir=run_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.write("\n")
            os.replace(temporary_path, run_dir / "run.json")
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)

    def append_result(self, run_id: str, result: dict[str, Any]) -> None:
        path = self._run_dir(run_id) / "results.jsonl"
        with self._write_lock, path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
            stream.write("\n")

    def list_runs(self, limit: int = 25) -> list[dict[str, Any]]:
        runs = []
        for run_path in self.root.iterdir():
            metadata_path = run_path / "run.json"
            if run_path.is_dir() and metadata_path.is_file():
                try:
                    runs.append(json.loads(metadata_path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
        return sorted(runs, key=lambda run: run.get("created_at", ""), reverse=True)[:limit]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        run_dir = self._run_dir(run_id)
        metadata_path = run_dir / "run.json"
        if not metadata_path.is_file():
            return None
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        results_path = run_dir / "results.jsonl"
        results = []
        if results_path.is_file():
            with results_path.open(encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        results.append(json.loads(line))
        return {**metadata, "results": results}

    def export_csv(self, run_id: str) -> str | None:
        run = self.get_run(run_id)
        if run is None:
            return None
        rows = run["results"]
        if not rows:
            return ""
        fields = list(rows[0].keys())
        from io import StringIO

        output = StringIO()
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue()
