"""Public managed lifecycle, ownership, and call-gate contracts."""

import asyncio
from dataclasses import dataclass, field
from threading import Condition, Thread
from unittest.mock import Mock

import pytest

from rpp_py.lifecycle import CallbackResult, DeactivationResult, LifecycleError, LifecycleState
from rpp_py.context import ComponentContext
from rpp_py.plugin import Plugin
from rpp_py.plugin_manager import PluginManager


@dataclass
class Estimate:
    position: list[float] = field(default_factory=list)


class Localizer(Plugin):
    HANDOVER_STATE_TYPE = Estimate

    def __init__(self, events):
        self.events = events
        self.estimate = Estimate([1.0])
        self.reject = None

    def on_configure(self, context):
        self.events.append("configure")
        return CallbackResult.failure("configuration rejected") if self.reject == "configure" else CallbackResult()

    def on_activate(self, state=None):
        self.events.append("activate")
        if state is not None:
            self.estimate = state
        return CallbackResult.failure("activation rejected") if self.reject == "activate" else CallbackResult()

    def on_deactivate(self):
        self.events.append("deactivate")
        return DeactivationResult(CallbackResult.failure("still active") if self.reject == "deactivate"
                                  else CallbackResult(), self.estimate)

    def on_cleanup(self):
        self.events.append("cleanup")
        return CallbackResult()


@pytest.fixture
def setup_manager():
    plugins = []
    events = []

    def build(path):
        plugin = Localizer(events)
        plugins.append(plugin)
        return ComponentContext(instance=plugin)

    builder = Mock(build_component_from_path=Mock(side_effect=build))
    manager = PluginManager(builder=builder)
    yield manager, plugins, events, builder
    manager.close()


def ready(manager, name="localizer"):
    assert manager.load(name, component="/workspace/localizer").success
    assert manager.configure(name).success
    assert manager.activate(name).success


def test_normal_flow_duplicate_activation_and_generation(setup_manager):
    manager, _, events, builder = setup_manager
    ready(manager)
    generation = manager.get_info("localizer").generation
    assert manager.activate("localizer").success
    assert events == ["configure", "activate"]
    assert manager.with_active("localizer", lambda plugin: plugin.estimate.position) == [1.0]
    assert manager.deactivate("localizer").state.position == [1.0]
    assert manager.deactivate("localizer").state is None
    assert manager.cleanup("localizer").success
    assert manager.unload("localizer").success
    assert manager.get_state("localizer") is LifecycleState.UNLOADED
    ready(manager)
    assert manager.get_info("localizer").generation > generation
    with pytest.raises(LifecycleError, match="generation"):
        manager.with_active("localizer", lambda plugin: None, generation=generation)
    assert builder.build_component_from_path.call_count == 2


def test_invalid_transition_keeps_actual_state_and_inactive_calls_are_rejected(setup_manager):
    manager, _, events, _ = setup_manager
    assert manager.load("localizer", component="/workspace/localizer").success
    assert not manager.activate("localizer").success
    assert manager.get_state("localizer") is LifecycleState.UNCONFIGURED
    assert events == []
    with pytest.raises(LifecycleError, match="active"):
        manager.with_active("localizer", lambda plugin: None)


def test_failed_callback_is_visible_and_cannot_affect_another_entry(setup_manager):
    manager, plugins, _, _ = setup_manager
    ready(manager, "other")
    assert manager.load("failed", component="/workspace/failed").success
    plugins[-1].reject = "configure"
    result = manager.configure("failed")
    assert not result.success and "configuration rejected" in result.message
    assert manager.get_state("failed") is LifecycleState.ERROR
    assert manager.get_state("other") is LifecycleState.ACTIVE
    assert not manager.deactivate("failed").result.success
    assert manager.get_state("failed") is LifecycleState.ERROR
    assert manager.unload("failed").success
    assert manager.get_state("other") is LifecycleState.ACTIVE


def test_deactivate_closes_gate_drains_existing_calls_and_keeps_queries_responsive(setup_manager, monkeypatch):
    manager, _, events, _ = setup_manager
    ready(manager)
    changed = Condition()
    state = {"entered": False, "release": False, "draining": False, "done": False}
    errors = []
    original_drain = manager._drain

    def drain(entry, timeout):
        with changed:
            state["draining"] = True
            changed.notify_all()
        return original_drain(entry, timeout)

    monkeypatch.setattr(manager, "_drain", drain)

    def call(plugin):
        with changed:
            state["entered"] = True
            changed.notify_all()
            assert changed.wait_for(lambda: state["release"], timeout=0.5)

    def deactivate():
        result = manager.deactivate("localizer", timeout=0.5)
        with changed:
            state["done"] = True
            if not result.result.success:
                errors.append(result.result.message)
            changed.notify_all()

    caller = Thread(target=lambda: manager.with_active("localizer", call))
    worker = Thread(target=deactivate)
    caller.start()
    try:
        with changed:
            assert changed.wait_for(lambda: state["entered"], timeout=0.5)
        worker.start()
        with changed:
            assert changed.wait_for(lambda: state["draining"], timeout=0.5)
            assert not state["done"]
        assert manager.get_info("localizer").transition == "deactivate"
        assert "deactivate" not in events
        with pytest.raises(LifecycleError):
            manager.with_active("localizer", lambda plugin: None)
    finally:
        with changed:
            state["release"] = True
            changed.notify_all()
        caller.join(0.5)
        if worker.ident is not None:
            worker.join(0.5)
    assert state["done"] and not errors


def test_drain_timeout_retains_instance_and_allows_safe_retry(setup_manager):
    manager, plugins, events, _ = setup_manager
    ready(manager)
    changed = Condition()
    state = {"entered": False, "release": False}

    def call(plugin):
        with changed:
            state["entered"] = True
            changed.notify_all()
            assert changed.wait_for(lambda: state["release"], timeout=0.5)

    thread = Thread(target=lambda: manager.with_active("localizer", call))
    thread.start()
    try:
        with changed:
            assert changed.wait_for(lambda: state["entered"], timeout=0.5)
        assert not manager.unload("localizer", timeout=0).success
        assert manager.get_state("localizer") is LifecycleState.ERROR
        assert "deactivate" not in events and "cleanup" not in events
        assert plugins[0].estimate.position == [1.0]
    finally:
        with changed:
            state["release"] = True
            changed.notify_all()
        thread.join(0.5)
    assert manager.unload("localizer").success


def test_switch_copies_handover_and_failed_target_keeps_source_inactive(setup_manager):
    manager, plugins, _, _ = setup_manager
    ready(manager, "ekf")
    assert manager.load("particle", component="/workspace/particle").success
    assert manager.switch_plugins("ekf", "particle", transfer_state=True).success
    plugins[0].estimate.position.append(99.0)
    assert manager.with_active("particle", lambda plugin: plugin.estimate.position) == [1.0]
    plugins[0].reject = "activate"
    assert not manager.switch_plugins("particle", "ekf", transfer_state=True).success
    assert manager.get_state("particle") is LifecycleState.INACTIVE
    assert manager.get_state("ekf") is LifecycleState.ERROR


def test_reload_uses_new_context_and_preserves_state_only_when_requested(setup_manager):
    manager, plugins, _, _ = setup_manager
    ready(manager)
    plugins[0].estimate.position = [7.0]
    first_generation = manager.get_info("localizer").generation
    assert manager.reload("localizer", transfer_state=True).success
    assert len(plugins) == 2
    assert manager.get_info("localizer").generation > first_generation
    assert manager.with_active("localizer", lambda plugin: plugin.estimate.position) == [7.0]
    assert manager.reload("localizer").success
    assert manager.with_active("localizer", lambda plugin: plugin.estimate.position) == [1.0]


def test_management_from_inside_a_guarded_call_is_rejected_without_deadlocking(setup_manager):
    manager, _, _, _ = setup_manager
    ready(manager)
    result = manager.with_active("localizer", lambda _: manager.deactivate("localizer"))
    assert not result.result.success
    assert manager.get_state("localizer") is LifecycleState.ACTIVE


def test_native_sync_creates_no_loop(setup_manager, monkeypatch):
    manager, _, _, _ = setup_manager
    forbidden = Mock(side_effect=AssertionError("Native synchronous use creates no loop"))
    with monkeypatch.context() as patch:
        patch.setattr(asyncio, "new_event_loop", forbidden)
        ready(manager)
        assert manager.unload("localizer").success
    forbidden.assert_not_called()



def test_async_guard_holds_access_until_the_awaited_callback_finishes(setup_manager, monkeypatch):
    manager, _, _, _ = setup_manager
    ready(manager)

    async def exercise():
        entered, release, draining = asyncio.Event(), asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        original_drain = manager._drain

        def drain(entry, timeout):
            loop.call_soon_threadsafe(draining.set)
            return original_drain(entry, timeout)

        monkeypatch.setattr(manager, "_drain", drain)

        async def call(plugin):
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=0.5)
            return plugin.estimate.position

        async def use_component():
            with manager.active("localizer") as component:
                return await call(component)

        task = asyncio.create_task(use_component())
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        stopped = asyncio.get_running_loop().run_in_executor(
            None, lambda: manager.deactivate("localizer", timeout=0.5))
        await asyncio.wait_for(draining.wait(), timeout=0.5)
        assert not stopped.done()
        release.set()
        assert await task == [1.0]
        assert (await stopped).result.success

    asyncio.run(exercise())


def test_close_continues_other_entries_and_reports_an_unstoppable_plugin(setup_manager):
    manager, plugins, _, _ = setup_manager
    ready(manager, "first")
    ready(manager, "second")
    plugins[1].reject = "deactivate"
    assert not manager.close().success
    assert manager.get_state("first") is LifecycleState.UNLOADED
    assert manager.get_state("second") is LifecycleState.ERROR
    plugins[1].reject = None
    assert manager.close().success
    assert not manager.load("new", component="/workspace/new").success
