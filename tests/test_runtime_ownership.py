"""Builders return ready transports; contexts own their release."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rpp_py.context import ComponentContext
from rpp_py.context_builder import ComponentRuntimeBuilder
from rpp_py.parameter_handler import Parameters
from rpp_py.runtime import ComponentRuntime


@pytest.fixture
def build(monkeypatch):
    events = []

    class Runtime:
        def __init__(self, name):
            self.name = name
            self.ready = False
            self.closed = False
            self.error = None

        def prepare(self):
            events.append(("prepare", self.name))
            if self.error:
                raise self.error
            self.ready = True

        def close(self):
            if not self.closed:
                self.closed = True
                events.append(("close", self.name))

    records = {name: SimpleNamespace(folder=name, name=name, id=name,
               plugin_name="lib::Cpp", subcomponents={}, subcomponent_spec={})
               for name in ("first", "second")}
    registry = SimpleNamespace(begin_build=Mock(), create=Mock())
    registry.clone = lambda: registry
    manager = SimpleNamespace(lm=SimpleNamespace(rpp_home="/test"),
        get_plugin_info_from_lib=lambda _: {
            "SourceLanguage": "cpp", "PluginName": "lib::Cpp",
            "PluginType": "test::Plugin",
        })
    builder = ComponentRuntimeBuilder(data_manager=manager,
        external_component_process_registry=registry, logger=Mock())
    builder.resolve_component = lambda path, *_: records[path]
    builder._load_parameters = lambda *_: Parameters()
    monkeypatch.setattr("rpp_py.context_builder.PluginAdapter.create_client", Mock(return_value=object()))
    runtimes = [Runtime("first"), Runtime("second")]
    registry.create.side_effect = runtimes
    return SimpleNamespace(builder=builder, runtimes=runtimes, events=events)


def test_builder_returns_prepared_runtime_and_transfers_ownership(build):
    runtime = build.builder.build_component_from_path("first")
    assert build.runtimes[0].ready
    assert not hasattr(runtime, "start")
    assert not hasattr(runtime, "stop")
    assert build.builder._prepared_components == {}
    runtime.close()
    runtime.close()
    assert build.events == [("prepare", "first"), ("close", "first")]


def test_failed_construction_releases_all_prepared_resources(build):
    failure = ValueError("parameter resolution failed")

    def parameters(record, _):
        if record.folder == "second":
            raise failure
        return Parameters()

    build.builder._load_parameters = parameters
    roots = [build.builder.ComponentRoot(name) for name in ("first", "second")]
    with pytest.raises(ValueError) as caught:
        build.builder.build_from_component_roots(roots)
    assert caught.value is failure
    assert all(runtime.closed for runtime in build.runtimes)
    assert build.builder._prepared_components == {}


def test_failed_preparation_releases_reserved_resources(build):
    build.runtimes[1].error = RuntimeError("host unavailable")
    roots = [build.builder.ComponentRoot(name) for name in ("first", "second")]
    with pytest.raises(RuntimeError, match="host unavailable"):
        build.builder.build_from_component_roots(roots)
    assert all(runtime.closed for runtime in build.runtimes)


def test_builder_reuse_does_not_close_an_existing_tree(build):
    first = build.builder.build_component_from_path("first")
    second = build.builder.build_component_from_path("second")
    second.close()
    assert not build.runtimes[0].closed
    first.close()
    assert all(runtime.closed for runtime in build.runtimes)


def test_runtime_destruction_releases_its_owned_process(build):
    runtime = build.builder.build_component_from_path("first")
    del runtime
    assert build.runtimes[0].closed


def test_runtime_close_continues_after_child_runtime_failure():
    from rpp_py.lifecycle import LifecycleError
    failed = Mock(close=Mock(side_effect=ValueError("release rejected")))
    sibling = Mock()
    root = ComponentContext._create(subcomponents={"children": [
        ComponentContext._create(runtime=sibling), ComponentContext._create(runtime=failed)]}, logger=Mock())
    with pytest.raises(LifecycleError, match="release rejected"):
        ComponentRuntime(root).close()
    sibling.close.assert_called_once()


def test_native_tree_lifecycle_needs_no_event_loop_or_executor(monkeypatch):
    from rpp_py.plugin import Plugin
    from rpp_py.runtime import ComponentRuntime
    forbidden = Mock(side_effect=AssertionError("Native trees need no async machinery"))
    monkeypatch.setattr("asyncio.new_event_loop", forbidden)
    monkeypatch.setattr("rpp_py.component_call_executor.ComponentCallExecutor", forbidden)
    context = ComponentContext(instance=Plugin(), subcomponents={
        "child": ComponentContext(instance=Plugin())})
    runtime = ComponentRuntime(context)
    runtime.configure()
    context.activate_subcomponents()
    runtime.activate()
    runtime.deactivate()
    context.deactivate_subcomponents()
    runtime.cleanup()
    runtime.close()
    forbidden.assert_not_called()
