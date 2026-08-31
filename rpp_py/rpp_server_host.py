import asyncio

from rpp_py.component_call_executor import ComponentCallExecutor
from rpp_py.logger import RppLogger

from rpp_py.capnp_runtime import CapnpRuntime
from rpp_py.plugin_runtime import PluginRuntimeServer


class RppServerHost:
    def __init__(self, host: str, port: int, runtime: CapnpRuntime = None,
                 logger: RppLogger = None):
        self._host = host
        self._port = port
        self.adapters = []
        self._shutdown_promise = None
        self._runtime = runtime
        self._logger = logger or RppLogger("rpp_server_host")
        self._executor = None

    def add_server(self, server):
        self.adapters.append(server)
        self._logger.debug("Registered runtime adapter: "
                           f"{server.get_info_adapter_server__().connection_name!r}")


    def run(self):
        executor = self._start_executor()
        try:
            return executor.call(self._run())
        finally:
            self._stop_executor()

    async def run_async(self):
        executor = self._start_executor()
        try:
            await asyncio.wrap_future(executor.submit(self._run()))
        finally:
            self._stop_executor()

    def _start_executor(self) -> ComponentCallExecutor:
        if self._executor is None:
            self._logger.debug("Starting runtime host executor.")
            self._executor = ComponentCallExecutor()
            self._executor.start()
        return self._executor

    def _stop_executor(self) -> None:
        if self._executor is None:
            return
        self._logger.debug("Stopping runtime host executor.")
        self._executor.stop()
        self._executor = None

    async def _run(self):
        if self._runtime is None:
            self._runtime = CapnpRuntime()
        plugin_runtime_server = PluginRuntimeServer(
            adapters=self.adapters, logger=self._logger)
        self._shutdown_promise = asyncio.get_running_loop().create_future()
        plugin_runtime_server.set_on_shutdown_callback(
            self.on_shutdown_callback_for_server)

        try:
            await self._runtime.start()
            await plugin_runtime_server.start(
                runtime=self._runtime, host=self._host, port=self._port)
            self._logger.info(
                f"Runtime host listening: host={self._host!r}, port={self._port}")
            await self._shutdown_promise
        finally:
            self._logger.info("Stopping runtime host.")
            for adapter in self.adapters:
                await adapter.stop_adapter_server__()
            await plugin_runtime_server.stop()
            await self._runtime.stop()
            self._shutdown_promise = None
            self._logger.info("Runtime host stopped.")


    def on_shutdown_callback_for_server(self):
        if self._shutdown_promise and not self._shutdown_promise.done():
            self._logger.info("Runtime host shutdown requested by client disconnect.")
            self._shutdown_promise.set_result(True)
