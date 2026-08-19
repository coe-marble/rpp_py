import argparse
import asyncio

from rpp_plugin_registrator.library_manager import LibraryManager
from rpp_py.adapter_info import AdapterServerParams
from rpp_py.capnp_runtime import CapnpRuntime
from rpp_py.plugin_runtime import PluginRuntimeServer
from rpp_py.plugin_loader import PluginAdapter, PythonPluginLoader
from rpp_py.rpp_server_host import RppServerHost

def main():

    argument_parser = argparse.ArgumentParser(
        description="RPP Component Server for Python plugins")
    argument_parser.add_argument("--host",
            help="Host address for the server")
    argument_parser.add_argument("--port",
            type=int, help="Port number for the server")
    argument_parser.add_argument("--home",
            help="Home directory for RPP")
    argument_parser.add_argument("--path",
            help="Path to component directory",
            action='append', required=True)
    argument_parser.add_argument('--conn',
            action='append', required=True)
    argument_parser.add_argument('--plugin',
            action='append', required=True)
    args = argument_parser.parse_args()

    if len(args.conn) == 0 or len(args.plugin) == 0 or len(args.path) == 0:
        raise ValueError("At least one --conn, --plugin, and --path argument must be provided.")

    if len(args.path) != len(args.conn) or len(args.path) != len(args.plugin):
        print(f"Number of --path arguments: {len(args.path)}")
        print(f"Number of --conn arguments: {len(args.conn)}")
        print(f"Number of --plugin arguments: {len(args.plugin)}")
        raise ValueError("The number of --path, --conn, and --plugin arguments must be the same.")

    host = RppServerHost(host=args.host, port=args.port)
    for path, conn, plugin in zip(args.path, args.conn, args.plugin):

        library_manager = LibraryManager(rpp_home=args.home)
        plugin_info = library_manager.get_plugin_info_from_lib(plugin_name=plugin)
        loader = PythonPluginLoader(
            library_manager=library_manager, available_plugins={plugin: plugin_info})

        instance = loader.create_instance(plugin)
        server_info = AdapterServerParams(
            backend=instance, plugin_name=plugin_info["PluginName"],
            name=f"{conn}_server", connection_name=conn)
        server = PluginAdapter.create_server(library_manager=library_manager,
                plugin_info=plugin_info, server_info=server_info)
        host.add_server(server)
    host.run()
    print("Exiting...")


if __name__ == "__main__":
    main()