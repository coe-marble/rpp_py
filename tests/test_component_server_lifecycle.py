"""The component host configures plugins and always releases their resources."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from rpp_common.lifecycle import CallbackResult, LifecycleError
from rpp_py.cli import component_server


@pytest.fixture
def server(monkeypatch):
    context = SimpleNamespace(close=Mock(),
                              configure=Mock(return_value=CallbackResult()),
                              cleanup=Mock(return_value=CallbackResult()),
                              get_instance=Mock())
    builder = Mock()
    builder.build_from_component_roots.return_value = [context]
    monkeypatch.setattr(component_server, "ComponentContextBuilder", Mock(return_value=builder))
    library = Mock()
    library.get_plugin_info_from_lib.return_value = {"PluginName": "test"}
    monkeypatch.setattr(component_server, "LibraryManager", Mock(return_value=library))
    monkeypatch.setattr(component_server, "DataManager", Mock())
    host = Mock(run_async=AsyncMock())
    monkeypatch.setattr(component_server, "RppServerHost", Mock(return_value=host))
    monkeypatch.setattr(component_server, "PluginAdapter", Mock())
    monkeypatch.setattr(component_server, "RppLogger", Mock())
    args = SimpleNamespace(host="localhost", port=1234, home="/tmp/unused",
                           path=["component"], plugin=["test"], conn=["connection"])
    return SimpleNamespace(context=context, host=host, args=args)


def test_host_configures_before_serving_and_cleans_before_stopping(server):
    events = []
    server.context.configure.side_effect = lambda: events.append("configure")
    server.host.run_async.side_effect = lambda: events.append("serve")
    server.context.cleanup.side_effect = lambda: events.append("cleanup")
    server.context.close.side_effect = lambda: events.append("close")

    asyncio.run(component_server._run(server.args))

    assert events == ["configure", "serve", "cleanup", "close"]


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_configuration_error_survives_shutdown_and_host_never_serves(server, cleanup_fails):
    failure = LifecycleError("on_configure rejected")
    server.context.configure.side_effect = failure
    if cleanup_fails:
        server.context.cleanup.side_effect = LifecycleError("on_cleanup rejected")

    with pytest.raises(LifecycleError) as caught:
        asyncio.run(component_server._run(server.args))

    assert caught.value is failure
    server.host.run_async.assert_not_awaited()
    server.context.cleanup.assert_called_once()
    server.context.close.assert_called_once()
