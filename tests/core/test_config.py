from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from slidetwin.core.config import (
    ConfigStore,
    FlatCostModel,
    ViewerCostModel,
    canonical_json,
    content_hash,
    load_profile_data,
    load_protocol_data,
)
from slidetwin.core.errors import ConfigError
from tests.conftest import CONFIG_DIR


def raw(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((CONFIG_DIR / name).read_text())
    return data


def test_repo_configs_load(configs: ConfigStore) -> None:
    assert sorted(configs.profiles) == ["unconstrained", "viewer"]
    assert sorted(configs.protocols) == ["sln_metastasis"]
    viewer = configs.profile("viewer")
    assert isinstance(viewer.cost_model, ViewerCostModel)
    assert viewer.cost_model.pan_per_viewport_s == 0.15
    assert viewer.magnifications == (1.25, 2.5, 5.0, 10.0, 20.0, 40.0)
    assert not viewer.crop.allowed
    unc = configs.profile("unconstrained")
    assert isinstance(unc.cost_model, FlatCostModel)
    assert unc.crop.allowed
    assert unc.cost_model.thumbnail_s == 0.0


def test_protocol_is_draft_everywhere(configs: ConfigStore) -> None:
    proto = configs.protocol("sln_metastasis")
    assert proto.status == "draft"
    assert proto.reviewed_by is None
    assert [r.id for r in proto.general_rules] == ["R-G1", "R-G2", "R-G3", "R-G4", "R-G5"]
    assert all(r.status == "draft" for r in proto.general_rules)
    assert proto.categories["macro"] == {"gt_mm": 2.0}
    assert proto.enforcement == "advisory"
    task = proto.task_description()
    assert task["protocol_id"] == "sln_metastasis"
    assert task["report_fields"] == [
        "metastasis",
        "category",
        "largest_deposit_mm",
        "extranodal_extension",
    ]
    assert task["categories"]["micro"] == {"gt_mm": 0.2, "le_mm": 2.0, "or_cells_gt": 200.0}
    assert task["coverage"]["negative_min_fraction"] == 0.98
    assert len(task["general_rules"]) == 5


def test_content_hash_is_order_independent_and_value_sensitive() -> None:
    data = raw("profiles/viewer.yaml")
    a = load_profile_data(data)
    b = load_profile_data(dict(reversed(list(data.items()))))
    assert content_hash(a) == content_hash(b)
    changed = dict(data, mag_tolerance=0.05)
    assert content_hash(load_profile_data(changed)) != content_hash(a)
    assert canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'


@pytest.mark.parametrize(
    "patch",
    [
        {"magnifications": [10, 5]},
        {"magnifications": [5, 5]},
        {"magnifications": [-1, 5]},
        {"magnifications": []},
        {"mag_tolerance": 1.0},
        {"viewport_default_px": [32, 768]},
        {"image": {"fill_rgb": [0, 0, 300]}},
        {"cost_model": {"kind": "viewer", "view_render_s": -1}},
        {"cost_model": {"kind": "other", "step_s": 1}},
        {"budget": {"max_views": 0}},
        {"typo_field": 1},
    ],
)
def test_invalid_profiles_are_rejected(patch: dict[str, Any]) -> None:
    data = raw("profiles/viewer.yaml")
    data.update(patch)
    with pytest.raises(ConfigError) as err:
        load_profile_data(data, "viewer.yaml")
    assert "viewer.yaml" in err.value.message


def test_invalid_protocol_is_rejected() -> None:
    data = raw("protocols/sln_metastasis.yaml")
    data["status"] = "final"
    with pytest.raises(ConfigError):
        load_protocol_data(data)


def copy_configs(tmp_path: Path) -> Path:
    target = tmp_path / "configs"
    shutil.copytree(CONFIG_DIR, target)
    return target


def test_store_errors(tmp_path: Path) -> None:
    cfg = copy_configs(tmp_path)
    store = ConfigStore(cfg)
    with pytest.raises(ConfigError) as err:
        store.profile("microscope")
    assert "unconstrained, viewer" in err.value.message
    with pytest.raises(ConfigError):
        store.protocol("t9")

    (cfg / "profiles" / "viewer.yaml").rename(cfg / "profiles" / "renamed.yaml")
    with pytest.raises(ConfigError, match=r"must be named viewer\.yaml"):
        ConfigStore(cfg)


def test_store_needs_profiles_and_protocols(tmp_path: Path) -> None:
    cfg = copy_configs(tmp_path)
    for f in (cfg / "protocols").glob("*.yaml"):
        f.unlink()
    with pytest.raises(ConfigError, match="No protocols"):
        ConfigStore(cfg)
    for f in (cfg / "profiles").glob("*.yaml"):
        f.unlink()
    with pytest.raises(ConfigError, match="No profiles"):
        ConfigStore(cfg)


def test_store_reports_unreadable_yaml(tmp_path: Path) -> None:
    cfg = copy_configs(tmp_path)
    (cfg / "profiles" / "viewer.yaml").write_text("id: [unclosed\n")
    with pytest.raises(ConfigError, match=r"Cannot read viewer\.yaml"):
        ConfigStore(cfg)
