import asyncio
from pathlib import Path

import unittest
import sys
import capnp
from mpl_toolkits.axes_grid1 import host_axes
from rpp_orchestrator.workspace import Workspace

import rpp_plugin_registrator.registry_config as rp
from rpp_plugin_registrator.library_manager import LibraryManager
from rpp_py.rpp_server_host import RppServerHost

sys.path.append(str(Path(__file__).parent.parent.parent / "rpp_py"))



from rpp_py.plugin_runtime import PluginRuntimeClient, PluginRuntimeServer
from rpp_py.python_plugin_loader import (
    PluginAdapter, PythonPluginLoader
)
from rpp_py.adapter_info import AdapterServerParams, AdapterClientParams
from rpp_py.capnp_runtime import CapnpRuntime
import time
import subprocess, os
import socket


class TestLoadAndExecutePlugin(unittest.TestCase):




    @classmethod
    def source_ros_workspace(cls):
        # get current path of the script
        current_path = Path(__file__).parent.resolve()
        rpp_source_path = current_path.parent.parent / "install" / "setup.bash"
        env = subprocess.check_output(["bash", "-c", "source " + str(rpp_source_path) + " && env"], text=True)
        for line in env.splitlines():
            key, _, value = line.partition("=")
            if key:
                os.environ[key] = value


    @classmethod
    def setUpClass(cls):
        # setup registrator module and paths
        from rpp_cli.testing import setup_tmp_rpp_with_test_plugins
        cls.original_home = rp.RPP_HOME
        cls.source_ros_workspace()

        os.environ["RPP_WHITELIST_PLUGIN_TYPES"] = \
            "rpp_testing::MotionController2D;rpp_testing::DisturbanceGenerator2D"
        whitelist = [
            "example_plugin_simple_cpp.cpp", "example_plugin_simple_py.py"
        ]
        cls.rpp_handle = setup_tmp_rpp_with_test_plugins(component_whitelist=whitelist)
        cls.library_manager = cls.rpp_handle.library_manager
        cls.ws = Workspace(cls.rpp_handle.home / "workspace", cls.library_manager)
        cls.component_record = \
            cls.ws.create_component("ComponentPluginInstance", "test_lib::ComponentPluginSimpleCpp")
        cls.test_lib = cls.rpp_handle.test_lib


        rp.RPP_HOME = cls.rpp_handle.home

    @classmethod
    def tearDownClass(cls):
        # Restore the original RPP_HOME after tests
        cls.rpp_handle.td.cleanup()
        rp.RPP_HOME = cls.original_home

    def get_free_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))  # OS allocates a free port
            return s.getsockname()[1]


    def test_load_and_execute_plugin_py(self):
        # Assuming the plugin is located in the 'plugins' directory relative to this test file
        plugins = self.library_manager.get_library_plugins(self.test_lib, source_language="python")
        self.assertTrue(len(plugins) > 0, "No plugins found in the test library.")

        test_plugin = plugins.get("test_lib::ComponentPluginSimplePy")
        self.assertIsNotNone(test_plugin, "Test plugin not found.")

        # Load the plugin
        loader = PythonPluginLoader(library_manager=self.library_manager, available_plugins=plugins)

        from rpp_plugin_types.rpp_testing import MotionController2D
        instance : MotionController2D = loader.create_instance("test_lib::ComponentPluginSimplePy")

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



    def test_load_and_execute_plugin_local_network(self):


        # Assuming the plugin is located in the 'plugins' directory relative to this test file
        plugins = self.library_manager.get_library_plugins(
                self.test_lib, source_language="python")
        self.assertTrue(len(plugins) > 0, "No plugins found in the test library.")

        plugin_info = self.library_manager.get_plugin_info_from_lib("test_lib::ComponentPluginSimplePy")
        self.assertIsNotNone(plugin_info, "Test plugin not found.")

        host = "localhost"
        port = self.get_free_port()
        client_info = AdapterClientParams(host=host, port=port)

        from rpp_plugin_types.rpp_testing import MotionController2D

        # Load the plugin
        client : MotionController2D = PluginAdapter.create_client(
                library_manager=self.library_manager,
                plugin_info=plugin_info,
                client_info=client_info)

        loader = PythonPluginLoader(library_manager=self.library_manager, available_plugins=plugins)
        server_backend : MotionController2D = loader.create_instance("test_lib::ComponentPluginSimplePy")

        server_info = AdapterServerParams(host=host, port=port, \
                backend=server_backend, plugin_name=plugin_info["PluginName"])
        server = PluginAdapter.create_server(library_manager=self.library_manager,
                plugin_info=plugin_info, server_info=server_info)


        runtime = CapnpRuntime()
        async def test_scenario():
            await runtime.start()
            await server.start_adapter_server__(runtime=runtime)
            await client.connect_adapter_client__(runtime=runtime)

            msg = MotionController2D.Odometry2D()
            msg.pose.position.x = 1.0
            msg.pose.position.y = 2.0
            msg.pose.yaw = 0.5

            as_dict = msg.pose.as_dict()
            self.assertEqual(as_dict, { 'position': {'x': 1.0, 'y': 2.0}, 'yaw': 0.5 },
                    "as_dict() method did not return the expected dictionary representation.")

            is_valid = await client.validate(msg)
            self.assertFalse(is_valid.ok, "Expected the validation to fail for the test plugin.")

            msg.pose.position.x = 6.0
            is_valid = await client.validate(msg)
            self.assertTrue(is_valid.ok, "Expected the validation to pass for the test plugin.")
            await runtime.stop()
            return is_valid.ok

        is_valid = asyncio.run(test_scenario())
        # 3. Standardni sinkroni unittest asserti
        self.assertTrue(is_valid)


    def test_plugin_runtime(self):

        host = "localhost"
        runtime_port = self.get_free_port()
        plugin_port = self.get_free_port()

        plugins = self.library_manager.get_library_plugins(
                self.test_lib, source_language="python")
        plugin_info = self.library_manager.get_plugin_info_from_lib("test_lib::ComponentPluginSimplePy")
        loader = PythonPluginLoader(library_manager=self.library_manager, available_plugins=plugins)
        server_backend = loader.create_instance("test_lib::ComponentPluginSimplePy")
        server_info = AdapterServerParams(host=host, port=plugin_port,
                plugin_name=plugin_info["PluginName"], backend=server_backend)
        async def test_runtime():
            runtime = CapnpRuntime()
            await runtime.start()

            server = PluginAdapter.create_server(library_manager=self.library_manager,
                    plugin_info=plugin_info, server_info=server_info)

            plugin_runtime_server = PluginRuntimeServer(host=host, port=runtime_port, adapters=[server])
            plugin_runtime_client = PluginRuntimeClient(host=host, port=runtime_port)

            await plugin_runtime_server.start(runtime=runtime)
            await plugin_runtime_client.connect(runtime=runtime)


            # Test ping
            await plugin_runtime_client.ping()

            # Test listAdapters
            adapters = await plugin_runtime_client.listAdapters()
            self.assertIsInstance(adapters, list, "Expected a list of adapters.")

            # Test shutdown
            await plugin_runtime_client.shutdown()
            await plugin_runtime_server.stop()
            await runtime.stop()

        asyncio.run(test_runtime())


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
        runtime_port = self.get_free_port()
        client_info = AdapterClientParams(host=host, port=port)

        from rpp_plugin_types.rpp_testing import MotionController2D

        # Load the plugin
        client : MotionController2D = PluginAdapter.create_client(
                library_manager=self.library_manager,
                plugin_info=plugin_info,
                client_info=client_info)

        loader = PythonPluginLoader(
                library_manager=self.library_manager, available_plugins=plugins)
        component = self.component_record
        command = ["rpp_component_server_cpp", "--host", host, "--plugin-port", str(port), \
             "--plugin", "test_lib::ComponentPluginSimpleCpp", '--home', str(self.rpp_handle.home), \
             "--component-path", str(component.folder), "--runtime-port", str(runtime_port)]
        server_p = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        runtime = CapnpRuntime()
        async def test_scenario():
            await runtime.start()
            num_retries = 5
            while num_retries > 0:
                try:
                    await client.connect_adapter_client__(runtime=runtime)
                    break
                except Exception:
                    await asyncio.sleep(1)  # Wait before retrying
                    num_retries -= 1
            if num_retries == 0:
                self.fail("Client failed to connect to the server after multiple attempts.")

            runtime_client = PluginRuntimeClient(host=host, port=runtime_port)
            await runtime_client.connect(runtime=runtime)

            msg = MotionController2D.Odometry2D()
            msg.pose.position.x = 1.0
            msg.pose.position.y = 2.0
            msg.pose.yaw = 0.5

            as_dict = msg.pose.as_dict()
            self.assertEqual(as_dict, {'position': {'x': 1.0, 'y': 2.0}, 'yaw': 0.5},
                    "as_dict() method did not return the expected dictionary representation.")

            is_valid = await client.validate(msg)
            self.assertFalse(is_valid.ok, "Expected the validation to fail for the test plugin.")

            msg.pose.position.x = 6.0
            is_valid = await client.validate(msg)
            self.assertTrue(is_valid.ok, "Expected the validation to pass for the test plugin.")

            await runtime_client.shutdown()
            await runtime_client.disconnect()
            await runtime.stop()
            return is_valid.ok

        is_valid = asyncio.run(test_scenario())
        # 3. Standardni sinkroni unittest asserti
        self.assertTrue(is_valid)
        server_p.terminate()