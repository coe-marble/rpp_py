from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any, Coroutine, TypeVar


ResultT = TypeVar("ResultT")


class ComponentCallExecutor:
    """Execute component RPC coroutines on one dedicated loop thread."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="rpp-component-rpc", daemon=True)
        self._started = False

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        self._loop.close()

    def start(self) -> None:
        if self._started:
            return
        self._thread.start()
        self._ready.wait()
        self._started = True

    def submit(self, coroutine: Coroutine[Any, Any, ResultT]) \
            -> concurrent.futures.Future[ResultT]:
        if not self._started:
            raise RuntimeError("Component call executor is not running.")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def call(self, coroutine: Coroutine[Any, Any, ResultT]) -> ResultT:
        if threading.current_thread() is self._thread:
            raise RuntimeError(
                "Component call executor cannot block its own event-loop thread.")
        return self.submit(coroutine).result()

    def stop(self) -> None:
        if not self._started:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._started = False
