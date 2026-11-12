"""Context access and runtime-owned lifecycle behavior."""

from dataclasses import dataclass, field
from unittest.mock import Mock

import pytest

from rpp_py.context import ComponentContext
from rpp_py.lifecycle import CallbackResult, DeactivationResult, LifecycleError
from rpp_py.plugin import Plugin
from rpp_py.runtime import ComponentRuntime


class RecordingPlugin(Plugin):
    def __init__(self, name, events, *, configure_error=None, cleanup_error=None,
                 activation_error=None, deactivation_error=None):
        self.name = name
        self.events = events
        self.configure_error = configure_error
        self.cleanup_error = cleanup_error
        self.activation_error = activation_error
        self.deactivation_error = deactivation_error

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


def tree(events):
    children = [ComponentContext._create(instance=RecordingPlugin(name, events), component_name=name)
                for name in ("first", "second")]
    context = ComponentContext._create(
        instance=RecordingPlugin("parent", events),
        subcomponents={"children": children}, logger=Mock())
    return ComponentRuntime(context), context


def test_runtime_configures_children_before_parent():
    events = []
    runtime, _ = tree(events)

    assert runtime.configure().success
    assert events == [("configure", "first"), ("configure", "second"),
                      ("configure", "parent")]


def test_runtime_processes_child_self_deactivation_without_stopping_siblings():
    events = []
    runtime, context = tree(events)
    runtime.configure().require_success()
    context.activate_subcomponents().require_success()
    first = context.get_subcomponent_context("children", "first")
    second = context.get_subcomponent_context("children", "second")
    events.clear()
    first.get_instance().context.request_deactivation().require_success()
    assert first.is_active()
    runtime.process_deactivations().require_success()
    assert not first.is_active()
    assert second.is_active()
    assert events == [("deactivate", "first")]
    runtime.close().require_success()
    assert events == [("deactivate", "first"), ("deactivate", "second"),
                      ("cleanup", "parent"), ("cleanup", "second"), ("cleanup", "first")]


@pytest.mark.parametrize("managed", [False, True])
def test_close_stops_active_grandchildren_and_retains_the_tree_after_failure(managed):
    from rpp_py.component_manager import ComponentManager

    events = []
    plugin = RecordingPlugin("grandchild", events, deactivation_error=ValueError("stop rejected"))
    grandchild = ComponentContext._create(instance=plugin, component_name="grandchild")
    parent = ComponentContext._create(instance=RecordingPlugin("parent", events),
                                      subcomponents={"children": [grandchild]})
    context = ComponentContext._create(instance=RecordingPlugin("root", events),
                                       subcomponents={"parents": [parent]})
    manager = ComponentManager() if managed else None
    if managed:
        manager.adopt("root", context, initial=True).require_success()
    runtime = ComponentRuntime(context, manager, "root" if managed else None)
    runtime.configure().require_success()
    parent.activate_subcomponents().require_success()
    assert not parent.is_active()
    assert grandchild.is_active()
    events.clear()

    if managed:
        assert not runtime.close().success
    else:
        with pytest.raises(LifecycleError, match="stop rejected"):
            runtime.close()
    assert events == [("deactivate", "grandchild")]
    assert grandchild._instance is plugin

    plugin.deactivation_error = None
    runtime.close().require_success()
    assert events[:2] == [("deactivate", "grandchild"), ("deactivate", "grandchild")]
    assert context._released
    assert grandchild._released


def test_runtime_does_not_implicitly_activate_or_deactivate_subcomponents():
    events = []
    runtime, context = tree(events)
    runtime.configure().require_success()
    events.clear()

    assert runtime.activate().success
    assert events == [("activate", "parent")]
    assert context.activate_subcomponents().success
    assert events == [("activate", "parent"), ("activate", "first"),
                      ("activate", "second")]

    assert runtime.deactivate().result.success
    assert context.deactivate_subcomponents().success
    assert events[-3:] == [("deactivate", "parent"), ("deactivate", "second"),
                           ("deactivate", "first")]


def test_failed_parent_activation_does_not_activate_subcomponents():
    events = []
    runtime, context = tree(events)
    runtime.configure().require_success()
    runtime.get_instance().activation_error = ValueError("parent rejected")

    with pytest.raises(LifecycleError, match="parent rejected"):
        runtime.activate()
    assert not context.get_subcomponent_context("children", "first").is_active()
    assert not context.get_subcomponent_context("children", "second").is_active()


def test_runtime_exports_only_its_own_handover_state():
    @dataclass
    class Estimate:
        position: list[float] = field(default_factory=list)

    class Stateful(Plugin):
        HANDOVER_STATE_TYPE = Estimate

        def on_configure(self, context):
            return CallbackResult()

        def on_activate(self, state=None):
            self.estimate = state or Estimate()
            return CallbackResult()

        def on_deactivate(self):
            return DeactivationResult(state=self.estimate)

    runtime = ComponentRuntime(ComponentContext(instance=Stateful()))
    runtime.configure().require_success()
    assert runtime.activate(Estimate([1.0])).success
    stopped = runtime.deactivate()
    assert stopped.result.success
    assert stopped.state.position == [1.0]


def test_adapted_context_forwards_lifecycle_through_runtime():
    external = Mock(
        configure=Mock(return_value=CallbackResult()),
        activate=Mock(return_value=CallbackResult()),
        deactivate=Mock(return_value=DeactivationResult()),
        cleanup=Mock(return_value=CallbackResult()))
    plugin = Plugin()
    plugin.on_configure = Mock()
    runtime = ComponentRuntime(ComponentContext._create(instance=plugin, runtime=external))

    assert runtime.configure().success
    assert runtime.activate().success
    assert runtime.deactivate().result.success
    assert runtime.cleanup().success
    external.configure.assert_called_once_with()
    external.activate.assert_called_once_with(None)
    external.deactivate.assert_called_once_with()
    external.cleanup.assert_called_once_with()
    plugin.on_configure.assert_not_called()


def test_cleanup_runs_parent_before_children_and_reports_errors():
    events = []
    logger = Mock()
    context = ComponentContext._create(
        instance=RecordingPlugin("parent", events, cleanup_error=ValueError("parent failed")),
        subcomponents={"children": [
            ComponentContext._create(instance=RecordingPlugin("first", events), component_name="first"),
            ComponentContext._create(instance=RecordingPlugin(
                "second", events, cleanup_error=ValueError("child failed")), component_name="second"),
        ]}, logger=logger)

    with pytest.raises(LifecycleError, match="parent failed"):
        ComponentRuntime(context).cleanup()
    assert events == [("cleanup", "parent"), ("cleanup", "second"), ("cleanup", "first")]
    assert "child failed" in logger.error.call_args.args[0]


def test_context_exposes_instances_and_views_but_not_lifecycle_or_slot_administration():
    single = ComponentContext._create(instance=Plugin(), component_name="single")
    first = ComponentContext._create(instance=Plugin(), component_name="first")
    second = ComponentContext._create(instance=Plugin(), component_name="second")
    context = ComponentContext(subcomponents={"single": single, "items": [first, second]},
        spec={"single": "controllers::Controller", "items": "List[controllers::Controller]"})

    assert context.get_subcomponent_instance("single") is single._instance
    assert list(context.enumerate_subcomponent_instances("items")) == [first._instance, second._instance]
    assert [view.get_instance() for view in context.enumerate_subcomponent_contexts("items")] == [
        first._instance, second._instance]
    assert not hasattr(context, "configure")
    assert not hasattr(context, "load_component")


def test_empty_runtime_can_complete_a_lifecycle():
    runtime = ComponentRuntime(ComponentContext())
    assert runtime.configure().success
    assert runtime.activate().success
    assert runtime.deactivate().result.success
    assert runtime.cleanup().success
