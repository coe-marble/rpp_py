"""Process ownership tests with mocked subprocesses and RPC clients."""

import asyncio
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

import rpp_py.external_component_process as processes
from rpp_py.lifecycle import CallbackResult


@pytest.fixture
def harness(monkeypatch):
    child = Mock()
    popen = Mock(return_value=child)
    monkeypatch.setattr(processes.subprocess, "Popen", popen)
    connection = SimpleNamespace(
        start=AsyncMock(return_value=(True, None)), stop=AsyncMock())
    runtime = SimpleNamespace(
        connect=AsyncMock(), shutdown=AsyncMock(), disconnect=AsyncMock(),
        configure_component=AsyncMock(return_value=CallbackResult()),
        cleanup_component=AsyncMock(return_value=CallbackResult()))
    monkeypatch.setattr(processes, "RppRuntimeClientContext", Mock(return_value=connection))
    monkeypatch.setattr(processes, "PluginRuntimeClient", Mock(return_value=runtime))
    adapters = [SimpleNamespace(
        _connect_adapter_client_with_executor__=AsyncMock(),
        _disconnect_adapter_client_on_executor__=AsyncMock(),
    ) for _ in range(2)]

    executor = SimpleNamespace(start=Mock(), stop=Mock(),
                               call=lambda coroutine: asyncio.run(coroutine))
    monkeypatch.setattr(processes, "ComponentCallExecutor", Mock(return_value=executor))
    process = processes.ExternalComponentProcess(
        components=[(adapter, processes.ExternalComponentSpec(
            f"/components/{index}", f"library::Plugin{index}", f"connection{index}"))
            for index, adapter in enumerate(adapters)],
        rpp_home="/rpp/home", command="component-server", port=12345,
        startup_timeout_ms=123, logger=Mock(),
    )
    return SimpleNamespace(
        process=process, child=child, popen=popen, connection=connection,
        runtime=runtime, adapters=adapters, executor=executor,
    )


def test_start_and_stop_are_idempotent(harness):
    def exercise():
        harness.process.prepare()
        harness.process.prepare()
        harness.process.close()
        harness.process.close()

    exercise()

    harness.popen.assert_called_once_with(harness.process.command_args)
    harness.connection.start.assert_awaited_once_with(123)
    for adapter in harness.adapters:
        adapter._connect_adapter_client_with_executor__.assert_awaited_once()
        adapter._disconnect_adapter_client_on_executor__.assert_awaited_once()
    harness.runtime.connect.assert_awaited_once_with(context=harness.connection)
    harness.runtime.shutdown.assert_awaited_once_with()
    harness.runtime.disconnect.assert_awaited_once_with()
    harness.connection.stop.assert_awaited_once_with()
    harness.child.wait.assert_called_once_with(timeout=2)
    harness.child.terminate.assert_not_called()
    harness.child.kill.assert_not_called()
    harness.executor.stop.assert_called_once_with()


@pytest.mark.parametrize("timeouts", [1, 2])
def test_stop_escalates_to_terminate_then_kill(harness, timeouts):
    harness.child.wait.side_effect = [
        subprocess.TimeoutExpired("component-server", 2)
        for _ in range(timeouts)
    ] + [0]

    def exercise():
        harness.process.prepare()
        harness.process.close()

    exercise()

    expected = [call.wait(timeout=2), call.terminate(), call.wait(timeout=2)]
    if timeouts == 2:
        expected.extend([call.kill(), call.wait()])
    assert harness.child.mock_calls == expected


def test_connection_timeout_cleans_up_and_preserves_cause(harness):
    failure = TimeoutError("server unavailable")
    harness.connection.start.return_value = (False, failure)

    with pytest.raises(RuntimeError, match="Failed to connect") as caught:
        harness.process.prepare()

    assert caught.value.__cause__ is failure
    harness.runtime.connect.assert_not_awaited()
    harness.connection.stop.assert_awaited_once_with()
    harness.child.wait.assert_called_once_with(timeout=2)


def test_adapter_connection_failure_cleans_up_partial_start(harness):
    failure = ValueError("second adapter failed")
    harness.adapters[1]._connect_adapter_client_with_executor__.side_effect = failure

    with pytest.raises(ValueError) as caught:
        harness.process.prepare()

    assert caught.value is failure
    harness.adapters[0]._disconnect_adapter_client_on_executor__.assert_awaited_once()
    harness.connection.stop.assert_awaited_once_with()
    harness.child.wait.assert_called_once_with(timeout=2)
    harness.runtime.connect.assert_not_awaited()


def test_spawn_failure_propagates_without_attempting_rpc(harness):
    failure = FileNotFoundError("component server missing")
    harness.popen.side_effect = failure

    with pytest.raises(FileNotFoundError) as caught:
        harness.process.prepare()

    assert caught.value is failure
    harness.connection.start.assert_not_awaited()
    harness.child.wait.assert_not_called()


def test_shutdown_rpc_failure_still_disconnects_and_reaps_process(harness):
    harness.runtime.shutdown.side_effect = ConnectionError("server already exited")

    def exercise():
        harness.process.prepare()
        harness.process.close()

    exercise()

    harness.runtime.disconnect.assert_awaited_once_with()
    harness.connection.stop.assert_awaited_once_with()
    harness.child.wait.assert_called_once_with(timeout=2)


@pytest.mark.parametrize("failure_at", ["runtime", "adapter", "connection"])
def test_disconnect_failure_still_releases_remaining_resources(harness, failure_at):
    failure = ValueError("disconnect failed")
    targets = {
        "runtime": harness.runtime.disconnect,
        "adapter": harness.adapters[1]._disconnect_adapter_client_on_executor__,
        "connection": harness.connection.stop,
    }
    targets[failure_at].side_effect = failure

    def exercise():
        harness.process.prepare()
        with pytest.raises(ValueError) as caught:
            harness.process.close()
        assert caught.value is failure

    exercise()

    for adapter in harness.adapters:
        adapter._disconnect_adapter_client_on_executor__.assert_awaited_once()
    harness.connection.stop.assert_awaited_once_with()
    harness.child.wait.assert_called_once_with(timeout=2)
    harness.executor.stop.assert_called_once_with()


def test_shared_process_stops_only_after_last_owner_releases(harness):
    registry = processes.ExternalComponentProcessRegistry()
    registry.register("cpp", lambda **kwargs: processes.ExternalComponentProcess(
        command="component-server", port=12345, logger=Mock(), **kwargs),
        share_process=True)
    handles = [registry.create(
        "cpp", adapter_client=adapter, component_path=f"/components/{index}",
        plugin_name=f"library::Plugin{index}", connection_name=f"connection{index}",
        rpp_home="/rpp/home",
    ) for index, adapter in enumerate(harness.adapters)]

    def exercise():
        handles[0].prepare()
        handles[1].prepare()
        assert handles[0].configure().success
        assert handles[1].configure().success
        assert handles[0].cleanup().success
        handles[0].close()
        handles[0].close()
        harness.child.wait.assert_not_called()
        harness.runtime.shutdown.assert_not_awaited()
        handles[1].close()
        handles[1].close()

    exercise()

    harness.popen.assert_called_once()
    for adapter in harness.adapters:
        adapter._connect_adapter_client_with_executor__.assert_awaited_once()
        adapter._disconnect_adapter_client_on_executor__.assert_awaited_once()
    harness.runtime.shutdown.assert_awaited_once_with()
    assert harness.runtime.configure_component.await_args_list == [call("connection0"), call("connection1")]
    harness.runtime.cleanup_component.assert_awaited_once_with("connection0")
    harness.child.wait.assert_called_once_with(timeout=2)


def test_registered_server_commands_are_normalized_and_preserved_by_clones(monkeypatch):
    monkeypatch.setattr(processes, "_get_available_port", lambda: 12345)
    registry = processes.ExternalComponentProcessRegistry()
    registry.register_server(" CPP ", "/servers/original")
    snapshot = registry.clone()
    registry.register_server("cpp", "/servers/replacement")
    handles = []
    for configured, expected in [(snapshot, "/servers/original"), (registry, "/servers/replacement")]:
        handle = configured.create("cpp", adapter_client=Mock(), component_path="/component",
            plugin_name="example::Plugin", connection_name="example", slot_declaration="example::Type",
            rpp_home="/home")
        handles.append(handle)
        assert handle._process.command_args[0] == expected
        assert "--slot" in handle._process.command_args
        assert not handle._process._started
    for handle in handles:
        handle.close()
    with pytest.raises(ValueError, match="required"):
        registry.register_server("cpp", "")


def test_new_build_gets_its_own_host_without_closing_the_previous_owner(harness):
    registry = processes.ExternalComponentProcessRegistry()
    registry.register("cpp", lambda **kwargs: processes.ExternalComponentProcess(
        command="component-server", port=12345, logger=Mock(), **kwargs), share_process=True)

    def create(index):
        return registry.create("cpp", adapter_client=harness.adapters[index],
            component_path=f"/components/{index}", plugin_name=f"lib::Plugin{index}",
            connection_name=f"connection{index}", rpp_home="/rpp/home")

    first = create(0)
    first.prepare()
    registry.begin_build()
    second = create(1)
    second.prepare()
    assert harness.popen.call_count == 2
    second.close()
    assert harness.child.wait.call_count == 1
    first.close()
    assert harness.child.wait.call_count == 2


def test_unprepared_shared_reservation_is_released_after_partial_build(harness):
    registry = processes.ExternalComponentProcessRegistry()
    registry.register("cpp", lambda **kwargs: processes.ExternalComponentProcess(
        command="component-server", port=12345, logger=Mock(), **kwargs), share_process=True)
    handles = [registry.create("cpp", adapter_client=adapter,
        component_path=f"/components/{index}", plugin_name=f"lib::Plugin{index}",
        connection_name=f"connection{index}", rpp_home="/rpp/home")
        for index, adapter in enumerate(harness.adapters)]
    handles[0].prepare()
    handles[1].close()
    harness.child.wait.assert_not_called()
    handles[0].close()
    harness.child.wait.assert_called_once_with(timeout=2)


def test_last_shared_owner_can_retry_failed_process_release(harness):
    registry = processes.ExternalComponentProcessRegistry()
    registry.register("cpp", lambda **kwargs: processes.ExternalComponentProcess(
        command="component-server", port=12345, logger=Mock(), **kwargs), share_process=True)
    handle = registry.create("cpp", adapter_client=harness.adapters[0],
        component_path="/component", plugin_name="lib::Plugin", connection_name="connection",
        rpp_home="/rpp/home")
    handle.prepare()
    harness.child.wait.side_effect = [OSError("wait failed"), 0]
    with pytest.raises(OSError, match="wait failed"):
        handle.close()
    handle.close()
    assert harness.child.wait.call_count == 2
    harness.executor.stop.assert_called_once()


def test_startup_error_survives_failed_rollback(harness):
    startup_failure = ValueError("adapter startup failed")
    harness.adapters[1]._connect_adapter_client_with_executor__.side_effect = startup_failure
    harness.adapters[0]._disconnect_adapter_client_on_executor__.side_effect = RuntimeError(
        "adapter cleanup failed")

    with pytest.raises(ValueError) as caught:
        harness.process.prepare()

    assert caught.value is startup_failure
    harness.connection.stop.assert_awaited_once_with()
    harness.executor.stop.assert_called_once_with()
    harness.child.wait.assert_called_once_with(timeout=2)
    harness.process.logger.error.assert_called_once()
    assert "adapter cleanup failed" in harness.process.logger.error.call_args.args[0]


def test_multiple_cleanup_errors_preserve_first_and_log_the_rest(harness):
    failure = ValueError("runtime disconnect failed")
    harness.runtime.disconnect.side_effect = failure
    harness.adapters[1]._disconnect_adapter_client_on_executor__.side_effect = RuntimeError(
        "adapter disconnect failed")
    harness.connection.stop.side_effect = RuntimeError("connection stop failed")
    harness.executor.stop.side_effect = RuntimeError("executor stop failed")

    def exercise():
        harness.process.prepare()
        with pytest.raises(ValueError) as caught:
            harness.process.close()
        assert caught.value is failure

    exercise()

    for adapter in harness.adapters:
        adapter._disconnect_adapter_client_on_executor__.assert_awaited_once()
    harness.connection.stop.assert_awaited_once_with()
    harness.executor.stop.assert_called_once_with()
    harness.child.wait.assert_called_once_with(timeout=2)
    messages = [entry.args[0] for entry in harness.process.logger.error.call_args_list]
    assert len(messages) == 3
    for expected in ("adapter disconnect failed", "connection stop failed", "executor stop failed"):
        assert any(expected in message for message in messages)


def test_executor_stop_failure_still_reaps_process(harness):
    failure = RuntimeError("executor stop failed")
    harness.executor.stop.side_effect = failure

    def exercise():
        harness.process.prepare()
        with pytest.raises(RuntimeError) as caught:
            harness.process.close()
        assert caught.value is failure

    exercise()

    harness.connection.stop.assert_awaited_once_with()
    harness.child.wait.assert_called_once_with(timeout=2)


def test_process_wait_failure_can_be_retried_without_reconnecting(harness):
    failure = OSError("wait failed")
    harness.child.wait.side_effect = [failure, 0]

    def exercise():
        harness.process.prepare()
        with pytest.raises(OSError) as caught:
            harness.process.close()
        assert caught.value is failure
        harness.process.close()

    exercise()

    assert harness.child.wait.call_args_list == [call(timeout=2), call(timeout=2)]
    harness.runtime.disconnect.assert_awaited_once_with()
    harness.connection.stop.assert_awaited_once_with()
    harness.executor.stop.assert_called_once_with()
