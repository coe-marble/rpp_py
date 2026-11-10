"""Host-slot status and protocol compatibility behavior."""

import asyncio
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
    assert manager.deactivate("root").result.success
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
