import json
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from rpp_orchestrator.component_storage import ComponentRecord
from rpp_py.context_builder import ComponentContextBuilder
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
    builder = ComponentContextBuilder(data_manager=FakeDataManager(description))
    builder._build_component_from_path = lambda path: path

    context = builder._build_script_from_description_path(
        "/workspace/script.json"
    )

    assert context.get_subcomponent_context("vehicle") == [
        "/parts/more_dynamics::HullVessel/vessel-id",
    ]


def test_builder_can_select_an_explicit_script_configuration():
    description = SimpleNamespace(
        configurations={
            "Default": {"Components": {}},
            "Alternative": {"Components": {"controllers": []}},
        },
        active_configuration="Default",
        spec={"controllers": "List[rpp_testing::Controller]"},
    )
    builder = ComponentContextBuilder(data_manager=FakeDataManager(description))

    context = builder._build_script_from_description_path(
        "/workspace/script.json", configuration="Alternative"
    )

    assert context.get_subcomponent_context("controllers") == []


def test_builder_rejects_unknown_script_configuration():
    description = SimpleNamespace(
        configurations={"Default": {"Components": {}}},
        active_configuration="Default",
        spec={},
    )
    builder = ComponentContextBuilder(data_manager=FakeDataManager(description))

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
    builder = ComponentContextBuilder(data_manager=data_manager)

    context = builder.build_script_from_library("provider", "simulation")

    assert context.list_subcomponents() == []


def test_builder_builds_script_from_path():
    description = SimpleNamespace(
        configurations={"Default": {"Components": {}}},
        active_configuration="Default",
        spec={},
    )
    builder = ComponentContextBuilder(
        data_manager=FakeDataManager(description)
    )

    context = builder.build_script_from_path("/workspace/simulation.py")

    assert context.list_subcomponents() == []


def test_component_context_exposes_all_components_in_a_slot():
    first = ComponentContext(instance="first")
    second = ComponentContext(instance="second")
    context = ComponentContext(
        subcomponents={"items": [first, second]},
        spec={"items": "List[test::Item]"},
    )

    assert context.get_components("items") == ["first", "second"]
    assert context.get_subcomponent_contexts("items") == [first, second]
    assert context.get_subcomponent_context("items") == [first, second]


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
    builder = ComponentContextBuilder(data_manager=data_manager)
    builder.resolve_component = lambda *_args: record
    builder._prepare_component_processes = lambda *_args: "prepared"
    builder._build_native_component = lambda *_args: "native"
    builder._build_adapted_component = lambda *_args: "adapted"

    result = builder.build_component_from_path("/parts/component")

    assert result == expected


def test_component_context_starts_and_stops_runtime():
    events = []

    class FakeRuntime:
        async def start(self):
            events.append("start")

        async def stop(self):
            events.append("stop")

    context = ComponentContext(instance=object(), runtime=FakeRuntime())

    asyncio.run(context.start())
    asyncio.run(context.stop())

    assert events == ["start", "stop"]


def test_adapted_component_is_not_initialized_as_native():
    class FakeInstance:
        def initialize(self, _context):
            raise AssertionError("Adapted instance must be initialized remotely")

    context = ComponentContext(instance=FakeInstance(), runtime=object())

    context.initialize()


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
