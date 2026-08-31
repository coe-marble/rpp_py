from pathlib import Path
from rpp_py.capnp_runtime import CapnpRuntime
from rpp_py.logger import RppLogger
import rpp_common
import capnp

from rpp_py.rpp_runtime_client_context import RppRuntimeClientContext


class RuntimeConstants:

    if getattr(rpp_common, "__file__", None) is not None:
        rpp_common_path = Path(rpp_common.__file__).parent
    else:
        # __path__ je lista staza koje paket koristi; uzimamo prvu
        rpp_common_path = Path(list(rpp_common.__path__)[0]) / "rpp_common"
    RUNTIME_CAPNP_FILE = rpp_common_path / "plugin_runtime.capnp"
    CAPNP_SCHEMA = None

    @staticmethod
    def get_capnp_schema():
        if RuntimeConstants.CAPNP_SCHEMA is None:
            path = RuntimeConstants.RUNTIME_CAPNP_FILE
            if not path.exists():
                raise FileNotFoundError(f"Cap'n Proto schema file not found: {path}")
            standard_imports = ["/usr/local/include", "/usr/include"]
            RuntimeConstants.CAPNP_SCHEMA = capnp.load(
                    str(RuntimeConstants.RUNTIME_CAPNP_FILE), imports=standard_imports)
        return RuntimeConstants.CAPNP_SCHEMA

class PluginRuntimeServer:

    def __init__(self, adapters=None, logger: RppLogger = None):
        self._rpc_server = None
        self._runtime = None
        self._runtime_server = None
        self._asyncio_server = None
        self.is_running = False
        self.adapters = {}
        for adapter in adapters or []:
            self.adapters[adapter.get_info_adapter_server__().connection_name] = adapter
        self._server_class = None
        self._on_shutdown_callback = None
        self._logger = logger or RppLogger("rpp_plugin_runtime_server")

    def set_on_shutdown_callback(self, callback):
        self._on_shutdown_callback = callback

    async def _handle_connection(self, stream):
        self._logger.debug("Runtime client connected.")
        if self._server_class is None:
            self._server_class = self._create_server_class()

        runtime_server = self._init_obj_of_server_class(self._server_class())
        rpc_server = capnp.TwoPartyServer(stream, bootstrap=runtime_server)
        self._runtime_server = runtime_server
        self._rpc_server = rpc_server
        await rpc_server.on_disconnect()
        self._logger.debug("Runtime client disconnected.")

        if (runtime_server.shutdown_requested
                and runtime_server.on_shutdown_callback):
            self._logger.debug(
                "Shutdown-requesting runtime client disconnected; notifying host.")
            runtime_server.on_shutdown_callback()

    async def start(self, runtime: CapnpRuntime, host="localhost", port=0):
        if port == 0:
            raise ValueError("Port must be specified and non-zero.")
        self._runtime = runtime

        self._logger.debug(f"Starting runtime RPC server on {host}:{port}.")
        self._asyncio_server = await capnp.AsyncIoStream.create_server( \
                self._handle_connection, host, port)
        self.is_running = True


    async def stop(self):

        if self._asyncio_server:
            self._logger.debug("Stopping runtime RPC server.")
            self._asyncio_server.close()
            await self._asyncio_server.wait_closed()
        self._asyncio_server = None
        self._rpc_server = None
        self._runtime = None
        self.is_running = False


    def _init_obj_of_server_class(self, obj):
        obj.on_shutdown_callback = self._on_shutdown_callback
        obj.logger = self._logger
        obj.shutdown_requested = False
        obj.adapters = self.adapters
        return obj

    def _create_server_class(self):
        interface = RuntimeConstants.get_capnp_schema().PluginRuntime
        msg_type = RuntimeConstants.get_capnp_schema().AdapterInfo

        async def ping(self, **kwargs):
            self.logger.debug("Runtime server received ping.")
            return

        async def listAdapters(self, _context, **kwargs):
            self.logger.debug("Runtime server received listAdapters.")

            msg = _context.results
            adapters_list = msg.init("adapters", len(self.adapters))
            for i, adapter in enumerate(self.adapters.values()):
                info = adapter.get_info_adapter_server__()
                adapter_info = adapters_list[i]
                adapter_info.name = info.name
                adapter_info.pluginName = info.plugin_name
                adapter_info.pluginType = info.plugin_type
                adapter_info.createdAt = 12345

        async def shutdown(self, **kwargs):
            self.logger.debug("Runtime server received shutdown request.")
            self.shutdown_requested = True


        async def getComponentCapability(self, _context, **kwargs):
            component_name = kwargs.get("name")
            self.logger.debug(
                f"Runtime server received getComponentCapability: {component_name!r}.")
            if component_name in self.adapters:
                adapter = self.adapters[component_name]
                _context.results.pluginRef = adapter

        methods = {
                "ping": ping,
                "shutdown": shutdown,
                "listAdapters": listAdapters,
                "getComponentCapability": getComponentCapability
            }

        return type(
            "PluginRuntimeServer",
            (interface.Server,),
            methods
        )


class PluginRuntimeClient:
    def __init__(self, logger: RppLogger = None):
        self._runtime = None
        self._context = None
        self._client = None
        self._logger = logger or RppLogger("rpp_plugin_runtime_client")

    async def connect(self, context: RppRuntimeClientContext):
        self._logger.debug("Connecting runtime client.")
        self._context = context
        self._runtime = context.runtime
        client_class = RuntimeConstants.get_capnp_schema().PluginRuntime
        self._client = self._context.get_client().cast_as(client_class)
        self._logger.debug("Runtime client connected.")

    async def disconnect(self):
        self._logger.debug("Disconnecting runtime client.")
        self._client = None
        self._context = None
        self._runtime = None
        self._logger.debug("Runtime client disconnected.")


    async def ping(self):
        self._logger.debug("Runtime client calling ping.")
        return await self._client.ping()

    async def listAdapters(self):
        self._logger.debug("Runtime client calling listAdapters.")
        response = await self._client.listAdapters()
        return [adapter for adapter in response.adapters]

    async def shutdown(self):
        self._logger.debug("Runtime client calling shutdown.")
        return await self._client.shutdown()

    async def getComponentCapability(self, component_name: str):
        self._logger.debug(
            f"Runtime client calling getComponentCapability: {component_name!r}.")
        response = await self._client.getComponentCapability(name=component_name)
        return response.pluginRef
