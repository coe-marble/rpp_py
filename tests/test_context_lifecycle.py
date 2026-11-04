"""Component lifecycle tests without plugins, servers, or generated schemas."""

from unittest.mock import Mock

import pytest

from rpp_py.context import ComponentContext
from rpp_py.plugin import Plugin
from rpp_py.lifecycle import CallbackResult, LifecycleError


class RecordingPlugin(Plugin):
    def __init__(self, name, events, configure_error=None, cleanup_error=None):
        self.name = name
        self.events = events
        self.configure_error = configure_error
        self.cleanup_error = cleanup_error
        self.activation_error = None
        self.deactivation_error = None

    def on_configure(self, context):
        self.events.append(("configure", self.name))
        self.context = context
        if self.configure_error:
            raise self.configure_error
        return CallbackResult()

    def on_cleanup(self):
        self.events.append(("cleanup", self.name))
        if self.cleanup_error:
            raise self.cleanup_error
        return CallbackResult()

    def on_activate(self):
        self.events.append(("activate", self.name))
        if self.activation_error:
            raise self.activation_error
        return CallbackResult()

    def on_deactivate(self):
        self.events.append(("deactivate", self.name))
        if self.deactivation_error:
            raise self.deactivation_error
        return CallbackResult()


def activation_tree(events):
    plugins = {name: RecordingPlugin(name, events)
               for name in ("parent", "first", "second", "unreached")}
    children = [ComponentContext(instance=plugins[name])
                for name in ("first", "second", "unreached")]
    context = ComponentContext(instance=plugins["parent"],
                               subcomponents={"children": children}, logger=Mock())
    return context, plugins


def test_activation_children_first_and_deactivation_parent_first():
    events = []
    context, _ = activation_tree(events)
    assert context.activate().success
    assert context.deactivate().result.success
    assert events == [("activate", "first"), ("activate", "second"),
                      ("activate", "unreached"), ("activate", "parent"),
                      ("deactivate", "parent"), ("deactivate", "unreached"),
                      ("deactivate", "second"), ("deactivate", "first")]


@pytest.mark.parametrize("failed", ["second", "parent"])
def test_failed_activation_rolls_back_only_completed_children(failed):
    events = []
    context, plugins = activation_tree(events)
    plugins[failed].activation_error = ValueError("activation rejected")

    with pytest.raises(LifecycleError, match="activation rejected"):
        context.activate()

    activated = ["first"] if failed == "second" else ["first", "second", "unreached"]
    assert events == ([("activate", name) for name in activated + [failed]] +
                      [("deactivate", name) for name in reversed(activated)])


def test_activation_rollback_continues_and_preserves_original_error():
    events = []
    context, plugins = activation_tree(events)
    plugins["parent"].activation_error = ValueError("activation rejected")
    plugins["second"].deactivation_error = ValueError("rollback rejected")

    with pytest.raises(LifecycleError, match="activation rejected"):
        context.activate()

    assert events[-3:] == [("deactivate", "unreached"), ("deactivate", "second"),
                           ("deactivate", "first")]
    assert "rollback rejected" in context.get_logger().error.call_args.args[0]


def test_nested_activation_failure_rolls_back_each_completed_subtree_once():
    events = []
    first = ComponentContext(instance=RecordingPlugin("first", events), subcomponents={
        "nested": ComponentContext(instance=RecordingPlugin("first nested", events))})
    failed = RecordingPlugin("failed", events)
    failed.activation_error = ValueError("activation rejected")
    second = ComponentContext(instance=failed, subcomponents={
        "nested": ComponentContext(instance=RecordingPlugin("second nested", events))})
    parent = ComponentContext(instance=RecordingPlugin("parent", events),
                              subcomponents={"children": [first, second]})

    with pytest.raises(LifecycleError, match="activation rejected"):
        parent.activate()

    assert events == [("activate", "first nested"), ("activate", "first"),
                      ("activate", "second nested"), ("activate", "failed"),
                      ("deactivate", "second nested"), ("deactivate", "first"),
                      ("deactivate", "first nested")]


def test_deactivation_continues_and_preserves_first_error():
    events = []
    context, plugins = activation_tree(events)
    context.activate()
    events.clear()
    plugins["parent"].deactivation_error = ValueError("parent rejected")
    plugins["second"].deactivation_error = ValueError("child rejected")

    with pytest.raises(LifecycleError, match="parent rejected"):
        context.deactivate()

    assert events == [("deactivate", "parent"), ("deactivate", "unreached"),
                      ("deactivate", "second"), ("deactivate", "first")]
    assert "child rejected" in context.get_logger().error.call_args.args[0]


def test_context_handover_is_root_only_and_discarded_if_a_child_cannot_stop():
    from dataclasses import dataclass, field
    from rpp_py.lifecycle import DeactivationResult

    @dataclass
    class Estimate:
        position: list[float] = field(default_factory=list)

    class Stateful(Plugin):
        HANDOVER_STATE_TYPE = Estimate

        def on_activate(self, state=None):
            self.estimate = state
            return CallbackResult()

        def on_deactivate(self):
            return DeactivationResult(state=self.estimate)

    child_plugin = RecordingPlugin("child", [])
    plugin = Stateful()
    context = ComponentContext(instance=plugin, subcomponents={
        "child": ComponentContext(instance=child_plugin)})
    state = Estimate([1.0])
    assert context.activate(state).success
    state.position.append(2.0)
    snapshot = context.deactivate().state
    plugin.estimate.position.append(3.0)
    assert snapshot.position == [1.0]

    assert context.activate(snapshot).success
    child_plugin.deactivation_error = ValueError("child is still active")
    with pytest.raises(LifecycleError, match="child is still active"):
        context.deactivate()


def test_incompatible_root_state_does_not_activate_children():
    events = []
    context, _ = activation_tree(events)
    with pytest.raises(TypeError, match="no state"):
        context.activate({"unexpected": "state"})
    assert events == []


def test_adapted_activation_is_rejected_until_runtime_control_exists():
    context = ComponentContext(instance=Plugin(), runtime=object())
    with pytest.raises(LifecycleError, match="Adapted.*not supported"):
        context.activate()
    with pytest.raises(LifecycleError, match="Adapted.*not supported"):
        context.deactivate()


def test_empty_context_can_activate_and_deactivate():
    context = ComponentContext()
    assert context.activate().success
    assert context.deactivate().state is None


def test_configure_children_first_and_cleanup_parent_first():
    events = []
    first = ComponentContext(instance=RecordingPlugin("first", events))
    second = ComponentContext(instance=RecordingPlugin("second", events))
    plugin = RecordingPlugin("parent", events)
    context = ComponentContext(instance=plugin, subcomponents={
        "single": first, "multiple": [second]})

    assert context.configure().success
    assert plugin.context is context
    assert context.cleanup().success
    assert events == [("configure", "first"), ("configure", "second"),
                      ("configure", "parent"), ("cleanup", "parent"),
                      ("cleanup", "second"), ("cleanup", "first")]


def test_failed_child_configuration_stops_before_siblings_and_parent():
    events = []
    failed = ComponentContext(instance=RecordingPlugin(
        "failed", events, configure_error=ValueError("configuration rejected")))
    context = ComponentContext(instance=RecordingPlugin("parent", events),
                               subcomponents={"children": [failed,
                                   ComponentContext(instance=RecordingPlugin("unreached", events))]})
    with pytest.raises(LifecycleError, match="on_configure.*configuration rejected"):
        context.configure()
    assert events == [("configure", "failed")]


def test_cleanup_continues_after_parent_and_child_errors():
    events = []
    logger = Mock()
    context = ComponentContext(instance=RecordingPlugin(
        "parent", events, cleanup_error=ValueError("parent cleanup failed")),
        subcomponents={"children": [
            ComponentContext(instance=RecordingPlugin("first", events)),
            ComponentContext(instance=RecordingPlugin(
                "second", events, cleanup_error=ValueError("child cleanup failed")))]},
        logger=logger)

    with pytest.raises(LifecycleError, match="parent cleanup failed"):
        context.cleanup()
    assert events == [("cleanup", "parent"), ("cleanup", "second"), ("cleanup", "first")]
    assert "child cleanup failed" in logger.error.call_args.args[0]


@pytest.mark.parametrize("result", [None, CallbackResult.failure("cleanup rejected")])
def test_cleanup_requires_successful_callback_result(result):
    plugin = Plugin()
    plugin.on_cleanup = Mock(return_value=result)
    with pytest.raises(LifecycleError, match="on_cleanup"):
        ComponentContext(instance=plugin).cleanup()


def test_empty_context_can_configure_and_cleanup():
    context = ComponentContext()
    assert context.configure().success
    assert context.cleanup().success
