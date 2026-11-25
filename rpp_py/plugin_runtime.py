from pathlib import Path
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable
from rpp_py.capnp_runtime import CapnpRuntime
from rpp_py.logger import RppLogger
import rpp_common
import capnp

from rpp_py.rpp_runtime_client_context import RppRuntimeClientContext
from rpp_py.lifecycle import CallbackResult, LifecycleError, LifecycleState


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

@dataclass
class HostedSlot:
    """One host-owned slot exposed through one runtime connection."""

    adapter: object
    manager: object
    load: Callable[[str, str], CallbackResult]
    refresh_adapter: Callable[[], CallbackResult]
    allows_dynamic_operations: bool = True


class PluginRuntimeServer:

    def __init__(self, adapters=None, logger: RppLogger = None, *, slots=None):
        self._rpc_server = None
        self._runtime = None
        self._runtime_server = None
        self._asyncio_server = None
        self.is_running = False
        self.adapters = {}
        self.slots = dict(slots or {})
        for connection_name, slot in self.slots.items():
            self.adapters[connection_name] = slot.adapter
        for adapter in adapters or []:
            self.adapters[adapter.get_info_adapter_server__().connection_name] = adapter
        self._server_class = None
        self._on_shutdown_callback = None
        self._logger = logger or RppLogger("rpp_plugin_runtime_server")

    def set_on_shutdown_callback(self, callback):
        self._on_shutdown_callback = callback

    def register_slot(self, connection_name: str, slot: HostedSlot):
        if not connection_name:
            raise ValueError("Hosted slot connection name is required")
        if connection_name in self.slots:
            raise ValueError(f"Duplicate hosted slot connection: {connection_name}")
        if not all((slot.adapter, slot.manager, slot.load, slot.refresh_adapter)):
            raise ValueError(f"Hosted slot control is incomplete: {connection_name}")
        self.slots[connection_name] = slot
        self.adapters[connection_name] = slot.adapter

    def _entry_for_instance(self, instance_id):
        for connection_name, slot in self.slots.items():
            for status in slot.manager.list():
                if status.instance_id == instance_id:
                    return connection_name, slot, status.name
        return None

    def _slot(self, connection_name):
        return self.slots.get(connection_name)

    @staticmethod
    def _dynamic_operations_disabled():
        return CallbackResult.failure(
            "This hosted slot is lifecycle-only; load, unload, and reload "
            "are available only for dynamic top-level components")

    @staticmethod
    def _decode_lifecycle_state(adapter, schema_id, payload):
        codec = getattr(adapter, "decode_lifecycle_state__", None)
        if codec is None:
            return (CallbackResult() if not schema_id else
                    CallbackResult.failure("This adapter does not accept lifecycle state"), None)
        return codec(schema_id, payload)

    def list_components(self):
        components = []
        for connection_name, slot in self.slots.items():
            info = slot.adapter.get_info_adapter_server__()
            for status in slot.manager.list():
                components.append({
                    "connection_name": connection_name,
                    "name": status.name,
                    "instance_id": status.instance_id,
                    "plugin_name": info.plugin_name,
                    "plugin_type": info.plugin_type,
                    "state": status.state.value.lower(),
                })
        return components

    @staticmethod
    def _invoke(callback):
        try:
            result = callback()
            if not isinstance(result, CallbackResult):
                return CallbackResult.failure("Lifecycle operation must return CallbackResult")
            return result
        except Exception as error:
            return CallbackResult.failure(str(error))

    def cleanup_components(self):
        first_error = CallbackResult()
        for connection_name, slot in reversed(tuple(self.slots.items())):
            result = slot.manager.close()
            if not result.success:
                self._logger.error(f"Hosted slot {connection_name!r} cleanup failed: {result.message}")
                if first_error.success:
                    first_error = result
        return first_error

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
        obj.owner = self
        return obj

    def _create_server_class(self):
        interface = RuntimeConstants.get_capnp_schema().PluginRuntime
        msg_type = RuntimeConstants.get_capnp_schema().AdapterInfo

        async def ping(self, **kwargs):
            self.logger.debug("Runtime server received ping.")
            return

        async def getProtocolVersion(self, _context, **kwargs):
            _context.results.version = RuntimeConstants.get_capnp_schema().protocolVersion

        def result_into(output, result):
            output.success = result.success
            output.message = result.message

        def resolve_instance(owner, instance_id, seen, expected=None):
            instance_id = str(instance_id)
            if not instance_id:
                raise LifecycleError("Component instance ID is required")
            if instance_id in seen:
                raise LifecycleError(f"Repeated component instance: {instance_id}")
            seen.add(instance_id)
            entry = owner._entry_for_instance(instance_id)
            if entry is None:
                raise LifecycleError(f"Unknown hosted component instance: {instance_id}")
            if expected is not None:
                status = entry[1].manager.get_info(entry[2])
                if status.transition or status.state is not expected:
                    raise LifecycleError(f"Instance {instance_id!r} must be {expected.value.lower()}")
            return instance_id, entry[1], entry[2]

        def apply_instances(instances, operate):
            for instance_id, slot, name in instances:
                result = operate(slot, name)
                if not result.success:
                    if len(instances) > 1:
                        return CallbackResult.failure(f"Instance {instance_id!r}: {result.message}")
                    return result
            return CallbackResult()

        def operate_on_instances(owner, instance_ids, operate):
            seen = set()
            instances = [resolve_instance(owner, key, seen) for key in instance_ids]
            return apply_instances(instances, operate)

        def prepare_activations(owner, instance_ids, handover_states, seen, expected=None):
            if handover_states and len(handover_states) != len(instance_ids):
                raise LifecycleError("Handover states must follow activation IDs, one entry per ID")
            instances = [resolve_instance(owner, key, seen, expected) for key in instance_ids]
            activations = []
            for index, (instance_id, slot, name) in enumerate(instances):
                handover = None
                if handover_states:
                    wire = handover_states[index]
                    decoded, handover = owner._decode_lifecycle_state(
                        slot.adapter, wire.schemaId, wire.payload)
                    decoded.require_success(f"State for {instance_id!r}")
                activations.append((instance_id, slot, name, deepcopy(handover)))
            return activations

        def activate_all(activations):
            for instance_id, slot, name, handover in activations:
                result = slot.manager.activate(name, handover)
                if not result.success:
                    if len(activations) > 1:
                        return CallbackResult.failure(f"Instance {instance_id!r}: {result.message}")
                    return result
            return CallbackResult()

        async def configureComponent(self, instanceIds, _context, **kwargs):
            result = self.owner._invoke(lambda: operate_on_instances(
                self.owner, instanceIds, lambda slot, name: slot.manager.configure(name)))
            result_into(_context.results.result, result)

        async def cleanupComponent(self, instanceIds, _context, **kwargs):
            result = self.owner._invoke(lambda: operate_on_instances(
                self.owner, instanceIds, lambda slot, name: slot.manager.cleanup(name)))
            result_into(_context.results.result, result)

        async def listComponents(self, _context, **kwargs):
            components = self.owner.list_components()
            output = _context.results.init("components", len(components))
            for index, component in enumerate(components):
                item = output[index]
                item.name = component["name"]
                item.instanceId = component["instance_id"]
                item.pluginName = component["plugin_name"]
                item.pluginType = component["plugin_type"]
                item.state = component["state"]
                item.connectionName = component["connection_name"]

        async def activateComponent(self, instanceIds, handoverStates, _context, **kwargs):
            result = self.owner._invoke(lambda: activate_all(
                prepare_activations(self.owner, instanceIds, handoverStates, set())))
            result_into(_context.results.result, result)

        async def deactivateComponent(self, instanceIds, _context, **kwargs):
            result = self.owner._invoke(lambda: operate_on_instances(
                self.owner, instanceIds, lambda slot, name: slot.manager.deactivate(name)))
            result_into(_context.results.result, result)

        async def loadComponent(self, component, _context, **kwargs):
            connection_name = str(component.connectionName)
            slot = self.owner._slot(connection_name)
            result = CallbackResult.failure(f"Unknown hosted slot connection: {connection_name}")
            if slot:
                if not slot.allows_dynamic_operations:
                    result = self.owner._dynamic_operations_disabled()
                else:
                    result = self.owner._invoke(
                        lambda: slot.load(str(component.componentPath), str(component.pluginName)))
                    if result.success:
                        result = self.owner._invoke(slot.refresh_adapter)
            result_into(_context.results.result, result)

        async def unloadComponent(self, instanceIds, _context, **kwargs):
            def unload(slot, name):
                if not slot.allows_dynamic_operations:
                    return self.owner._dynamic_operations_disabled()
                result = slot.manager.unload(name)
                if result.success and slot.manager.selected_context() is not None:
                    return slot.refresh_adapter()
                return result

            result = self.owner._invoke(lambda: operate_on_instances(self.owner, instanceIds, unload))
            result_into(_context.results.result, result)

        async def reloadComponent(self, instanceIds, handoverStates, _context, **kwargs):
            def reload_all():
                replacements = prepare_activations(self.owner, instanceIds, handoverStates, set())
                for instance_id, slot, name, state in replacements:
                    if not slot.allows_dynamic_operations:
                        return self.owner._dynamic_operations_disabled()
                    result = slot.manager.reload(name, state)
                    if not result.success:
                        return CallbackResult.failure(f"Instance {instance_id!r}: {result.message}")
                    result = slot.refresh_adapter()
                    if not result.success:
                        return result
                return CallbackResult()

            result_into(_context.results.result, self.owner._invoke(reload_all))

        async def switchComponents(self, deactivate, activate, handoverStates, _context, **kwargs):
            def switch():
                seen = set()
                outgoing = [resolve_instance(self.owner, key, seen, LifecycleState.ACTIVE)
                            for key in deactivate]
                incoming = prepare_activations(
                    self.owner, activate, handoverStates, seen, LifecycleState.INACTIVE)
                stopped = apply_instances(outgoing, lambda slot, name: slot.manager.deactivate(name))
                if not stopped.success:
                    return stopped
                started = activate_all(incoming)
                if not started.success:
                    return started
                refreshed_slots = set()
                for _, slot, _, _ in incoming:
                    if id(slot) in refreshed_slots:
                        continue
                    refreshed_slots.add(id(slot))
                    result = slot.refresh_adapter()
                    if not result.success:
                        return result
                return CallbackResult()

            result_into(_context.results.result, self.owner._invoke(switch))

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
            slot = self.owner._slot(str(component_name))
            if slot is not None:
                result = self.owner._invoke(slot.refresh_adapter)
                if not result.success:
                    raise LifecycleError(result.message)
                _context.results.pluginRef = slot.adapter.create_capability_adapter_server__()

        methods = {
                "ping": ping,
                "shutdown": shutdown,
                "listAdapters": listAdapters,
                "getComponentCapability": getComponentCapability,
                "getProtocolVersion": getProtocolVersion,
                "configureComponent": configureComponent,
                "cleanupComponent": cleanupComponent,
                "listComponents": listComponents,
                "activateComponent": activateComponent,
                "deactivateComponent": deactivateComponent,
                "loadComponent": loadComponent,
                "unloadComponent": unloadComponent,
                "reloadComponent": reloadComponent,
                "switchComponents": switchComponents,
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
        self._component_ids_by_connection = {}

    async def connect(self, context: RppRuntimeClientContext):
        self._logger.debug("Connecting runtime client.")
        self._context = context
        self._runtime = context.runtime
        client_class = RuntimeConstants.get_capnp_schema().PluginRuntime
        self._client = self._context.get_client().cast_as(client_class)
        version = await self._client.getProtocolVersion()
        if version.version != RuntimeConstants.get_capnp_schema().protocolVersion:
            await self.disconnect()
            raise LifecycleError("Incompatible component runtime protocol version")
        self._logger.debug("Runtime client connected.")

    async def disconnect(self):
        self._logger.debug("Disconnecting runtime client.")
        self._client = None
        self._context = None
        self._runtime = None
        self._component_ids_by_connection.clear()
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

    async def list_components(self, connection_name: str | None = None):
        response = await self._client.listComponents()
        components = list(response.components)
        if connection_name is None:
            return components
        return [component for component in components
                if str(component.connectionName) == connection_name]

    async def _component_id(self, connection_name: str) -> str:
        if connection_name in self._component_ids_by_connection:
            return self._component_ids_by_connection[connection_name]
        components = [component for component in await self.list_components(connection_name)
                      if component.instanceId]
        if len(components) != 1 or not components[0].instanceId:
            raise LifecycleError(
                "Remote lifecycle-by-connection requires exactly one loaded component "
                f"in slot: {connection_name}")
        instance_id = str(components[0].instanceId)
        self._component_ids_by_connection[connection_name] = instance_id
        return instance_id

    async def configure_component(self, connection_name: str) -> CallbackResult:
        return await self.configure_instance(await self._component_id(connection_name))

    async def configure_instance(self, instance_id: str) -> CallbackResult:
        return await self.configure_instances([instance_id])

    async def configure_instances(self, instance_ids: list[str]) -> CallbackResult:
        response = await self._client.configureComponent(instanceIds=list(instance_ids))
        return CallbackResult(response.result.success, str(response.result.message))

    async def cleanup_component(self, connection_name: str) -> CallbackResult:
        return await self.cleanup_instance(await self._component_id(connection_name))

    async def cleanup_instance(self, instance_id: str) -> CallbackResult:
        return await self.cleanup_instances([instance_id])

    async def cleanup_instances(self, instance_ids: list[str]) -> CallbackResult:
        response = await self._client.cleanupComponent(instanceIds=list(instance_ids))
        return CallbackResult(response.result.success, str(response.result.message))

    async def activate_component(self, connection_name: str, state=None, *, adapter) -> CallbackResult:
        return await self.activate_instance(await self._component_id(connection_name), state, adapter=adapter)

    async def activate_instance(self, instance_id: str, state=None, *, adapter) -> CallbackResult:
        request = self._client.activateComponent_request()
        request.instanceIds = [instance_id]
        wire = request.init("handoverStates", 1)[0]
        encoded = adapter.encode_lifecycle_state__(state, wire)
        if not encoded.success:
            return encoded
        response = await request.send()
        return CallbackResult(response.result.success, str(response.result.message))

    async def activate_instances(self, instance_ids: list[str], handover_states=None) -> CallbackResult:
        """Forward encoded state envelopes in instance-ID order."""
        response = await self._client.activateComponent(
            instanceIds=list(instance_ids), handoverStates=handover_states or [])
        return CallbackResult(response.result.success, str(response.result.message))

    async def switch_components(self, deactivate: list[str], activate: list[str],
                                handover_states=None) -> CallbackResult:
        """Forward encoded state envelopes in activation-ID order."""
        response = await self._client.switchComponents(
            deactivate=deactivate, activate=activate, handoverStates=handover_states or [])
        return CallbackResult(response.result.success, str(response.result.message))

    async def deactivate_component(self, connection_name: str) -> CallbackResult:
        return await self.deactivate_instance(await self._component_id(connection_name))

    async def deactivate_instance(self, instance_id: str) -> CallbackResult:
        return await self.deactivate_instances([instance_id])

    async def deactivate_instances(self, instance_ids: list[str]) -> CallbackResult:
        response = await self._client.deactivateComponent(instanceIds=list(instance_ids))
        return CallbackResult(response.result.success, str(response.result.message))

    async def load_component(self, component_path: str, plugin_name: str,
                             connection_name: str) -> CallbackResult:
        response = await self._client.loadComponent(component={
            "componentPath": component_path,
            "pluginName": plugin_name,
            "connectionName": connection_name,
        })
        self._component_ids_by_connection.pop(connection_name, None)
        return CallbackResult(response.result.success, str(response.result.message))

    async def unload_component(self, connection_name: str) -> CallbackResult:
        result = await self.unload_instance(await self._component_id(connection_name))
        self._component_ids_by_connection.pop(connection_name, None)
        return result

    async def unload_instance(self, instance_id: str) -> CallbackResult:
        return await self.unload_instances([instance_id])

    async def unload_instances(self, instance_ids: list[str]) -> CallbackResult:
        # A failed batch may already have unloaded earlier entries.
        self._component_ids_by_connection.clear()
        response = await self._client.unloadComponent(instanceIds=list(instance_ids))
        return CallbackResult(response.result.success, str(response.result.message))

    async def reload_component(self, connection_name: str, handover_states=None) -> CallbackResult:
        result = await self.reload_instance(
            await self._component_id(connection_name), handover_states)
        self._component_ids_by_connection.pop(connection_name, None)
        return result

    async def reload_instance(self, instance_id: str, handover_states=None) -> CallbackResult:
        return await self.reload_instances([instance_id], handover_states)

    async def reload_instances(self, instance_ids: list[str], handover_states=None) -> CallbackResult:
        self._component_ids_by_connection.clear()
        response = await self._client.reloadComponent(
            instanceIds=list(instance_ids), handoverStates=handover_states or [])
        return CallbackResult(response.result.success, str(response.result.message))
