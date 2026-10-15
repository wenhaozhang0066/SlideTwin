"""Rule checks E1-E4: at least one passing and one failing case each (SPEC 11), plus enforcement."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from slidetwin.core.config import ConfigStore, Protocol
from slidetwin.core.coverage import build_tissue_map, seen_cells
from slidetwin.core.errors import RuleViolation
from slidetwin.core.geometry import BBox
from slidetwin.core.registry import SlideRegistry
from slidetwin.core.replay import replay
from slidetwin.core.report import Report
from slidetwin.core.rules import (
    MeasurementFact,
    ViewFact,
    check_e1,
    check_e2,
    check_e3,
    check_e4,
    check_report,
)
from tests.conftest import SessionFactory
from tests.helpers.synth import tissue_source

BASE: dict[str, Any] = {
    "task": "sln_metastasis",
    "conclusion": {
        "metastasis": "present",
        "category": "micro",
        "largest_deposit_mm": 1.3,
        "extranodal_extension": "absent",
    },
    "claims": [],
    "free_text": "",
}


def make_report(claims: list[dict[str, Any]] | None = None, **conclusion: Any) -> Report:
    data = copy.deepcopy(BASE)
    data["claims"] = claims or []
    data["conclusion"].update(conclusion)
    return Report.model_validate(data)


VIEWS = {
    "v_0001": ViewFact("v_0001", 1.25, BBox(0, 0, 8192, 6144)),
    "v_0002": ViewFact("v_0002", 10.0, BBox(0, 0, 1024, 768)),
    "v_0003": ViewFact("v_0003", 20.0, BBox(0, 0, 512, 384)),
}
MEAS = {
    "m_0001": MeasurementFact("m_0001", 1300.0, 650.0),  # 1.3 mm over 650 px
    "m_0002": MeasurementFact("m_0002", 150.0, 12.0),  # spans too few pixels
    "m_0003": MeasurementFact("m_0003", 2600.0, 1300.0),
}


def statuses(checks: list[Any]) -> list[str]:
    return [c.status for c in checks]


# --- E1 ----------------------------------------------------------------------------------------


def test_e1() -> None:
    ok = make_report([{"id": "c1", "type": "finding", "text": "t", "evidence": ["v_0002"]}])
    assert statuses(check_e1(ok, VIEWS, MEAS)) == ["pass"]
    bad = make_report(
        [{"id": "c1", "type": "measurement", "text": "t", "measurement_id": "m_0099"}]
    )
    checks = check_e1(bad, VIEWS, MEAS)
    assert statuses(checks) == ["fail"]
    assert "m_0099" in checks[0].message
    assert statuses(check_e1(make_report(), VIEWS, MEAS)) == ["not_applicable"]


# --- E2 ----------------------------------------------------------------------------------------


def test_e2() -> None:
    def finding(*ev: str) -> Report:
        return make_report([{"id": "f", "type": "finding", "text": "t", "evidence": list(ev)}])

    assert statuses(check_e2(finding("v_0002", "v_0003"), VIEWS, 20)) == ["pass"]
    low = check_e2(finding("v_0002"), VIEWS, 20)
    assert statuses(low) == ["fail"]
    assert "at most at 10x" in low[0].message
    assert statuses(check_e2(finding(), VIEWS, 20)) == ["fail"]
    assert statuses(check_e2(make_report(), VIEWS, 20)) == ["not_applicable"]


# --- E3 ----------------------------------------------------------------------------------------


def measure_claim(mid: str) -> dict[str, Any]:
    return {"id": "m", "type": "measurement", "text": "largest deposit", "measurement_id": mid}


@pytest.mark.parametrize(
    ("claims", "conclusion", "expected"),
    [
        ([measure_claim("m_0001")], {"category": "micro"}, ["pass", "pass"]),
        ([measure_claim("m_0001")], {"category": "macro"}, ["pass", "fail"]),  # 1.3 mm is not macro
        ([measure_claim("m_0003")], {"category": "macro"}, ["pass", "pass"]),
        ([measure_claim("m_0002")], {"category": "itc"}, ["fail", "pass"]),  # 12 px < 20 px
        ([measure_claim("m_0099")], {"category": "micro"}, ["fail", "pass"]),  # falls back to 1.3
        ([], {"category": "micro", "largest_deposit_mm": 0.1}, ["not_verifiable"]),  # cell count
        ([], {"category": "itc", "largest_deposit_mm": 0.3}, ["fail"]),
        ([], {"category": "micro", "largest_deposit_mm": None}, ["not_verifiable"]),
        ([], {"metastasis": "absent", "category": "macro"}, ["fail"]),
        ([], {"metastasis": "present", "category": "none"}, ["fail"]),
        ([], {"metastasis": "absent", "category": "none"}, ["not_applicable"]),
        ([], {"metastasis": "indeterminate", "category": "none"}, ["not_applicable"]),
    ],
)
def test_e3(
    protocol: Protocol,
    claims: list[dict[str, Any]],
    conclusion: dict[str, Any],
    expected: list[str],
) -> None:
    assert statuses(check_e3(make_report(claims, **conclusion), protocol, MEAS)) == expected


def test_e3_with_categories_the_protocol_lacks(protocol: Protocol) -> None:
    from slidetwin.core.config import CategoryRule

    weird = protocol.model_copy(
        update={"categories": (CategoryRule(id="macro", limits={"gt_cm": 0.2}),)}
    )
    report = make_report(category="macro", largest_deposit_mm=3.0)
    assert statuses(check_e3(report, weird, MEAS)) == ["not_verifiable"]  # unknown limit
    report = make_report(category="micro", largest_deposit_mm=1.0)
    assert statuses(check_e3(report, weird, MEAS)) == ["not_verifiable"]  # undefined category


# --- E4 ----------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tissue(configs: ConfigStore) -> Any:
    return build_tissue_map(tissue_source(), configs.protocol("sln_metastasis").coverage)


def negative(scope: str | None) -> dict[str, Any]:
    return {"id": "n", "type": "negative", "text": "nothing here", "scope": scope}


def test_e4_whole_slide(protocol: Protocol, tissue: Any) -> None:
    absent = make_report(metastasis="absent", category="none", largest_deposit_mm=None)
    everything = seen_cells(tissue, [BBox(0, 0, 4096, 4096)])
    nothing = seen_cells(tissue, [])
    assert statuses(check_e4(absent, protocol, tissue, everything)) == ["pass"]
    fail = check_e4(absent, protocol, tissue, nothing)
    assert statuses(fail) == ["fail"]
    assert "0.0% of the tissue in the whole slide" in fail[0].message
    assert statuses(check_e4(make_report(), protocol, tissue, nothing)) == ["not_applicable"]


def test_e4_scopes(protocol: Protocol, tissue: Any) -> None:
    piece2 = tissue.pieces[1].bbox_um
    seen = seen_cells(tissue, [piece2])  # only the small piece was looked at
    report = make_report(
        [
            negative("tissue_piece:2"),
            negative("tissue_piece:1"),
            negative(f"region:{piece2.x0},{piece2.y0},{piece2.x1},{piece2.y1}"),
            negative("tissue_piece:9"),
            negative("region:5,5,5,5"),
            negative("lymph node 3"),
        ]
    )
    checks = check_e4(report, protocol, tissue, seen)
    assert statuses(checks) == ["pass", "fail", "pass", "fail", "fail", "fail"]
    assert "does not exist" in checks[3].message
    assert "empty" in checks[4].message
    assert "tissue_piece:N" in checks[5].message


# --- whole sessions ----------------------------------------------------------------------------


def screen_everything(s: Any) -> None:
    s.set_magnification(10)
    for x, y in ((512, 384), (1536, 384), (512, 1152), (1536, 1152)):
        s.move_to(x, y)


NEGATIVE_REPORT = {
    **BASE,
    "conclusion": {
        **BASE["conclusion"],
        "metastasis": "absent",
        "category": "none",
        "largest_deposit_mm": None,
    },
}


def test_advisory_receipt_and_logged_violations(make_session: SessionFactory) -> None:
    s = make_session(source=tissue_source(name="t1"), slide_id="tissue")
    pieces = s.get_thumbnail().observation.to_dict()["tissue_pieces"]  # type: ignore[union-attr]
    assert [p["piece"] for p in pieces] == [1, 2]
    r = s.submit_report(NEGATIVE_REPORT)  # nothing was seen at 10x
    receipt = r.observation.to_dict()  # type: ignore[union-attr]
    assert receipt["accepted"] is True
    assert receipt["violations"] == 1
    assert receipt["coverage"]["fraction"] == 0.0
    assert [p["piece"] for p in receipt["coverage"]["pieces"]] == [1, 2]
    assert [v["rule"] for v in r.rule_violations] == ["E4"]
    event = [e for e in s.history if e["type"] == "event"][-1]
    assert [v["rule"] for v in event["rule_violations"]] == ["E4"]


def test_enforced_refusal_then_acceptance(
    make_session: SessionFactory, protocol: Protocol, synthetic: Any
) -> None:
    enforced = protocol.model_copy(update={"enforcement": "enforced"})
    source = tissue_source(name="t2")
    s = make_session(source=source, slide_id="tissue", protocol=enforced)
    with pytest.raises(RuleViolation) as err:
        s.submit_report(NEGATIVE_REPORT)
    assert err.value.violations[0]["rule"] == "E4"
    assert "look at more tissue" in (err.value.hint or "")
    assert err.value.to_dict()["violations"][0]["status"] == "fail"
    assert not s.closed  # the reader can keep reading
    failed = [e for e in s.history if e["type"] == "event"][-1]
    assert failed["error"]["code"] == "RULE_VIOLATION"
    assert failed["rule_violations"][0]["rule"] == "E4"
    screen_everything(s)
    r = s.submit_report(NEGATIVE_REPORT)
    assert r.observation is not None
    assert r.observation.to_dict()["coverage"]["fraction"] == 1.0
    assert s.closed
    reg = SlideRegistry([], Path("."))
    reg.add_source("tissue", source, "0" * 64)
    report = replay(s.export_trajectory().text or "", reg)
    assert report.ok, report.mismatches


def test_check_report_end_to_end(protocol: Protocol, tissue: Any) -> None:
    report = make_report(
        [
            {"id": "f", "type": "finding", "text": "cluster", "evidence": ["v_0003"]},
            measure_claim("m_0001"),
            negative("tissue_piece:2"),
        ]
    )
    outcome = check_report(report, protocol=protocol, views=VIEWS, measurements=MEAS, tissue=tissue)
    assert [c.rule for c in outcome.checks] == ["E1", "E1", "E2", "E3", "E3", "E4"]
    assert outcome.coverage["at_or_above_mag"] == 10
    assert len(outcome.coverage["pieces"]) == 2
    assert [c.rule for c in outcome.failures] == ["E4"]  # piece 2 lies outside the 10x view
