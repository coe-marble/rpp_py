from pathlib import Path
from rpp_py.capnp_runtime import CapnpRuntime
import rpp_common
import capnp

from rpp_py.client_context import ClientContext


class RuntimeConstants:
    rpp_common_path = Path(rpp_common.__file__).parent
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

    def __init__(self, adapters = None):
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

    def set_on_shutdown_callback(self, callback):
        self._on_shutdown_callback = callback

    async def _handle_connection(self, stream):
        if self._server_class is None:
            self._server_class = self._create_server_class()

        self._runtime_server = self._init_obj_of_server_class(self._server_class())
        self._rpc_server = capnp.TwoPartyServer(stream, bootstrap=self._runtime_server)
        await self._rpc_server.on_disconnect()

    async def start(self, runtime: CapnpRuntime, host="localhost", port=0):
        if port == 0:
            raise ValueError("Port must be specified and non-zero.")
        self._runtime = runtime

        self._asyncio_server = await capnp.AsyncIoStream.create_server( \
                self._handle_connection, host, port)
        self.is_running = True


    async def stop(self):

        if self._asyncio_server:
            self._asyncio_server.close()
            await self._asyncio_server.wait_closed()
        self._asyncio_server = None
        self._rpc_server = None
        self._runtime = None
        self.is_running = False


    def _init_obj_of_server_class(self, obj):
        obj.on_shutdown_callback = self._on_shutdown_callback
        obj.adapters = self.adapters
        return obj

    def _create_server_class(self):
        interface = RuntimeConstants.get_capnp_schema().PluginRuntime
        msg_type = RuntimeConstants.get_capnp_schema().AdapterInfo

        async def ping(self, **kwargs):
            return

        async def listAdapters(self, _context, **kwargs):

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
            if self.on_shutdown_callback:
                self.on_shutdown_callback()


        async def getComponentCapability(self, _context, **kwargs):
            component_name = kwargs.get("name")
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
    def __init__(self):
        self._runtime = None
        self._context = None
        self._client = None

    async def connect(self, context: ClientContext):
        self._context = context
        self._runtime = context.runtime
        client_class = RuntimeConstants.get_capnp_schema().PluginRuntime
        self._client = self._context.get_client().cast_as(client_class)

    async def disconnect(self):
        self._client = None
        self._context = None
        self._runtime = None


    async def ping(self):
        return await self._client.ping()

    async def listAdapters(self):
        response = await self._client.listAdapters()
        return [adapter for adapter in response.adapters]

    async def shutdown(self):
        return await self._client.shutdown()

    async def getComponentCapability(self, component_name: str):
        response = await self._client.getComponentCapability(name=component_name)
        return response.pluginRef