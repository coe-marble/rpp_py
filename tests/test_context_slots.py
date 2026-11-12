"""Runtime-owned slot administration and context-owned child access."""

import pytest

from rpp_py.component_slots import SlotKind
from rpp_py.context import ComponentContext
from rpp_py.lifecycle import CallbackResult, LifecycleError, LifecycleState
from rpp_py.plugin import Plugin
from rpp_py.runtime import ComponentRuntime


class Controller(Plugin):
    def __init__(self, axes=()):
        self.axes = set(axes)


def child(name, axes=()):
    return ComponentContext._create(instance=Controller(axes), component_name=name,
                                    plugin_type="controllers::Controller")


def root(declaration, children, *, dynamic=False, factory=None, validator=None):
    context = ComponentContext._create(
        subcomponents={"controllers": children}, spec={"controllers": declaration},
        allow_dynamic_components=dynamic, component_factory=factory,
        registration_validators={"controllers": validator})
    return ComponentRuntime(context), context


def no_axis_overlap(candidate, registered):
    claimed = set().union(*(entry.get_instance().axes for entry in registered))
    overlap = claimed & candidate.get_instance().axes
    return (CallbackResult.failure(f"Control axes already registered: {sorted(overlap)}")
            if overlap else CallbackResult())


def test_static_slot_lifecycle_uses_runtime_and_context_views():
    runtime, context = root("Option[controllers::Controller]", [child("first"), child("second")])
    runtime.configure().require_success()
    assert context.activate_subcomponents().success
    assert runtime.activate().success

    assert context.get_subcomponent_instance("controllers") is not None
    assert [status.state for status in runtime.get_subcomponent_statuses("controllers")] == [
        LifecycleState.ACTIVE, LifecycleState.INACTIVE]
    assert not runtime.load_component("controllers", "third", "/third").success
    assert not runtime.unload_component("controllers", "first").success
    assert not runtime.switch_component("controllers", "first", "second").success


@pytest.mark.parametrize("declaration", ["controllers::Controller", "Option[controllers::Controller]"])
def test_dynamic_slot_loads_and_switches_through_runtime(declaration):
    runtime, context = root(
        declaration, [child("first")], dynamic=True, factory=lambda _: child("second"))
    runtime.configure().require_success()
    assert context.activate_subcomponents().success
    assert runtime.activate().success
    assert runtime.load_component("controllers", "second", "/second").success
    assert runtime.configure_component("controllers", "second").success
    assert runtime.switch_component("controllers", "first", "second").success

    selected = context.get_subcomponent_context("controllers")
    assert selected.get_instance() is context.get_subcomponent_instance("controllers")
    assert selected.is_active()
    assert runtime.unload_component("controllers", "first").success


def test_list_slot_requires_a_name_for_single_access_and_enumerates_lazily():
    runtime, context = root("List[controllers::Controller]", [child("surge"), child("sway")])
    runtime.configure().require_success()
    assert context.activate_subcomponents().success

    with pytest.raises(LifecycleError, match="requires a component name"):
        context.get_subcomponent_instance("controllers")
    assert len(list(context.enumerate_subcomponent_instances("controllers"))) == 2
    assert len(list(context.enumerate_subcomponent_contexts("controllers"))) == 2


def test_runtime_registration_validator_protects_dynamic_slot():
    runtime, _ = root(
        "List[controllers::Controller]", [child("surge", ["surge"]), child("sway", ["sway"])],
        dynamic=True, factory=lambda _: child("new", ["surge", "yaw"]), validator=no_axis_overlap)
    runtime.configure().require_success()
    result = runtime.load_component("controllers", "surge_yaw", "/surge_yaw")
    assert not result.success
    assert "surge" in result.message


def test_context_reports_slot_kind_without_exposing_its_manager():
    _, context = root("List[controllers::Controller]", [child("surge")])
    assert context.parse_component_slot_type("List[controllers::Controller]").kind is SlotKind.LIST
    assert not hasattr(context, "load_component")


def test_dynamic_policy_is_evaluated_per_slot_including_empty_slots():
    calls = []

    def policy(name, slot):
        calls.append((name, slot.kind))
        return name == "enabled"

    context = ComponentContext._create(
        spec={"enabled": "List[controllers::Controller]", "fixed": "Option[controllers::Controller]"},
        allow_dynamic_components=policy, component_factory=lambda _: child("loaded"))
    runtime = ComponentRuntime(context)
    assert calls == [("enabled", SlotKind.LIST), ("fixed", SlotKind.OPTION)]
    assert runtime.load_component("enabled", "loaded", "/component").success
    assert not runtime.load_component("fixed", "loaded", "/component").success
    assert len(calls) == 2
    runtime.close().require_success()


def test_named_validators_apply_to_initial_names_and_dynamic_registration_names():
    calls = []

    def validator(candidate, registered):
        calls.append((candidate.get_context().get_instance_id(),
                      tuple(item.get_instance().axes for item in registered)))
        return CallbackResult.failure("name rejected") if registered else CallbackResult()

    context = ComponentContext._create(
        spec={"controllers": "List[controllers::Controller]"},
        subcomponents={"controllers": [child("first", ["surge"])]},
        allow_dynamic_components=True, component_factory=lambda _: child("workspace_name"),
        named_registration_validators={"controllers::Controller": {
            "first": validator, "blocked": validator}})
    runtime = ComponentRuntime(context)
    assert len(calls) == 1
    assert not runtime.load_component("controllers", "blocked", "/component").success
    assert len(calls) == 2
    assert calls[0][0]
    assert calls[1][1] == ({"surge"},)
    runtime.close().require_success()
