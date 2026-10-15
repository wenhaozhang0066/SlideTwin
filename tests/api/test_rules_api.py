"""M5 through REST: receipts with rule checks and coverage; enforced protocols refuse reports."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml

from tests.api.conftest import ClientFactory, open_session
from tests.conftest import CONFIG_DIR
from tests.core.test_report import EXAMPLE

NEGATIVE = {
    **EXAMPLE,
    "conclusion": {
        "metastasis": "absent",
        "category": "none",
        "largest_deposit_mm": None,
        "extranodal_extension": "not_assessable",
    },
    "claims": [{"id": "n1", "type": "negative", "text": "no deposits", "scope": "tissue_piece:1"}],
}


def enforced_configs(tmp_path: Path) -> Path:
    target = tmp_path / "cfg"
    shutil.copytree(CONFIG_DIR, target)
    path = target / "protocols" / "sln_metastasis.yaml"
    data = yaml.safe_load(path.read_text())
    data["enforcement"] = "enforced"
    path.write_text(yaml.safe_dump(data))
    return target


def test_receipt_carries_rule_checks_and_coverage(make_client: ClientFactory) -> None:
    client = make_client()
    sid = open_session(client)["session_id"]
    thumb = client.post(f"/api/sessions/{sid}/thumbnail").json()["observation"]
    assert thumb["tissue_pieces"] == []  # the line slide has no tissue
    r = client.post(f"/api/sessions/{sid}/report", json={"report": EXAMPLE}).json()
    receipt = r["observation"]
    assert {c["rule"] for c in receipt["rule_checks"]} == {"E1", "E2", "E3", "E4"}
    assert receipt["violations"] == len(r["rule_violations"])
    assert receipt["coverage"]["grid_um"] == 32


def test_enforced_protocol_refuses_with_reasons(make_client: ClientFactory, tmp_path: Path) -> None:
    client = make_client(config_dir=enforced_configs(tmp_path))
    sid = open_session(client)["session_id"]
    resp = client.post(f"/api/sessions/{sid}/report", json={"report": NEGATIVE})
    assert resp.status_code == 422
    body: dict[str, Any] = resp.json()["error"]
    assert body["code"] == "RULE_VIOLATION"
    assert body["violations"][0]["rule"] == "E4"
    assert "tissue piece 1 does not exist" in body["violations"][0]["message"]
    assert body["seq"] == 2
    state = client.get(f"/api/sessions/{sid}/state").json()["state"]
    assert state["closed"] is False
