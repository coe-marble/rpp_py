import asyncio
from pathlib import Path
import sys
import threading
import time

import unittest
import json
from unittest.mock import Base, Mock
from rpp_orchestrator.workspace import Workspace

import rpp_plugin_registrator.registry_config as rp
from rpp_py.clock import ClockOptions
from rpp_py.context_builder import ComponentContextBuilder
from rpp_py.data_manager import DataManager

from rpp_py.component_call_executor import ComponentCallExecutor
from rpp_py.rpp_runtime_client_context import RppRuntimeClientContext
from rpp_py.plugin_runtime import PluginRuntimeClient, PluginRuntimeServer
from rpp_py.rpp_server_host import RppServerHost
from rpp_py.context import ComponentContext
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
    instance.initialize(mock_context_with_logger())
    return instance


def write_config(home: Path, orig_cfg: dict):
    new_config_path = home / "config.json"
    home.mkdir(parents=True, exist_ok=True)
    with open(new_config_path, "w", encoding="utf-8") as f:
        json.dump(orig_cfg, f, indent=4)


class BaseTestLoadAndExecutePlugin(unittest.TestCase):

    original_home = None
    rpp_handle = None

    @classmethod
    def source_ros_workspace(cls):
        # get current path of the script
        current_path = Path(__file__).parent.resolve()
        rpp_source_path = current_path.parent.parent.parent.parent / "install" / "setup.bash"
        env = subprocess.check_output(["bash", "-c", "source " + str(rpp_source_path) + " && env"], text=True)
        for line in env.splitlines():
            key, _, value = line.partition("=")
            if key:
                os.environ[key] = value

    @classmethod
    def reset_generated_interface_modules(cls):
        """Prevent one temporary RPP home from leaking generated types to another."""
        for module_name in tuple(sys.modules):
            if module_name == "rpp_plugin_types" or \
                    module_name.startswith("rpp_plugin_types."):
                sys.modules.pop(module_name, None)

    @classmethod
    def tearDownClass(cls):
        # Restore the original RPP_HOME after tests
        if hasattr(cls, 'rpp_handle') and cls.rpp_handle is not None:
            cls.rpp_handle.td.cleanup()
        rp.reset_module()
        cls.reset_generated_interface_modules()
        os.environ.pop("RPP_WHITELIST_PLUGIN_TYPES", None)
        rp.RPP_HOME = cls.original_home

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


class TestLoadAndExecutePluginNative(BaseTestLoadAndExecutePlugin):


    @classmethod
    def setUpClass(cls):
        # setup registrator module and paths
        from rpp_cli.testing import setup_tmp_rpp_with_test_plugins
        cls.original_home = rp.RPP_HOME
        cls.source_ros_workspace()

        os.environ["RPP_WHITELIST_PLUGIN_TYPES"] = \
            "rpp_testing::MotionController2D;rpp_testing::DisturbanceGenerator2D"
        whitelist = [
            "example_plugin_simple_py.py",
            "example_plugin_complex_py.py",
        ]
        cls.rpp_handle = setup_tmp_rpp_with_test_plugins(component_whitelist=whitelist)
        cls.library_manager = cls.rpp_handle.library_manager
        cls.ws = Workspace(cls.rpp_handle.home / "workspace", cls.library_manager)
        cls.test_lib = cls.rpp_handle.test_lib
        rp.RPP_HOME = cls.rpp_handle.home
        cfg = rp.get_config()
        write_config(cls.rpp_handle.home, cfg)



    def test_load_and_execute_plugin_py(self):
        # Assuming the plugin is located in the 'plugins' directory relative to this test file
        plugins = self.library_manager.get_library_plugins(self.test_lib, source_language="python")
        self.assertTrue(len(plugins) > 0, "No plugins found in the test library.")

        test_plugin = plugins.get("test_lib::ComponentPluginSimplePy")
        self.assertIsNotNone(test_plugin, "Test plugin not found.")

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
        with self.assertRaises(Exception) as context:
            msg.non_existing_field = 10
            assert False, "Expected an exception when setting a non-existing field."

        self.assertTrue("has no attribute 'non_existing_field'" in str(context.exception), "Unexpected exception message.")

        result = instance.validate(msg)

        self.assertFalse(result, "Expected the validation to fail for the test plugin.")
        msg.pose.position.x = 6.0
        result = instance.validate(msg)
        self.assertTrue(result, "Expected the validation to pass for the test plugin.")

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
            self.assertTrue(connected, error)
            client.connect_adapter_client__(context, executor=client_executor)
            client_connected = True

            self.assertFalse(client.validate(self._odometry_message(interface, 1.0)))
            self.assertTrue(client.validate(self._odometry_message(interface, 6.0)))
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

            self.assertFalse(client.validate(self._odometry_message(interface, 1.0)))
            self.assertTrue(client.validate(self._odometry_message(interface, 6.0)))
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
        client_connected = False
        try:
            client.connect_adapter_client__(
                RppRuntimeClientContext(host=host, port=port))
            client_connected = True

            async def validate_messages():
                self.assertFalse(await client.validate_async(
                    self._odometry_message(interface, 1.0)))
                self.assertTrue(await client.validate_async(
                    self._odometry_message(interface, 6.0)))

            asyncio.run(validate_messages())
        finally:
            if client_connected:
                client.disconnect_adapter_client__()
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
        server_backend = create_initialized_plugin_instance(
            loader, "test_lib::ComponentPluginSimplePy")
        server_info = AdapterServerParams(
            plugin_name=plugin_info["PluginName"], backend=server_backend,
            name="test_server", connection_name="test_connection")
        server = PluginAdapter.create_server(
            library_manager=self.library_manager,
            plugin_info=plugin_info,
            server_info=server_info)
        plugin_runtime_server = PluginRuntimeServer(adapters=[server])
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
            self.assertTrue(connected, error)
            client_executor.call(plugin_runtime_client.connect(client_context))
            plugin_client.connect_adapter_client__(
                client_context, executor=client_executor)
            client_connected = True

            plugin_msg = self._odometry_message(plugin_client, 1.0)
            self.assertFalse(plugin_client.validate(plugin_msg))

            client_executor.call(plugin_runtime_client.ping())
            adapters = client_executor.call(plugin_runtime_client.listAdapters())
            self.assertIsInstance(adapters, list, "Expected a list of adapters.")
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
        server_host.add_server(server)
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

        self.assertIsNotNone(server_host._executor)
        self.assertFalse(server_error)

        client_connected = False
        runtime_client = PluginRuntimeClient()
        try:
            client.connect_adapter_client__(
                RppRuntimeClientContext(host=host_name, port=port))
            client_connected = True
            self.assertIsNotNone(client._owned_component_call_executor)
            self.assertFalse(client.validate(self._odometry_message(interface, 1.0)))
            self.assertTrue(client.validate(self._odometry_message(interface, 6.0)))
            # test that the runtime client can connect to the runtime server and shutdown it
            client._component_call_executor.call(
                runtime_client.connect(client._runtime_client_context))
            client._component_call_executor.call(runtime_client.shutdown())
        finally:
            if client_connected:
                client.disconnect_adapter_client__()
            server_thread.join(timeout=2)

        self.assertFalse(server_thread.is_alive())
        self.assertFalse(server_error)

    def test_complex_instance_plugin_using_context_builder(self):

        component_path = EXAMPLES_DATA_PATH / "test_component_py"
        data_manager = DataManager(library_manager=self.library_manager)
        context_builder = ComponentContextBuilder(
            data_manager=data_manager, clock_options=ClockOptions())
        component_context = context_builder.build_component_from_path(
            str(component_path))

        self.assertIsNotNone(component_context, "Component context should not be None.")
        self.assertTrue(hasattr(component_context, '_subcomponents'), "Component context should have subcomponents.")
        self.assertTrue(hasattr(component_context, '_clock'), "Component context should have clock options.")


        params = component_context._params

        class TestClass:
            def __init__(self, a=0, b=0):
                self.a = a
                self.b = b

        self.assertEqual(params.get("param1"), 1, "param1 should be 1")
        self.assertEqual(params.get("param2"), 2, "param2 should be 2")
        self.assertEqual(params.get("param3"), "set_string", "param3 should be 'set_string'")
        self.assertEqual(params.get("param7"), {"a": 10, "b": 20}, "param7 should be an instance of SuperClass with a=10 and b=20")
        self.assertEqual(params.get("param4"), True, "param4 should be True")
        self.assertEqual(params.get("param5"), [1, 2, 3], "param5 should be [1, 2, 3]")
        self.assertEqual(params.get("param6"), {"key1": "value1", "key2": 2}, "param6 should be {'key1': 'value1', 'key2': 2}")

        res = params.get_as("param7", TestClass)
        self.assertIsInstance(res, TestClass, "param7 should be an instance of TestClass")
        self.assertEqual(res.a, 10, "param7.a should be 10")
        self.assertEqual(res.b, 20, "param7.b should be 20")
        instance = component_context.get_instance()
        self.assertIsNotNone(instance, "Instance should not be None.")

        component1 = component_context.get_component("ctl_main")
        self.assertIsNotNone(component1, "Component 'ctl_main' should not be None.")

        with self.assertRaises(RuntimeError):
            component_context.get_component("non_existing_component")




class TestLoadAndExecutePluginAdapter(BaseTestLoadAndExecutePlugin):

    @classmethod
    def setUpClass(cls):
        # setup registrator module and paths
        from rpp_cli.testing import setup_tmp_rpp_with_test_plugins
        cls.original_home = rp.RPP_HOME
        cls.source_ros_workspace()

        os.environ["RPP_WHITELIST_PLUGIN_TYPES"] = \
            "rpp_testing::MotionController2D;rpp_testing::DisturbanceGenerator2D"
        whitelist = [
            "example_plugin_simple_cpp.cpp",
            "example_plugin_simple_py.py",
            "example_plugin_complex_cpp.cpp",
            "example_plugin_complex_py.py",
        ]
        cls.rpp_handle = setup_tmp_rpp_with_test_plugins(component_whitelist=whitelist)
        cls.library_manager = cls.rpp_handle.library_manager
        cls.ws = Workspace(cls.rpp_handle.home / "workspace", cls.library_manager)
        cls.component_record = \
            cls.ws.create_component("ComponentPluginInstance", "test_lib::ComponentPluginSimpleCpp")
        cls.test_lib = cls.rpp_handle.test_lib
        rp.RPP_HOME = cls.rpp_handle.home
        cfg = rp.get_config()
        write_config(cls.rpp_handle.home, cfg)

    def test_load_and_execute_plugin_local_network_with_cpp(self):
        # Assuming the plugin is located in the 'plugins' directory relative to this test file
        plugins = self.library_manager.get_library_plugins(
                self.test_lib, source_language="cpp")
        self.assertTrue(len(plugins) > 0, "No plugins found in the test library.")

        plugin_info = self.library_manager \
            .get_plugin_info_from_lib("test_lib::ComponentPluginSimpleCpp")
        self.assertIsNotNone(plugin_info, "Test plugin not found.")

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
             "--conn", "test_connection"]
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
            self.assertTrue(connected, error)
            client.connect_adapter_client__(context, executor=client_executor)
            client_connected = True
            client_executor.call(runtime_client.connect(context))

            self.assertFalse(client.validate(
                self._odometry_message(MotionController2D, 1.0)))
            self.assertTrue(client.validate(
                self._odometry_message(MotionController2D, 6.0)))
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

    def test_component_py_with_cpp_subc(self):
        component_path = EXAMPLES_DATA_PATH / "test_component_py_with_cpp_subc"
        data_manager = DataManager(library_manager=self.library_manager)
        context_builder = ComponentContextBuilder(
            data_manager=data_manager, clock_options=ClockOptions())
        component_context = context_builder.build_component_from_path(
            str(component_path))

        lifecycle_executor = ComponentCallExecutor()
        lifecycle_executor.start()
        lifecycle_executor.call(component_context.start())
        try:
            component_context.initialize()
            python_component = component_context.get_instance()
            message = python_component.Odometry2D()

            message.pose.position.x = 1.0
            self.assertFalse(python_component.validate(message))

            message.pose.position.x = 6.0
            self.assertFalse(python_component.validate(message))

            message.pose.position.x = 11.0
            self.assertTrue(python_component.validate(message))
        finally:
            lifecycle_executor.call(component_context.stop())
            lifecycle_executor.stop()
