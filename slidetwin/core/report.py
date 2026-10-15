"""Structured report (SPEC 5.3).

M1/M2 validate the structure only. Rule checks E1-E4 and coverage are added in M5.
`report_schema.json` is generated from these models (see `export_schema`); a test keeps it in
sync.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from slidetwin.core.errors import ReportInvalid

SCHEMA_PATH = Path(__file__).with_name("report_schema.json")


class _Strict(BaseModel):
    # strict: JSON integers still count as numbers, but booleans and numeric strings do not
    model_config = ConfigDict(extra="forbid", strict=True)


Ident = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")]


class Conclusion(_Strict):
    metastasis: Literal["present", "absent", "indeterminate"]
    category: Literal["macro", "micro", "itc", "none"]
    largest_deposit_mm: float | None = Field(None, ge=0, allow_inf_nan=False)
    extranodal_extension: Literal["present", "absent", "not_assessable"]


class Claim(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,32}$")
    type: Literal["finding", "measurement", "negative"]
    text: str = Field(min_length=1, max_length=2000)
    evidence: list[Ident] = Field(default_factory=list, max_length=50)
    measurement_id: Ident | None = None
    scope: str | None = Field(None, max_length=200)


class Report(_Strict):
    task: Ident
    conclusion: Conclusion
    claims: list[Claim] = Field(default_factory=list, max_length=200)
    free_text: str = Field("", max_length=20000)


def validate_report(data: Any, *, expected_task: str) -> Report:
    try:
        report = Report.model_validate(data)
    except ValidationError as err:
        parts = []
        for e in err.errors()[:5]:
            loc = ".".join(str(p) for p in e["loc"]) or "(root)"
            parts.append(f"{loc}: {e['msg']}")
        raise ReportInvalid(
            "The report does not match the schema: " + "; ".join(parts) + ".",
            hint="The report schema is served at GET /api/report_schema.",
        ) from None
    if report.task != expected_task:
        raise ReportInvalid(
            f"The report is for task '{report.task}', but this session uses '{expected_task}'.",
            hint=f'Set "task": "{expected_task}".',
        )
    ids = [c.id for c in report.claims]
    if len(ids) != len(set(ids)):
        raise ReportInvalid("Claim ids must be unique within a report.")
    return report


def export_schema() -> dict[str, Any]:
    schema = Report.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "SlideTwin report"
    return schema


def inline_schema() -> dict[str, Any]:
    """The report schema with every $ref expanded, for embedding in another document (OpenAPI)."""
    schema = export_schema()
    defs = schema.pop("$defs", {})
    schema.pop("$schema", None)

    def expand(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return expand(defs[ref.rsplit("/", 1)[1]])
            return {k: expand(v) for k, v in node.items()}
        if isinstance(node, list):
            return [expand(v) for v in node]
        return node

    expanded: dict[str, Any] = expand(schema)
    return expanded


def write_schema(path: Path = SCHEMA_PATH) -> None:
    path.write_text(json.dumps(export_schema(), indent=2) + "\n", encoding="utf-8")
