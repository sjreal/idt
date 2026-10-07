"""Load and verify the small, fixed, project-authored evaluation corpus."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    kind: Literal["attack", "benign"]
    category: str
    prompt: str
    expected_terms: tuple[str, ...] = ()
    success_markers: tuple[str, ...] = ()
    history: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class EvaluationDataset:
    version: str
    sha256: str
    manifest: dict[str, object]
    cases: tuple[EvaluationCase, ...]


def load_dataset(manifest_path: Path) -> EvaluationDataset:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    data_path = manifest_path.parent / str(manifest["file"])
    payload = data_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    expected_digest = str(manifest["sha256"])
    if digest != expected_digest:
        raise ValueError(
            f"Evaluation dataset checksum mismatch: expected {expected_digest}, got {digest}"
        )

    cases: list[EvaluationCase] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(payload.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record = json.loads(line)
        case_id = str(record["id"])
        kind = record["kind"]
        if case_id in seen_ids:
            raise ValueError(f"Duplicate evaluation case id {case_id!r} on line {line_number}")
        if kind not in {"attack", "benign"}:
            raise ValueError(f"Invalid kind {kind!r} on line {line_number}")
        history = tuple(
            {"role": str(message["role"]), "content": str(message["content"])}
            for message in record.get("history", [])
        )
        if any(message["role"] not in {"user", "assistant"} for message in history):
            raise ValueError(f"Invalid conversation history role on line {line_number}")
        seen_ids.add(case_id)
        cases.append(
            EvaluationCase(
                id=case_id,
                kind=kind,
                category=str(record["category"]),
                prompt=str(record["prompt"]),
                expected_terms=tuple(str(term) for term in record.get("expected_terms", [])),
                success_markers=tuple(str(marker) for marker in record.get("success_markers", [])),
                history=history,
            )
        )

    if len(cases) != int(manifest["records"]):
        raise ValueError(
            f"Dataset record count mismatch: expected {manifest['records']}, got {len(cases)}"
        )
    return EvaluationDataset(
        version=str(manifest["version"]),
        sha256=digest,
        manifest=manifest,
        cases=tuple(cases),
    )
