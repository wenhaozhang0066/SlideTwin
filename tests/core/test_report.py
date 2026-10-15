from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from slidetwin.core.errors import ReportInvalid
from slidetwin.core.report import SCHEMA_PATH, export_schema, validate_report, write_schema

# The example from SPEC 5.3
EXAMPLE: dict[str, Any] = {
    "task": "sln_metastasis",
    "conclusion": {
        "metastasis": "present",
        "category": "micro",
        "largest_deposit_mm": 1.3,
        "extranodal_extension": "absent",
    },
    "claims": [
        {
            "id": "c1",
            "type": "finding",
            "text": "Cohesive cluster of atypical epithelial cells in the subcapsular sinus",
            "evidence": ["v_0042", "v_0043"],
        },
        {
            "id": "c2",
            "type": "measurement",
            "text": "Largest contiguous deposit",
            "measurement_id": "m_0003",
        },
        {
            "id": "c3",
            "type": "negative",
            "text": "No other deposits identified in tissue piece 2",
            "scope": "tissue_piece:2",
        },
    ],
    "free_text": "...",
}


def test_schema_file_is_in_sync() -> None:
    assert json.loads(SCHEMA_PATH.read_text()) == export_schema()


def test_write_schema(tmp_path: Path) -> None:
    out = tmp_path / "schema.json"
    write_schema(out)
    assert json.loads(out.read_text()) == export_schema()


def test_spec_example_is_valid() -> None:
    report = validate_report(EXAMPLE, expected_task="sln_metastasis")
    assert report.conclusion.category == "micro"
    assert [c.id for c in report.claims] == ["c1", "c2", "c3"]


def mutate(path: list[Any], value: Any) -> dict[str, Any]:
    data = copy.deepcopy(EXAMPLE)
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return data


@pytest.mark.parametrize(
    "data",
    [
        mutate(["conclusion", "metastasis"], "maybe"),
        mutate(["conclusion", "category"], "huge"),
        mutate(["conclusion", "largest_deposit_mm"], -1),
        mutate(["conclusion", "extranodal_extension"], "yes"),
        mutate(["claims", 0, "type"], "opinion"),
        mutate(["claims", 0, "text"], ""),
        mutate(["extra"], 1),
        mutate(["claims", 1, "id"], "c1"),
        "not an object",
    ],
)
def test_invalid_reports(data: Any) -> None:
    with pytest.raises(ReportInvalid):
        validate_report(data, expected_task="sln_metastasis")


def test_wrong_task() -> None:
    with pytest.raises(ReportInvalid, match="this session uses 'other'"):
        validate_report(EXAMPLE, expected_task="other")
