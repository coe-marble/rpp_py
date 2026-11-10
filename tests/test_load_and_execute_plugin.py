import asyncio
from pathlib import Path
import sys
import threading
import time
import pytest

import json
from unittest.mock import Mock
from rpp_orchestrator.workspace import Workspace

import rpp_plugin_registrator.registry_config as rp
from rpp_py.clock import ClockOptions
from rpp_py.build_requests import ComponentRequest
from rpp_py.context_builder import ComponentRuntimeBuilder
from rpp_py.data_manager import DataManager

from rpp_py.component_call_executor import ComponentCallExecutor
from rpp_py.component_manager import ComponentManager
from rpp_py.rpp_runtime_client_context import RppRuntimeClientContext
from rpp_py.plugin_runtime import HostedSlot, PluginRuntimeClient, PluginRuntimeServer
from rpp_py.rpp_server_host import RppServerHost
from rpp_py.context import ComponentContext
from rpp_py.lifecycle import CallbackResult
from rpp_py.logger import RppLogger
from rpp_py.plugin_loader import (
    PluginAdapter, PythonPluginLoader
)
from rpp_py.external_component_process import CppExternalComponentProcess
from rpp_py.adapter_info import AdapterServerParams, AdapterClientParams
from rpp_py.capnp_runtime import CapnpRuntime
import subprocess, os
import socket


RPP_TESTING_PATH = Path(__file__).parent.parent.parent.resolve() \
    / "rpp_testing" / "rpp_testing"
EXAMPLES_DATA_PATH = RPP_TESTING_PATH / "data"


def mock_context_with_logger(logger: RppLogger | None = None) -> Mock:
    """Create the minimal context required by direct plugin-instance tests."""
    context = Mock(spec=ComponentContext)
    context.get_logger.return_value = logger or RppLogger("test_plugin")
    context.get_parameter.side_effect = lambda _name, default=None: default
    return context


def create_initialized_plugin_instance(loader, plugin_name: str):
    instance = loader.create_instance(plugin_name)
    instance.configure(mock_context_with_logger())
    return instance


def write_config(home: Path, orig_cfg: dict):
    new_config_path = home / "config.json"
    home.mkdir(parents=True, exist_ok=True)
    with open(new_config_path, "w", encoding="utf-8") as f:
        json.dump(orig_cfg, f, indent=4)


class BaseTestLoadAndExecutePlugin:
    @staticmethod
    def source_ros_workspace(monkeypatch):
        # get current path of the script
        current_path = Path(__file__).parent.resolve()
        rpp_source_path = current_path.parents[4] / "install" / "setup.bash"
        env = subprocess.check_output(["bash", "-c", "source " + str(rpp_source_path) + " && env"], text=True)
        for line in env.splitlines():
            key, _, value = line.partition("=")
            if key:
                monkeypatch.setenv(key, value)

    @staticmethod
    def reset_generated_interface_modules():
        """Prevent one temporary RPP home from leaking generated types to another."""
        for module_name in tuple(sys.modules):
            if module_name == "rpp_plugin_types" or \
                    module_name.startswith("rpp_plugin_types."):
                sys.modules.pop(module_name, None)

    def get_free_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))  # OS allocates a free port
            return s.getsockname()[1]

    @staticmethod
    def _odometry_message(interface, position_x):
        message = interface.Odometry2D()
        message.pose.position.x = position_x
        message.pose.position.y = 2.0
        message.pose.yaw = 0.5
        return message


@pytest.fixture(scope="class")
def native_rpp_runtime(request):
    from rpp_cli.testing import setup_tmp_rpp_with_test_plugins

    monkeypatch = pytest.MonkeyPatch()
    BaseTestLoadAndExecutePlugin.source_ros_workspace(monkeypatch)
    monkeypatch.setenv(
        "RPP_WHITELIST_PLUGIN_TYPES",
        "rpp_testing::MotionController2D;rpp_testing::DisturbanceGenerator2D",
    )
    rpp_handle = setup_tmp_rpp_with_test_plugins(component_whitelist=[
        "example_plugin_simple_py.py",
        "example_plugin_complex_py.py",
    ])
    monkeypatch.setattr(rp, "RPP_HOME", rpp_handle.home)
    request.cls.rpp_handle = rpp_handle
    request.cls.library_manager = rpp_handle.library_manager
    request.cls.ws = Workspace(rpp_handle.home / "workspace", rpp_handle.library_manager)
    request.cls.test_lib = rpp_handle.test_lib
    write_config(rpp_handle.home, rp.get_config())
    try:
        yield
    finally:
        rpp_handle.td.cleanup()
        rp.reset_module()
        BaseTestLoadAndExecutePlugin.reset_generated_interface_modules()
        monkeypatch.undo()


@pytest.mark.usefixtures("native_rpp_runtime")
class TestLoadAndExecutePluginNative(BaseTestLoadAndExecutePlugin):

    def test_native_python_lifecycle_and_method_call_happy_path(self):
        """Native Python tree: explicit child and parent lifecycle, then method access."""
        builder = ComponentRuntimeBuilder(
            data_manager=DataManager(library_manager=self.library_manager),
            clock_options=ClockOptions(), allow_dynamic_components=True)
        runtime = builder.build_component(ComponentRequest(
            EXAMPLES_DATA_PATH / "test_component_py", "test_lib::ComponentPluginComplexPy"))
        context = runtime.get_context()
        controller = context.get_subcomponent_context("ctl_main")

        try:
            assert runtime.configure().success
            assert context.activate_subcomponents().success
            assert runtime.activate().success

            message = runtime.get_instance().Odometry2D()
            message.pose.position.x = 6.0
            # This fixture intentionally leaves validate_subcomponent disabled:
            # the parent rejects while its native child accepts the same request.
            assert not runtime.get_instance().validate(message)
            assert context.get_subcomponent_instance("ctl_main").validate(message)
            assert controller.deactivate().result.success
            assert controller.activate().success

            assert not runtime.get_instance().validate(message)
        finally:
            runtime.deactivate()
            context.deactivate_subcomponents()
            runtime.close().require_success("close native integration runtime")



    def test_load_and_execute_plugin_py(self):
        # Assuming the plugin is located in the 'plugins' directory relative to this test file
        plugins = self.library_manager.get_library_plugins(self.test_lib, source_language="python")
        assert len(plugins) > 0, "No plugins found in the test library."

        test_plugin = plugins.get("test_lib::ComponentPluginSimplePy")
        assert test_plugin is not None, "Test plugin not found."

        # Load the plugin
        loader = PythonPluginLoader(library_manager=self.library_manager, available_plugins=plugins)

        from rpp_plugin_types.rpp_testing import MotionController2D
        instance: MotionController2D = create_initialized_plugin_instance(
            loader, "test_lib::ComponentPluginSimplePy")

        msg = MotionController2D.Odometry2D()
        msg.pose.position.x = 1.0
        msg.pose.position.y = 2.0
        msg.pose.yaw = 0.5

        # except when setting non existig field
        with pytest.raises(Exception) as context:
            msg.non_existing_field = 10
            assert False, "Expected an exception when setting a non-existing field."

        assert "has no attribute 'non_existing_field'" in str(context.value), "Unexpected exception message."

        result = instance.validate(msg)

        assert not (result), "Expected the validation to fail for the test plugin."
        msg.pose.position.x = 6.0
        result = instance.validate(msg)
        assert result, "Expected the validation to pass for the test plugin."

    def _create_local_network_adapter_pair(self, plugin_name):
        plugins = self.library_manager.get_library_plugins(
                self.test_lib, source_language="python")
        plugin_info = self.library_manager.get_plugin_info_from_lib(plugin_name)
        client_info = AdapterClientParams(
                    plugin_name=plugin_info["PluginName"],
                    name="test_name",
                    connection_name="test_connection_name",
                )
        from rpp_plugin_types.rpp_testing import MotionController2D
        client : MotionController2D = PluginAdapter.create_client(
                library_manager=self.library_manager,
                plugin_info=plugin_info,
                client_info=client_info)

        loader = PythonPluginLoader(library_manager=self.library_manager, available_plugins=plugins)
        server_backend: MotionController2D = create_initialized_plugin_instance(
            loader, plugin_name)

        server_info = AdapterServerParams(
            backend=server_backend,
            plugin_name=plugin_info["PluginName"],
            connection_name=client_info.connection_name,
        )
        server = PluginAdapter.create_server(library_manager=self.library_manager,
                plugin_info=plugin_info, server_info=server_info)
        return client, server, MotionController2D

    @staticmethod
    def _start_adapter_server_sync(server, host, port):
        executor = ComponentCallExecutor()
        executor.start()
        runtime = CapnpRuntime()
        executor.call(runtime.start())
        executor.call(server.start_adapter_server__(
            runtime=runtime, host=host, port=port))
        return executor, runtime

    @staticmethod
    def _stop_adapter_server_sync(server, runtime, executor):
        try:
            executor.call(server.stop_adapter_server__())
            executor.call(runtime.stop())
        finally:
            executor.stop()


    def test_load_and_execute_plugin_local_network_sync(self):
        host = "localhost"
        port = self.get_free_port()
        client, server, interface = self._create_local_network_adapter_pair(
            "test_lib::ComponentPluginSimplePy")
        server_executor, server_runtime = self._start_adapter_server_sync(
            server, host, port)
        client_executor = ComponentCallExecutor()
        client_executor.start()
        context = RppRuntimeClientContext(host=host, port=port)
        client_connected = False
        try:
            connected, error = client_executor.call(context.start())
            assert connected, error
            client.connect_adapter_client__(context, executor=client_executor)
            client_connected = True

            assert not (client.validate(self._odometry_message(interface, 1.0)))
            assert client.validate(self._odometry_message(interface, 6.0))
        finally:
            if client_connected:
                client.disconnect_adapter_client__()
            client_executor.call(context.stop())
            client_executor.stop()
            self._stop_adapter_server_sync(
                server, server_runtime, server_executor)

    def test_load_and_execute_plugin_local_network_sync_owned_executor(self):
        host = "localhost"
        port = self.get_free_port()
        client, server, interface = self._create_local_network_adapter_pair(
            "test_lib::ComponentPluginSimplePy")
        server_executor, server_runtime = self._start_adapter_server_sync(
            server, host, port)
        client_connected = False
        try:
            client.connect_adapter_client__(
                RppRuntimeClientContext(host=host, port=port))
            client_connected = True

            assert not (client.validate(self._odometry_message(interface, 1.0)))
            assert client.validate(self._odometry_message(interface, 6.0))
        finally:
            if client_connected:
                client.disconnect_adapter_client__()
            self._stop_adapter_server_sync(
                server, server_runtime, server_executor)

    def test_load_and_execute_plugin_local_network_async(self):
        host = "localhost"
        port = self.get_free_port()
        client, server, interface = self._create_local_network_adapter_pair(
            "test_lib::ComponentPluginSimplePy")
        server_executor, server_runtime = self._start_adapter_server_sync(
            server, host, port)
        try:
            async def validate_messages():
                await client.connect_adapter_client_async(
                    RppRuntimeClientContext(host=host, port=port))
                try:
                    assert client._component_call_executor is None
                    assert not (await client.validate_async(
                        self._odometry_message(interface, 1.0)))
                    assert await client.validate_async(
                        self._odometry_message(interface, 6.0))
                    with pytest.raises(RuntimeError, match="validate_async"):
                        client.validate(self._odometry_message(interface, 6.0))
                finally:
                    await client.disconnect_adapter_client_async()

            asyncio.run(validate_messages())
        finally:
            self._stop_adapter_server_sync(
                server, server_runtime, server_executor)



    def test_plugin_runtime(self):
        host = "localhost"
        port = self.get_free_port()
        plugins = self.library_manager.get_library_plugins(
                self.test_lib, source_language="python")
        plugin_info = self.library_manager.get_plugin_info_from_lib(
            "test_lib::ComponentPluginSimplePy")
        loader = PythonPluginLoader(
            library_manager=self.library_manager, available_plugins=plugins)
        server_backend = loader.create_instance("test_lib::ComponentPluginSimplePy")
        server_context = ComponentContext(instance=server_backend)
        server_info = AdapterServerParams(
            plugin_name=plugin_info["PluginName"], backend=server_backend,
            name="test_server", connection_name="test_connection")
        server = PluginAdapter.create_server(
            library_manager=self.library_manager,
            plugin_info=plugin_info,
            server_info=server_info)
        manager = ComponentManager()
        manager.adopt("test_connection", server_context, initial=True).require_success()
        plugin_runtime_server = PluginRuntimeServer(
            adapters=[server], slots={"test_connection": HostedSlot(
                server, manager, lambda *_: CallbackResult.failure("no test loader"), CallbackResult)})
        plugin_runtime_client = PluginRuntimeClient()
        plugin_client = PluginAdapter.create_client(
            library_manager=self.library_manager,
            plugin_info=plugin_info,
            client_info=AdapterClientParams(
                plugin_name=plugin_info["PluginName"],
                name="test_client",
                connection_name="test_connection"))

        server_executor = ComponentCallExecutor()
        client_executor = ComponentCallExecutor()
        server_runtime = CapnpRuntime()
        client_context = RppRuntimeClientContext(host=host, port=port)
        client_connected = False
        try:
            server_executor.start()
            server_executor.call(server_runtime.start())
            server_executor.call(plugin_runtime_server.start(
                runtime=server_runtime, host=host, port=port))

            client_executor.start()
            connected, error = client_executor.call(client_context.start())
            assert connected, error
            client_executor.call(plugin_runtime_client.connect(client_context))
            plugin_client.connect_adapter_client__(
                client_context, executor=client_executor)
            client_connected = True

            result = client_executor.call(plugin_runtime_client.configure_component("test_connection"))
            assert result.success, result.message
            assert server_backend._context is server_context
            missing = client_executor.call(plugin_runtime_client.configure_instance("missing"))
            assert not missing.success
            plugin_msg = self._odometry_message(plugin_client, 1.0)
            assert not (plugin_client.validate(plugin_msg))

            client_executor.call(plugin_runtime_client.ping())
            adapters = client_executor.call(plugin_runtime_client.listAdapters())
            assert isinstance(adapters, list), "Expected a list of adapters."
            result = client_executor.call(plugin_runtime_client.cleanup_component("test_connection"))
            assert result.success, result.message
            client_executor.call(plugin_runtime_client.shutdown())
        finally:
            if client_connected:
                plugin_client.disconnect_adapter_client__()
            client_executor.call(plugin_runtime_client.disconnect())
            client_executor.call(client_context.stop())
            client_executor.stop()
            server_executor.call(plugin_runtime_server.stop())
            server_executor.call(server_runtime.stop())
            server_executor.stop()


    def test_rpp_server_host_with_owned_client_executor(self):
        host_name = "localhost"
        port = self.get_free_port()
        client, server, interface = self._create_local_network_adapter_pair(
            "test_lib::ComponentPluginSimplePy")
        server_host = RppServerHost(host=host_name, port=port)
        manager = ComponentManager()
        manager.adopt("test_connection", ComponentContext(instance=server._backend),
                      initial=True).require_success()
        server_host.add_slot(server, manager, lambda *_: CallbackResult.failure("no test loader"))
        server_error = []

        def run_server_host():
            try:
                server_host.run()
            except BaseException as error:
                server_error.append(error)

        server_thread = threading.Thread(target=run_server_host)
        server_thread.start()
        while server_host._executor is None and server_thread.is_alive():
            time.sleep(0.01)

        assert server_host._executor is not None
        assert not (server_error)

        client_connected = False
        runtime_client = PluginRuntimeClient()
        try:
            client.connect_adapter_client__(
                RppRuntimeClientContext(host=host_name, port=port))
            client_connected = True
            assert client._owned_component_call_executor is not None
            assert not (client.validate(self._odometry_message(interface, 1.0)))
            assert client.validate(self._odometry_message(interface, 6.0))
            # test that the runtime client can connect to the runtime server and shutdown it
            client._component_call_executor.call(
                runtime_client.connect(client._runtime_client_context))
            client._component_call_executor.call(runtime_client.shutdown())
        finally:
            if client_connected:
                client.disconnect_adapter_client__()
            server_thread.join(timeout=2)

        assert not (server_thread.is_alive())
        assert not (server_error)

    def test_complex_instance_plugin_using_context_builder(self):

        component_path = EXAMPLES_DATA_PATH / "test_component_py"
        data_manager = DataManager(library_manager=self.library_manager)
        context_builder = ComponentRuntimeBuilder(
            data_manager=data_manager, clock_options=ClockOptions())
        component_runtime = context_builder.build_component_from_path(
            str(component_path))
        component_context = component_runtime.get_context()

        assert component_context is not None, "Component context should not be None."
        assert component_context.list_subcomponents() == ["ctl_main"]
        assert hasattr(component_context, '_clock'), "Component context should have clock options."


        params = component_context._params

        class TestClass:
            def __init__(self, a=0, b=0):
                self.a = a
                self.b = b

        assert params.get("param1") == 1, "param1 should be 1"
        assert params.get("param2") == 2, "param2 should be 2"
        assert params.get("param3") == "set_string", "param3 should be 'set_string'"
        assert params.get("param7") == {"a": 10, "b": 20}, "param7 should be an instance of SuperClass with a=10 and b=20"
        assert params.get("param4") == True, "param4 should be True"
        assert params.get("param5") == [1, 2, 3], "param5 should be [1, 2, 3]"
        assert params.get("param6") == {"key1": "value1", "key2": 2}, "param6 should be {'key1': 'value1', 'key2': 2}"

        res = params.get_as("param7", TestClass)
        assert isinstance(res, TestClass), "param7 should be an instance of TestClass"
        assert res.a == 10, "param7.a should be 10"
        assert res.b == 20, "param7.b should be 20"
        instance = component_runtime.get_instance()
        assert instance is not None, "Instance should not be None."

        component1 = component_context.get_subcomponent_instance("ctl_main")
        assert component1 is not None, "Component 'ctl_main' should not be None."

        with pytest.raises(RuntimeError):
            component_context.get_subcomponent_instance("non_existing_component")

        component_runtime.close().require_success("close complex runtime")




@pytest.fixture(scope="class")
def adapter_rpp_runtime(request):
    from rpp_cli.testing import setup_tmp_rpp_with_test_plugins

    monkeypatch = pytest.MonkeyPatch()
    BaseTestLoadAndExecutePlugin.source_ros_workspace(monkeypatch)
    monkeypatch.setenv(
        "RPP_WHITELIST_PLUGIN_TYPES",
        "rpp_testing::MotionController2D;rpp_testing::DisturbanceGenerator2D",
    )
    rpp_handle = setup_tmp_rpp_with_test_plugins(component_whitelist=[
        "example_plugin_simple_cpp.cpp",
        "example_plugin_simple_py.py",
        "example_plugin_complex_cpp.cpp",
        "example_plugin_complex_py.py",
    ])
    monkeypatch.setattr(rp, "RPP_HOME", rpp_handle.home)
    request.cls.rpp_handle = rpp_handle
    request.cls.library_manager = rpp_handle.library_manager
    request.cls.ws = Workspace(rpp_handle.home / "workspace", rpp_handle.library_manager)
    request.cls.component_record = request.cls.ws.create_component(
        "ComponentPluginInstance", "test_lib::ComponentPluginSimpleCpp"
    )
    request.cls.test_lib = rpp_handle.test_lib
    write_config(rpp_handle.home, rp.get_config())
    try:
        yield
    finally:
        rpp_handle.td.cleanup()
        rp.reset_module()
        BaseTestLoadAndExecutePlugin.reset_generated_interface_modules()
        monkeypatch.undo()


@pytest.mark.usefixtures("adapter_rpp_runtime")
class TestLoadAndExecutePluginAdapter(BaseTestLoadAndExecutePlugin):

    def test_load_and_execute_plugin_local_network_with_cpp(self):
        # Assuming the plugin is located in the 'plugins' directory relative to this test file
        plugins = self.library_manager.get_library_plugins(
                self.test_lib, source_language="cpp")
        assert len(plugins) > 0, "No plugins found in the test library."

        plugin_info = self.library_manager \
            .get_plugin_info_from_lib("test_lib::ComponentPluginSimpleCpp")
        assert plugin_info is not None, "Test plugin not found."

        host = "localhost"
        port = self.get_free_port()
        client_info = AdapterClientParams(
            plugin_name=plugin_info["PluginName"],
            name="test_client",
            connection_name="test_connection"
        )

        from rpp_plugin_types.rpp_testing import MotionController2D

        # Load the plugin
        client : MotionController2D = PluginAdapter.create_client(
                library_manager=self.library_manager,
                plugin_info=plugin_info,
                client_info=client_info)

        component = self.component_record
        command = [CppExternalComponentProcess.resolve_command(),
             "--host", host, "--port", str(port), \
             "--plugin", "test_lib::ComponentPluginSimpleCpp", '--home', str(self.rpp_handle.home), \
             "--path", str(component.folder),
             "--conn", "test_connection",
             "--slot", "rpp_testing::MotionController2D"]
        server_p = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        client_executor = ComponentCallExecutor()
        client_executor.start()
        context = RppRuntimeClientContext(host=host, port=port)
        runtime_client = PluginRuntimeClient()
        client_connected = False
        try:
            connected, error = client_executor.call(context.start(1000))
            assert connected, error
            client.connect_adapter_client__(context, executor=client_executor)
            client_connected = True
            client_executor.call(runtime_client.connect(context))

            assert not (client.validate(
                self._odometry_message(MotionController2D, 1.0)))
            assert client.validate(
                self._odometry_message(MotionController2D, 6.0))
        finally:
            if client_connected:
                client.disconnect_adapter_client__()
            client_executor.call(runtime_client.disconnect())
            client_executor.call(context.stop())
            client_executor.stop()
            if server_p.poll() is None:
                server_p.terminate()
                try:
                    server_p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    server_p.kill()
                    server_p.wait()

    def test_cpp_hosted_slot_load_switch_reload_and_method_call_happy_path(self):
        """The real C++ host exposes lifecycle and dynamic slot control over RPC."""
        plugin_name = "test_lib::ComponentPluginSimpleCpp"
        plugin_info = self.library_manager.get_plugin_info_from_lib(plugin_name)
        first = self.ws.create_component("remote_first", plugin_name)
        second = self.ws.create_component("remote_second", plugin_name)
        connection_name = "remote_slot"
        host, port = "localhost", self.get_free_port()
        process = subprocess.Popen([
            CppExternalComponentProcess.resolve_command(), "--host", host, "--port", str(port),
            "--plugin", plugin_name, "--home", str(self.rpp_handle.home),
            "--path", str(first.folder), "--conn", connection_name,
            "--slot", "rpp_testing::MotionController2D",
            "--allow-dynamic-components",
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        executor = ComponentCallExecutor()
        executor.start()
        context = RppRuntimeClientContext(host=host, port=port)
        runtime_client = PluginRuntimeClient()
        adapter = None

        def connect_adapter():
            client = PluginAdapter.create_client(
                library_manager=self.library_manager, plugin_info=plugin_info,
                client_info=AdapterClientParams(plugin_name=plugin_name, name="remote_client",
                                                 connection_name=connection_name))
            client.connect_adapter_client__(context, executor=executor)
            return client

        try:
            connected, error = executor.call(context.start(5000))
            assert connected, error
            executor.call(runtime_client.connect(context))
            adapter = connect_adapter()

            statuses = executor.call(runtime_client.list_components(connection_name))
            assert len(statuses) == 1
            first_id = str(statuses[0].instanceId)
            assert executor.call(runtime_client.configure_instance(first_id)).success
            assert executor.call(runtime_client.activate_instance(first_id, adapter=adapter)).success
            message = self._odometry_message(adapter, 6.0)
            assert adapter.validate(message)
            stopped = executor.call(runtime_client.deactivate_instance(first_id, adapter=adapter))
            assert stopped.result.success
            assert executor.call(runtime_client.activate_instance(
                first_id, stopped.state, adapter=adapter)).success

            assert executor.call(runtime_client.load_component(
                str(second.folder), plugin_name, connection_name)).success
            statuses = executor.call(runtime_client.list_components(connection_name))
            second_id = next(str(status.instanceId) for status in statuses
                             if str(status.name) == "remote_second")
            assert executor.call(runtime_client.configure_instance(second_id)).success
            assert executor.call(runtime_client.switch_components(
                connection_name, first_id, second_id, transfer_state=True)).success

            adapter.disconnect_adapter_client__()
            adapter = connect_adapter()
            assert adapter.validate(message)
            assert executor.call(runtime_client.unload_instance(first_id)).success
            assert executor.call(runtime_client.reload_instance(
                second_id, transfer_state=True)).success
            statuses = executor.call(runtime_client.list_components(connection_name))
            reloaded_id = next(str(status.instanceId) for status in statuses
                               if str(status.name) == "remote_second")
            assert reloaded_id != second_id

            adapter.disconnect_adapter_client__()
            adapter = connect_adapter()
            assert adapter.validate(message)
            assert executor.call(runtime_client.deactivate_instance(
                reloaded_id, adapter=adapter)).result.success
            assert executor.call(runtime_client.cleanup_instance(reloaded_id)).success
            executor.call(runtime_client.shutdown())
        finally:
            if adapter is not None:
                adapter.disconnect_adapter_client__()
            executor.call(runtime_client.disconnect())
            executor.call(context.stop())
            executor.stop()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def test_python_to_cpp_remote_lifecycle_and_method_call_happy_path(self):
        """Python parent controls its C++ child through the actual RPC host."""
        component_path = EXAMPLES_DATA_PATH / "test_component_py_with_cpp_subc"
        data_manager = DataManager(library_manager=self.library_manager)
        runtime_builder = ComponentRuntimeBuilder(
            data_manager=data_manager, clock_options=ClockOptions())
        runtime = runtime_builder.build_component_from_path(
            str(component_path))
        context = runtime.get_context()
        remote_child = context.get_subcomponent_context("ctl_main")

        try:
            assert runtime.configure().success
            assert runtime.activate().success
            assert remote_child.activate().success
            python_component = runtime.get_instance()
            message = python_component.Odometry2D()

            message.pose.position.x = 1.0
            assert not (python_component.validate(message))

            message.pose.position.x = 6.0
            assert not (python_component.validate(message))

            message.pose.position.x = 11.0
            assert python_component.validate(message)
            stopped = remote_child.deactivate()
            assert stopped.result.success
            assert remote_child.activate(stopped.state).success
            message.pose.position.x = 6.0
            assert not python_component.validate(message)
            message.pose.position.x = 11.0
            assert python_component.validate(message)
        finally:
            runtime.deactivate()
            remote_child.deactivate()
            runtime.close().require_success("close remote integration runtime")
