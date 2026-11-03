"""Process ownership tests with mocked subprocesses and RPC clients."""

import asyncio
from concurrent.futures import Future
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

import rpp_py.external_component_process as processes


@pytest.fixture
def harness(monkeypatch):
    async def to_thread(function, *args, **kwargs):
        # All blocking process operations below are mocks.
        return function(*args, **kwargs)

    monkeypatch.setattr(processes.asyncio, "to_thread", to_thread)
    child = Mock()
    popen = Mock(return_value=child)
    monkeypatch.setattr(processes.subprocess, "Popen", popen)
    connection = SimpleNamespace(
        start=AsyncMock(return_value=(True, None)), stop=AsyncMock())
    runtime = SimpleNamespace(
        connect=AsyncMock(), shutdown=AsyncMock(), disconnect=AsyncMock())
    monkeypatch.setattr(processes, "RppRuntimeClientContext", Mock(return_value=connection))
    monkeypatch.setattr(processes, "PluginRuntimeClient", Mock(return_value=runtime))
    adapters = [SimpleNamespace(
        _connect_adapter_client_with_executor__=AsyncMock(),
        _disconnect_adapter_client_on_executor__=AsyncMock(),
    ) for _ in range(2)]

    def submit(coroutine):
        # Keep process tests on this loop; the executor has its own tests.
        future = Future()

        async def execute():
            try:
                future.set_result(await coroutine)
            except Exception as error:
                future.set_exception(error)

        asyncio.get_running_loop().create_task(execute())
        return future

    executor = SimpleNamespace(start=Mock(), stop=Mock(), submit=submit)
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
    async def exercise():
        await harness.process.start()
        await harness.process.start()
        await harness.process.stop()
        await harness.process.stop()

    asyncio.run(exercise())

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

    async def exercise():
        await harness.process.start()
        await harness.process.stop()

    asyncio.run(exercise())

    expected = [call.wait(timeout=2), call.terminate(), call.wait(timeout=2)]
    if timeouts == 2:
        expected.extend([call.kill(), call.wait()])
    assert harness.child.mock_calls == expected


def test_connection_timeout_cleans_up_and_preserves_cause(harness):
    failure = TimeoutError("server unavailable")
    harness.connection.start.return_value = (False, failure)

    with pytest.raises(RuntimeError, match="Failed to connect") as caught:
        asyncio.run(harness.process.start())

    assert caught.value.__cause__ is failure
    harness.runtime.connect.assert_not_awaited()
    harness.connection.stop.assert_awaited_once_with()
    harness.child.wait.assert_called_once_with(timeout=2)


def test_adapter_connection_failure_cleans_up_partial_start(harness):
    failure = ValueError("second adapter failed")
    harness.adapters[1]._connect_adapter_client_with_executor__.side_effect = failure

    with pytest.raises(ValueError) as caught:
        asyncio.run(harness.process.start())

    assert caught.value is failure
    harness.adapters[0]._disconnect_adapter_client_on_executor__.assert_awaited_once()
    harness.connection.stop.assert_awaited_once_with()
    harness.child.wait.assert_called_once_with(timeout=2)
    harness.runtime.connect.assert_not_awaited()


def test_spawn_failure_propagates_without_attempting_rpc(harness):
    failure = FileNotFoundError("component server missing")
    harness.popen.side_effect = failure

    with pytest.raises(FileNotFoundError) as caught:
        asyncio.run(harness.process.start())

    assert caught.value is failure
    harness.connection.start.assert_not_awaited()
    harness.child.wait.assert_not_called()


def test_shutdown_rpc_failure_still_disconnects_and_reaps_process(harness):
    harness.runtime.shutdown.side_effect = ConnectionError("server already exited")

    async def exercise():
        await harness.process.start()
        await harness.process.stop()

    asyncio.run(exercise())

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

    async def exercise():
        await harness.process.start()
        with pytest.raises(ValueError) as caught:
            await harness.process.stop()
        assert caught.value is failure

    asyncio.run(exercise())

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

    async def exercise():
        await handles[0].start()
        await handles[1].start()
        await handles[0].stop()
        await handles[0].stop()
        harness.child.wait.assert_not_called()
        harness.runtime.shutdown.assert_not_awaited()
        await handles[1].stop()
        await handles[1].stop()

    asyncio.run(exercise())

    harness.popen.assert_called_once()
    for adapter in harness.adapters:
        adapter._connect_adapter_client_with_executor__.assert_awaited_once()
        adapter._disconnect_adapter_client_on_executor__.assert_awaited_once()
    harness.runtime.shutdown.assert_awaited_once_with()
    harness.child.wait.assert_called_once_with(timeout=2)


def test_startup_error_survives_failed_rollback(harness):
    startup_failure = ValueError("adapter startup failed")
    harness.adapters[1]._connect_adapter_client_with_executor__.side_effect = startup_failure
    harness.adapters[0]._disconnect_adapter_client_on_executor__.side_effect = RuntimeError(
        "adapter cleanup failed")

    with pytest.raises(ValueError) as caught:
        asyncio.run(harness.process.start())

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

    async def exercise():
        await harness.process.start()
        with pytest.raises(ValueError) as caught:
            await harness.process.stop()
        assert caught.value is failure

    asyncio.run(exercise())

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

    async def exercise():
        await harness.process.start()
        with pytest.raises(RuntimeError) as caught:
            await harness.process.stop()
        assert caught.value is failure

    asyncio.run(exercise())

    harness.connection.stop.assert_awaited_once_with()
    harness.child.wait.assert_called_once_with(timeout=2)


def test_process_wait_failure_can_be_retried_without_reconnecting(harness):
    failure = OSError("wait failed")
    harness.child.wait.side_effect = [failure, 0]

    async def exercise():
        await harness.process.start()
        with pytest.raises(OSError) as caught:
            await harness.process.stop()
        assert caught.value is failure
        await harness.process.stop()

    asyncio.run(exercise())

    assert harness.child.wait.call_args_list == [call(timeout=2), call(timeout=2)]
    harness.runtime.disconnect.assert_awaited_once_with()
    harness.connection.stop.assert_awaited_once_with()
    harness.executor.stop.assert_called_once_with()
