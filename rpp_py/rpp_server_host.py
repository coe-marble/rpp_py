import asyncio

from rpp_py.capnp_runtime import CapnpRuntime
from rpp_py.plugin_runtime import PluginRuntimeServer


class RppServerHost:
    def __init__(self, host: str, runtime_port: int, runtime: CapnpRuntime = None):
        self._host = host
        self._runtime_port = runtime_port
        self.adapters = []
        self._shutdown_promise = None
        self._runtime = runtime

    def add_server(self, server):
        self.adapters.append(server)


    def run(self):
        if self._runtime is None:
            self._runtime = CapnpRuntime()
        return asyncio.run(self._run())

    async def run_async(self):
        if self._runtime is None:
            self._runtime = CapnpRuntime()
        await self._run()

    async def _run(self):
        await self._runtime.start()
        self._shutdown_promise = asyncio.get_running_loop().create_future()

        print(f"Starting RPP Server Host on {self._host}:{self._runtime_port} with {len(self.adapters)} adapters...")

        plugin_runtime_server = PluginRuntimeServer(
            host=self._host, port=self._runtime_port, adapters=self.adapters
        )
        plugin_runtime_server.set_on_shutdown_callback(
            self.on_shutdown_callback_for_server)
        await plugin_runtime_server.start(runtime=self._runtime)

        for adapter in self.adapters:
            await adapter.start_adapter_server__(runtime=self._runtime)
        await self._shutdown_promise

        print("RPP Server Host stopped.")
        for adapter in self.adapters:
            await adapter.stop_adapter_server__()

        await plugin_runtime_server.stop()


    def on_shutdown_callback_for_server(self):
        print("Shutdown callback called for RPP Server Host.")
        # This method is called when the user calls the shutdown method on the PluginRuntimeServer.
        # It should resolve the promise that was returned by the start method of the PluginRuntimeServer.
        if self._shutdown_promise and not self._shutdown_promise.done():
            self._shutdown_promise.set_result(True)