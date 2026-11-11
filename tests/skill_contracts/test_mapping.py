"""Semantic checks must not be inferred from names or hidden-only evidence."""

import copy

import pytest

from memos.skill_contracts.mapping import assess_checks, catalog, contract


def passed(profile, visibility):
    return [{"name": name, "status": "passed"} for name in profile[visibility]]


def test_p5_full_inventory_and_host_semantics_are_separate():
    profile = contract("p5-update-repair-r1")
    visible = assess_checks(profile, passed(profile, "public"), visibility="public")
    hidden = assess_checks(profile, passed(profile, "hidden"), visibility="hidden")
    assert visible["inventoryComplete"]
    assert not visible["semanticsComplete"]
    assert "preserve_missing_content" in visible["missingObligations"]
    assert hidden["semanticsComplete"]


@pytest.mark.parametrize("case", ["transfer-batch-update", "transfer-version-update"])
def test_transfer_visible_or_hidden_cannot_supply_original_contract(case):
    profile = contract(case)
    for visibility in ("public", "hidden"):
        result = assess_checks(profile, passed(profile, visibility), visibility=visibility)
        assert result["inventoryComplete"]
        assert not result["semanticsComplete"]
        assert "other_action_semantics" in result["missingObligations"]
        assert "independent_caller_contract" in result["missingObligations"]


@pytest.mark.parametrize("mutation", ["missing", "failed", "duplicate", "unknown", "invalid"])
def test_bad_checks_do_not_acquire_acceptance(mutation):
    profile = contract("p5-update-repair-r1")
    rows = passed(profile, "hidden")
    if mutation == "missing":
        rows.pop()
    elif mutation == "failed":
        rows[-1]["status"] = "failed"
    elif mutation == "duplicate":
        rows.append(rows[0])
    elif mutation == "unknown":
        rows[0]["name"] = "update_contract"
    else:
        rows[0]["status"] = True
    result = assess_checks(profile, rows, visibility="hidden")
    assert not result["semanticsComplete"]
    assert not result["allPassed"]


def test_catalog_is_pinned_and_caller_cannot_redefine_mapping():
    profile = contract("transfer-batch-update")
    altered = copy.deepcopy(profile)
    altered["public"]["update_contract"] = list(catalog()["obligations"])
    with pytest.raises(ValueError, match="contract_mapping_changed"):
        assess_checks(altered, passed(altered, "public"), visibility="public")
    with pytest.raises(ValueError, match="contract_unknown_task"):
        contract("unregistered")


def test_task_change_rejects_old_mapping(monkeypatch):
    from memos.skill_contracts import mapping

    monkeypatch.setattr(mapping, "task_identity", lambda case: {"taskDigest": "changed"})
    with pytest.raises(ValueError, match="contract_implementation_changed"):
        contract("transfer-batch-update")
