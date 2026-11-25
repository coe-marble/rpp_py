"""Host-slot status and protocol compatibility behavior."""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from rpp_py.component_manager import ComponentManager
from rpp_py.context import ComponentContext
from rpp_py.lifecycle import CallbackResult, LifecycleError, LifecycleState
from rpp_py.plugin import Plugin
from rpp_py.plugin_runtime import HostedSlot, PluginRuntimeClient, PluginRuntimeServer, RuntimeConstants


class Recorder(Plugin):
    def __init__(self, events, name):
        self.events = events
        self.name = name

    def on_configure(self, context):
        self.events.append(("configure", self.name))
        return CallbackResult()

    def on_cleanup(self):
        self.events.append(("cleanup", self.name))
        return CallbackResult()


def hosted_slot(*names):
    events = []
    manager = ComponentManager(allow_dynamic_components=True)
    for name in names:
        context = ComponentContext._create(instance=Recorder(events, name), component_name=name)
        manager.adopt(name, context, f"/{name}", initial=True).require_success()
    adapter = Mock()
    adapter.get_info_adapter_server__.return_value = SimpleNamespace(
        plugin_name="testing::Recorder", plugin_type="testing::Recorder")
    return manager, events, HostedSlot(adapter, manager, lambda *_: CallbackResult(), CallbackResult)


def test_host_lists_every_slot_entry_with_immutable_loaded_instance_ids():
    manager, _, slot = hosted_slot("first", "second")
    server = PluginRuntimeServer(slots={"controllers": slot})
    listed = server.list_components()
    assert {component["name"] for component in listed} == {"first", "second"}
    assert all(component["connection_name"] == "controllers" for component in listed)
    assert all(component["instance_id"] for component in listed)
    first_id = next(component["instance_id"] for component in listed if component["name"] == "first")
    assert server._entry_for_instance(first_id)[2] == "first"
    assert manager.unload("first").success
    assert not next(component for component in server.list_components()
                    if component["name"] == "first")["instance_id"]


def test_hosted_slot_manager_controls_full_native_lifecycle_happy_path():
    manager, events, _ = hosted_slot("root")
    assert manager.configure("root").success
    assert manager.get_state("root") is LifecycleState.INACTIVE
    assert manager.activate("root").success
    assert manager.deactivate("root").success
    assert manager.cleanup("root").success
    assert events == [("configure", "root"), ("cleanup", "root")]


def test_client_rejects_incompatible_protocol_and_clears_connection(monkeypatch):
    monkeypatch.setattr(RuntimeConstants, "get_capnp_schema", lambda: SimpleNamespace(
        PluginRuntime=object(), protocolVersion=1))
    backend = SimpleNamespace(getProtocolVersion=AsyncMock(return_value=SimpleNamespace(version=2)))
    context = SimpleNamespace(runtime=object(), get_client=Mock())
    context.get_client.return_value.cast_as.return_value = backend
    client = PluginRuntimeClient()
    with pytest.raises(LifecycleError, match="protocol version"):
        asyncio.run(client.connect(context))
    assert client._client is None
    assert client._context is None


@pytest.mark.parametrize("operation,wire_method,stateful", [
    ("configure_instances", "configureComponent", False),
    ("cleanup_instances", "cleanupComponent", False),
    ("activate_instances", "activateComponent", True),
    ("deactivate_instances", "deactivateComponent", False),
    ("unload_instances", "unloadComponent", False),
    ("reload_instances", "reloadComponent", True),
])
def test_batch_client_preserves_id_and_handover_order(operation, wire_method, stateful):
    response = SimpleNamespace(result=SimpleNamespace(success=True, message=""))
    invoke = AsyncMock(return_value=response)
    client = PluginRuntimeClient()
    client._client = SimpleNamespace(**{wire_method: invoke})
    instance_ids = ["first-instance", "second-instance"]
    states = [object(), object()]
    arguments = [instance_ids, states] if stateful else [instance_ids]
    result = asyncio.run(getattr(client, operation)(*arguments))
    assert result.success
    expected = {"instanceIds": instance_ids}
    if stateful:
        expected["handoverStates"] = states
    invoke.assert_awaited_once_with(**expected)


@pytest.mark.parametrize("operation,wire_method", [
    ("unload_instances", "unloadComponent"),
    ("reload_instances", "reloadComponent"),
])
def test_partial_batch_failure_invalidates_cached_instance_ids(operation, wire_method):
    response = SimpleNamespace(result=SimpleNamespace(success=False, message="second entry failed"))
    client = PluginRuntimeClient()
    client._client = SimpleNamespace(**{wire_method: AsyncMock(return_value=response)})
    client._component_ids_by_connection = {"slot": "first-instance"}
    result = asyncio.run(getattr(client, operation)(["first-instance", "second-instance"]))
    assert not result.success
    assert client._component_ids_by_connection == {}


def test_rpc_switch_decodes_each_state_before_stopping_outgoing_components(monkeypatch):
    @dataclass
    class Snapshot:
        name: str

    class StatefulRecorder(Plugin):
        HANDOVER_STATE_TYPE = Snapshot

        def __init__(self):
            self.received = None

        def on_activate(self, state=None):
            self.received = state
            return CallbackResult()

    manager = ComponentManager()
    plugins = {name: StatefulRecorder() for name in ("outgoing", "first", "second")}
    for name, plugin in plugins.items():
        context = ComponentContext._create(instance=plugin, component_name=name)
        manager.adopt(name, context, name, initial=True).require_success()
        manager.configure(name).require_success()
    manager.activate("outgoing").require_success()
    adapter = Mock()
    adapter.decode_lifecycle_state__.side_effect = (
        lambda schema_id, payload: (CallbackResult(), Snapshot(payload)))
    slot = HostedSlot(adapter, manager, lambda *_: CallbackResult(), CallbackResult)
    owner = PluginRuntimeServer(slots={"controllers": slot})
    monkeypatch.setattr(RuntimeConstants, "get_capnp_schema", lambda: SimpleNamespace(
        PluginRuntime=SimpleNamespace(Server=object), AdapterInfo=object))
    server = owner._init_obj_of_server_class(owner._create_server_class()())
    context = SimpleNamespace(results=SimpleNamespace(result=SimpleNamespace()))
    ids = {name: manager.get_info(name).instance_id for name in plugins}
    states = [SimpleNamespace(schemaId=1, payload="first-state"),
              SimpleNamespace(schemaId=1, payload="second-state")]
    try:
        asyncio.run(server.switchComponents(
            deactivate=[ids["outgoing"]], activate=[ids["first"], ids["second"]],
            handoverStates=states, _context=context))
        assert context.results.result.success
        assert manager.get_state("outgoing") is LifecycleState.INACTIVE
        assert plugins["first"].received == Snapshot("first-state")
        assert plugins["second"].received == Snapshot("second-state")
    finally:
        manager.close()
