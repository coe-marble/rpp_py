import json
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from rpp_orchestrator.component_storage import ComponentRecord
from rpp_py.context_builder import ComponentRuntimeBuilder
from rpp_py.context import ComponentContext
from rpp_py.component_call_executor import ComponentCallExecutor
from rpp_py.external_component_process import (
    ExternalComponentProcessRegistry,
    ExternalComponentProcess,
    ExternalComponentSpec,
)
from rpp_py.data_manager import DataManager


class FakeDataManager:
    def __init__(self, description):
        self.description = description

    def load_script_description(self, _path):
        return self.description

    def get_default_script_parts_folder_path_from_description(self, _path):
        return "/parts"

    def get_default_script_description_path(self, script_path):
        return f"/workspace/{Path(script_path).stem}.json"

    def get_component_path_in_parts_folder(
            self, parts_folder, plugin_name, component_id):
        return f"{parts_folder}/{plugin_name}/{component_id}"

    def load_component_info(self, component_path) -> ComponentRecord:
        return ComponentRecord(
            id="fake-id",
            name="fake-name",
            plugin_type="fake-plugin-type",
            plugin_name="fake-plugin-name",
            library="fake-library",
            folder=component_path,
            subcomponent_spec={}
        )

    def get_plugin_info_from_lib(self, library_name):
        return {
            "PluginType": "fake-plugin-type",
            "PluginName": "fake-plugin-name",
            "Library": library_name,
            "SourceLanguage": "python"
        }


@pytest.mark.parametrize("dynamic", [False, True])
def test_builder_propagates_dynamic_permission_to_native_subtrees(monkeypatch, dynamic):
    from unittest.mock import Mock
    from rpp_py.plugin import Plugin
    from rpp_py.parameter_handler import Parameters
    from rpp_py.lifecycle import CallbackResult, LifecycleError

    manager = FakeDataManager(None)
    manager.lm = object()
    records = {path: ComponentRecord(id=path, name=path, plugin_type="fake-plugin-type",
        plugin_name="fake-plugin-name", library="fake", folder=path,
        subcomponent_spec={}, subcomponents={}) for path in ("/root", "/child")}
    records["/root"].subcomponent_spec = {"items": "Option[fake-plugin-type]"}
    records["/root"].subcomponents = {"items": [SimpleNamespace(id="child", plugin_name="fake-plugin-name")]}
    manager.load_component_info = lambda path: records[path]
    manager.get_subcomponent_folder_path = lambda *_: "/child"
    monkeypatch.setattr("rpp_py.context_builder.PythonPluginLoader",
        Mock(return_value=Mock(create_instance=lambda _: Plugin())))
    builder = ComponentRuntimeBuilder(data_manager=manager, allow_dynamic_components=dynamic)
    builder._load_parameters = lambda *_: Parameters()
    runtime = builder.build_component_from_path("/root")
    context = runtime.get_context()
    # Dynamic slot administration belongs only to composition roots (scripts
    # and component servers), never to a component's own subcomponent slots.
    assert not runtime.allows_dynamic_components()
    assert not context._slot("items")._dynamic
    assert not context.get_subcomponent_context("items", "/child")._context._dynamic
    runtime.close()

    validator = Mock(return_value=CallbackResult.failure("admission rejected"))
    builder.registration_validators = {"/root": {"items": validator}}
    with pytest.raises(LifecycleError, match="admission rejected"):
        builder.build_component_from_path("/root")
    assert validator.call_count == 1


def test_data_manager_loads_configuration_based_script_description(tmp_path: Path):
    description_path = tmp_path / "simulation.json"
    description_path.write_text(json.dumps({
        "ScriptPath": "/library/simulation.py",
        "Language": "python",
        "Configurations": {
            "Default": {"Components": {"vehicle": []}},
        },
        "ActiveConfiguration": "Default",
        "Spec": {"vehicle": "more_dynamics::VehicleModel3D"},
    }), encoding="utf-8")

    description = DataManager.__new__(DataManager).load_script_description(
        str(description_path)
    )

    assert description.configurations == {
        "Default": {"Components": {"vehicle": []}},
    }
    assert description.active_configuration == "Default"
    assert description.spec == {
        "vehicle": "more_dynamics::VehicleModel3D",
    }


def test_builder_uses_active_script_configuration():
    description = SimpleNamespace(
        configurations={
            "Default": {"Components": {}},
            "Simulation": {
                "Components": {
                    "vehicle": {
                        "PluginName": "more_dynamics::HullVessel",
                        "Id": "vessel-id",
                    },
                },
            },
        },
        active_configuration="Simulation",
        spec={"vehicle": "more_dynamics::VehicleModel3D"},
    )
    builder = ComponentRuntimeBuilder(data_manager=FakeDataManager(description))
    builder._build_component_from_path = lambda path: ComponentContext(instance=path)

    runtime = builder._build_script_from_description_path(
        "/workspace/script.json"
    )

    context = runtime.get_context()
    assert context.get_subcomponent_context("vehicle").get_instance() == "/parts/more_dynamics::HullVessel/vessel-id"


def test_builder_can_select_an_explicit_script_configuration():
    description = SimpleNamespace(
        configurations={
            "Default": {"Components": {}},
            "Alternative": {"Components": {"controllers": []}},
        },
        active_configuration="Default",
        spec={"controllers": "List[rpp_testing::Controller]"},
    )
    builder = ComponentRuntimeBuilder(data_manager=FakeDataManager(description))

    runtime = builder._build_script_from_description_path(
        "/workspace/script.json", configuration="Alternative"
    )

    assert list(runtime.get_context().enumerate_subcomponent_contexts("controllers")) == []


def test_builder_rejects_unknown_script_configuration():
    description = SimpleNamespace(
        configurations={"Default": {"Components": {}}},
        active_configuration="Default",
        spec={},
    )
    builder = ComponentRuntimeBuilder(data_manager=FakeDataManager(description))

    with pytest.raises(
            RuntimeError, match="configuration 'Missing' is not defined"):
        builder._build_script_from_description_path(
            "/workspace/script.json", configuration="Missing"
        )


def test_data_manager_resolves_linked_script_from_current_workspace(
        tmp_path: Path):
    workspace_path = tmp_path / "consumer" / ".rppws"
    descriptions_path = workspace_path / "script_descriptions"
    descriptions_path.mkdir(parents=True)
    linked_description = descriptions_path / "linked_script.json"
    linked_description.write_text(json.dumps({
        "ScriptLibrary": "provider",
        "ScriptName": "provider::simulation",
        "Configurations": {"Default": {"Components": {}}},
        "ActiveConfiguration": "Default",
    }), encoding="utf-8")

    data_manager = DataManager.__new__(DataManager)
    data_manager.workspace_path = workspace_path

    result = data_manager.get_script_description_path_from_library(
        "provider", "simulation"
    )

    assert result == str(linked_description)


def test_data_manager_rejects_mismatched_qualified_script_name(tmp_path: Path):
    data_manager = DataManager.__new__(DataManager)
    data_manager.workspace_path = tmp_path / ".rppws"

    with pytest.raises(ValueError, match="does not belong to library 'provider'"):
        data_manager.get_script_description_path_from_library(
            "provider", "other::simulation"
        )


def test_builder_builds_script_from_library():
    description = SimpleNamespace(
        configurations={"Default": {"Components": {}}},
        active_configuration="Default",
        spec={},
    )
    data_manager = FakeDataManager(description)
    data_manager.get_script_description_path_from_library = (
        lambda library_name, script_name:
        f"/workspace/{library_name}/{script_name}.json"
    )
    builder = ComponentRuntimeBuilder(data_manager=data_manager)

    runtime = builder.build_script_from_library("provider", "simulation")

    assert runtime.get_context().list_subcomponents() == []


def test_builder_builds_script_from_path():
    description = SimpleNamespace(
        configurations={"Default": {"Components": {}}},
        active_configuration="Default",
        spec={},
    )
    builder = ComponentRuntimeBuilder(
        data_manager=FakeDataManager(description)
    )

    runtime = builder.build_script_from_path("/workspace/simulation.py")

    assert runtime.get_context().list_subcomponents() == []


def test_component_context_exposes_all_components_in_a_slot():
    first = ComponentContext(instance="first")
    second = ComponentContext(instance="second")
    context = ComponentContext(
        subcomponents={"items": [first, second]},
        spec={"items": "List[test::Item]"},
    )

    assert list(context.enumerate_subcomponent_instances("items")) == ["first", "second"]
    views = list(context.enumerate_subcomponent_contexts("items"))
    assert [view.get_instance() for view in views] == ["first", "second"]
    assert context.get_subcomponent_context("items", "0").get_instance() == "first"


@pytest.mark.parametrize(
    ("source_language", "expected"),
    [("python", "native"), ("cpp", "adapted")],
)
def test_builder_dispatches_component_by_source_language(
        source_language, expected):
    record = SimpleNamespace(plugin_name="test::Plugin", subcomponents={}, id="fake-id")
    data_manager = SimpleNamespace(
        get_plugin_info_from_lib=lambda _name: {
            "SourceLanguage": source_language,
        }
    )
    builder = ComponentRuntimeBuilder(data_manager=data_manager)
    builder.resolve_component = lambda *_args: record
    builder._prepare_component_processes = lambda *_args: "prepared"
    builder._build_native_component = lambda *_args: "native"
    builder._build_adapted_component = lambda *_args: "adapted"

    result = builder._build_component_from_path("/parts/component")

    assert result == expected


def test_adapted_component_is_not_configured_or_cleaned_as_native():
    class FakeInstance:
        def on_configure(self, _context):
            raise AssertionError("Adapted instance must be configured remotely")

        def on_cleanup(self):
            raise AssertionError("Adapted instance must be cleaned remotely")

    from unittest.mock import Mock
    from rpp_py.lifecycle import CallbackResult
    runtime = Mock(configure=Mock(return_value=CallbackResult()),
                   cleanup=Mock(return_value=CallbackResult()))
    context = ComponentContext._create(instance=FakeInstance(), runtime=runtime)
    from rpp_py.runtime import ComponentRuntime

    facade = ComponentRuntime(context)
    assert facade.configure().success
    assert facade.cleanup().success


def test_external_component_process_builds_language_specific_command():
    process = ExternalComponentProcess(
        components=[(
            object(),
            ExternalComponentSpec(
                "/workspace/.rppws/parts/component",
                "test::Plugin",
                "component_connection",
            ),
        )],
        rpp_home="/rpp/home",
        command="rpp_component_server_cpp",
        host="127.0.0.1",
        port=12345,
    )

    assert process.command_args == [
        "rpp_component_server_cpp",
        "--host", "127.0.0.1",
        "--port", "12345",
        "--home", "/rpp/home",
        "--path", "/workspace/.rppws/parts/component",
        "--plugin", "test::Plugin",
        "--conn", "component_connection",
    ]


def test_external_component_process_registry_dispatches_by_language():
    registry = ExternalComponentProcessRegistry()
    registry.register("future_language", lambda **kwargs: kwargs["value"])

    result = registry.create("FUTURE_LANGUAGE", value="created")

    assert result == "created"


def test_external_component_process_registry_rejects_unregistered_language():
    registry = ExternalComponentProcessRegistry()

    with pytest.raises(
            RuntimeError, match="No component process is registered"):
        registry.create("future_language")


def test_component_call_executor_runs_calls_and_propagates_errors():
    async def run_test():
        executor = ComponentCallExecutor()
        executor.start()

        async def return_worker_thread_id():
            return threading.get_ident()

        async def raise_error():
            raise ValueError("expected failure")

        async def call_executor_from_worker():
            coroutine = return_worker_thread_id()
            try:
                with pytest.raises(RuntimeError, match="own event-loop thread"):
                    executor.call(coroutine)
            finally:
                coroutine.close()

        try:
            assert executor.call(return_worker_thread_id()) != threading.get_ident()
            with pytest.raises(ValueError, match="expected failure"):
                executor.call(raise_error())
            await asyncio.wrap_future(
                executor.submit(call_executor_from_worker()))
        finally:
            executor.stop()

        coroutine = return_worker_thread_id()
        try:
            with pytest.raises(RuntimeError, match="not running"):
                executor.submit(coroutine)
        finally:
            coroutine.close()

    asyncio.run(run_test())
