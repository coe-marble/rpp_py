import argparse
import asyncio

from rpp_plugin_registrator.library_manager import LibraryManager
from rpp_py.adapter_info import AdapterServerParams
from rpp_py.capnp_runtime import CapnpRuntime
from rpp_py.plugin_runtime import PluginRuntimeServer
from rpp_py.python_plugin_loader import PluginAdapter, PythonPluginLoader
from rpp_py.rpp_server_host import RppServerHost

def main():

    argument_parser = argparse.ArgumentParser(description="RPP Component Server for Python plugins")
    argument_parser.add_argument("--host", help="Host address for the server")
    argument_parser.add_argument("--runtime-port", type=int, help="Port number for the server")
    argument_parser.add_argument("--plugin-port", type=int, help="Port number for the server")
    argument_parser.add_argument("--plugin", help="Name of the plugin to load")
    argument_parser.add_argument("--home", help="Home directory for RPP")
    argument_parser.add_argument("--component-path", help="Path to component directory")
    args = argument_parser.parse_args()

    library_manager = LibraryManager(rpp_home=args.home)
    plugin_info = library_manager.get_plugin_info_from_lib(args.plugin)
    loader = PythonPluginLoader(library_manager=library_manager, available_plugins={args.plugin: plugin_info})

    instance = loader.create_instance(args.plugin)
    server_info = AdapterServerParams(host=args.host, port=args.plugin_port,
            backend=instance, plugin_name=plugin_info["PluginName"])
    server = PluginAdapter.create_server(library_manager=library_manager,
            plugin_info=plugin_info, server_info=server_info)

    host = RppServerHost(host=args.host, runtime_port=args.runtime_port)
    host.add_server(server)
    host.run()
    print("Exiting...")


if __name__ == "__main__":
    main()