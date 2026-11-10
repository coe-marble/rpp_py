"""Slot construction cardinality is independent of runtime activation policy."""

import pytest

from rpp_py.component_slots import (
    ActivationPolicy, SlotKind, parse_component_slot_type,
)


def test_single_and_option_share_exclusive_policy_but_accept_different_inputs():
    single = parse_component_slot_type("controllers::Controller")
    option = parse_component_slot_type("Option[controllers::Controller]")
    multiple = parse_component_slot_type("List[controllers::Controller]")

    assert single.kind is SlotKind.SINGLE
    assert not single.accepts_component_list
    assert option.kind is SlotKind.OPTION
    assert option.accepts_component_list
    assert single.activation_policy is option.activation_policy is ActivationPolicy.EXCLUSIVE
    assert multiple.accepts_component_list
    assert multiple.activation_policy is ActivationPolicy.MULTIPLE
    assert single.plugin_type == option.plugin_type == multiple.plugin_type == "controllers::Controller"


def test_slot_declaration_trims_surrounding_whitespace():
    slot = parse_component_slot_type("  Option[ controllers::Controller ]  ")
    assert slot.plugin_type == "controllers::Controller"
    assert slot.kind is SlotKind.OPTION


@pytest.mark.parametrize("declaration", [
    "", "List[]", "Option[ ]", "Option[controllers::Controller",
    "List[Option[controllers::Controller]]", "Options[controllers::Controller]",
])
def test_invalid_slot_declarations_are_rejected(declaration):
    with pytest.raises(ValueError, match="Invalid component slot type"):
        parse_component_slot_type(declaration)
