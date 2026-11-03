"""Component lifecycle tests without plugins, servers, or generated schemas."""

import asyncio
from unittest.mock import Mock

import pytest

from rpp_py.context import ComponentContext


class RecordingRuntime:
    def __init__(self, name, events, *, start_error=None, stop_error=None):
        self.name = name
        self.events = events
        self.start_error = start_error
        self.stop_error = stop_error

    async def start(self):
        self.events.append(("start", self.name))
        if self.start_error is not None:
            raise self.start_error

    async def stop(self):
        self.events.append(("stop", self.name))
        if self.stop_error is not None:
            raise self.stop_error


def make_context(name, events, **kwargs):
    return ComponentContext(
        runtime=RecordingRuntime(name, events, **kwargs), logger=Mock())


def test_start_children_before_parent_and_stop_in_reverse_order():
    events = []
    context = ComponentContext(
        runtime=RecordingRuntime("parent", events),
        subcomponents={
            "single": make_context("first", events),
            "multiple": [make_context("second", events),
                         make_context("third", events)],
        },
        logger=Mock(),
    )

    async def exercise():
        await context.start()
        await context.stop()

    asyncio.run(exercise())

    assert events == [
        ("start", "first"), ("start", "second"), ("start", "third"),
        ("start", "parent"), ("stop", "parent"), ("stop", "third"),
        ("stop", "second"), ("stop", "first"),
    ]


def test_failed_child_start_rolls_back_started_siblings_only():
    events = []
    failure = ValueError("child startup failed")
    context = ComponentContext(
        runtime=RecordingRuntime("parent", events),
        subcomponents={"children": [
            make_context("first", events),
            make_context("second", events),
            make_context("failed", events, start_error=failure),
            make_context("unreached", events),
        ]},
        logger=Mock(),
    )

    with pytest.raises(ValueError) as caught:
        asyncio.run(context.start())

    assert caught.value is failure
    assert events == [
        ("start", "first"), ("start", "second"), ("start", "failed"),
        ("stop", "second"), ("stop", "first"),
    ]


def test_failed_parent_start_rolls_back_all_started_children():
    events = []
    failure = ValueError("parent startup failed")
    context = ComponentContext(
        runtime=RecordingRuntime("parent", events, start_error=failure),
        subcomponents={"children": [make_context("first", events),
                                    make_context("second", events)]},
        logger=Mock(),
    )

    with pytest.raises(ValueError) as caught:
        asyncio.run(context.start())

    assert caught.value is failure
    assert events == [
        ("start", "first"), ("start", "second"), ("start", "parent"),
        ("stop", "second"), ("stop", "first"),
    ]


@pytest.mark.parametrize("failure_at", ["parent", "second"])
def test_stop_attempts_all_children_when_one_stop_fails(failure_at):
    events = []
    failure = ValueError("shutdown failed")
    context = ComponentContext(
        runtime=RecordingRuntime(
            "parent", events,
            stop_error=failure if failure_at == "parent" else None),
        subcomponents={"children": [
            make_context("first", events),
            make_context("second", events,
                         stop_error=failure if failure_at == "second" else None),
        ]},
        logger=Mock(),
    )

    with pytest.raises(ValueError) as caught:
        asyncio.run(context.stop())

    assert caught.value is failure
    assert events == [("stop", "parent"), ("stop", "second"), ("stop", "first")]


def test_rollback_continues_and_preserves_startup_error_when_cleanup_fails():
    events = []
    startup_failure = ValueError("startup failed")
    context = ComponentContext(
        runtime=RecordingRuntime("parent", events, start_error=startup_failure),
        subcomponents={"children": [
            make_context("first", events),
            make_context("second", events, stop_error=RuntimeError("cleanup failed")),
        ]},
        logger=Mock(),
    )

    with pytest.raises(ValueError) as caught:
        asyncio.run(context.start())

    assert caught.value is startup_failure
    assert events[-2:] == [("stop", "second"), ("stop", "first")]
    context.get_logger().error.assert_called_once()
    assert "cleanup failed" in context.get_logger().error.call_args.args[0]


def test_stop_preserves_first_error_and_logs_additional_failures():
    events = []
    failure = ValueError("parent shutdown failed")
    logger = Mock()
    context = ComponentContext(
        runtime=RecordingRuntime("parent", events, stop_error=failure),
        subcomponents={"children": [
            make_context("first", events, stop_error=RuntimeError("first cleanup failed")),
            make_context("second", events, stop_error=RuntimeError("second cleanup failed")),
        ]},
        logger=logger,
    )

    with pytest.raises(ValueError) as caught:
        asyncio.run(context.stop())

    assert caught.value is failure
    assert events == [("stop", "parent"), ("stop", "second"), ("stop", "first")]
    assert logger.error.call_count == 2
    messages = [entry.args[0] for entry in logger.error.call_args_list]
    assert "second cleanup failed" in messages[0]
    assert "first cleanup failed" in messages[1]
