"""The builder hands complete host-owned slots to the component server."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from rpp_py.lifecycle import CallbackResult, LifecycleError
from rpp_py.cli import component_server


@pytest.fixture
def server(monkeypatch):
    builder = Mock()
    host = Mock(run_async=AsyncMock(), close=Mock(return_value=CallbackResult()))
    builder.build_component_server.return_value = host
    monkeypatch.setattr(component_server, "ComponentRuntimeBuilder", Mock(return_value=builder))
    library = Mock()
    library.get_plugin_info_from_lib.return_value = {
        "PluginName": "test", "PluginType": "test::Plugin",
    }
    monkeypatch.setattr(component_server, "LibraryManager", Mock(return_value=library))
    monkeypatch.setattr(component_server, "DataManager", Mock())
    monkeypatch.setattr(component_server, "RppLogger", Mock())
    args = SimpleNamespace(host="localhost", port=1234, home="/tmp/unused",
                           path=["component"], plugin=["test"], conn=["connection"])
    return SimpleNamespace(host=host, builder=builder, args=args)


def test_host_serves_builder_owned_slots_without_entering_lifecycle(server):
    events = []
    server.host.run_async.side_effect = lambda: events.append("serve")
    server.host.close.side_effect = lambda: events.append("close") or CallbackResult()

    asyncio.run(component_server._run(server.args))

    assert events == ["serve", "close"]
    server.builder.build_component_server.assert_called_once()


@pytest.mark.parametrize("close_fails", [False, True])
def test_host_error_survives_slot_release(server, close_fails):
    failure = LifecycleError("host rejected")
    server.host.run_async.side_effect = failure
    if close_fails:
        server.host.close.side_effect = LifecycleError("close rejected")

    with pytest.raises(LifecycleError) as caught:
        asyncio.run(component_server._run(server.args))

    assert caught.value is failure
    server.host.close.assert_called_once()
