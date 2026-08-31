import argparse
import asyncio

from rpp_plugin_registrator.library_manager import LibraryManager
from rpp_py.adapter_info import AdapterServerParams
from rpp_py.context_builder import ComponentContextBuilder
from rpp_py.data_manager import DataManager
from rpp_py.plugin_loader import PluginAdapter
from rpp_py.rpp_server_host import RppServerHost
from rpp_py.logger import RppLogger, LoggerOptions


async def _run(args):
    logger = RppLogger(LoggerOptions(name="rpp_component_server_python"))
    logger.debug(f"Starting component server: host={args.host!r}, port={args.port}, "
                f"home={args.home!r}, components={len(args.path)}")
    library_manager = LibraryManager(rpp_home=args.home)
    data_manager = DataManager(library_manager=library_manager)
    context_builder = ComponentContextBuilder(data_manager=data_manager, logger=logger)
    host = RppServerHost(host=args.host, port=args.port, logger=logger)
    contexts = []

    try:
        roots = [
            ComponentContextBuilder.ComponentRoot(path, plugin)
            for path, plugin in zip(args.path, args.plugin)
        ]
        logger.debug(f"Building roots: {list(zip(args.path, args.plugin))!r}")
        contexts = context_builder.build_from_component_roots(roots)
        for context, conn, plugin in zip(contexts, args.conn, args.plugin):
            plugin_info = library_manager.get_plugin_info_from_lib(
                plugin_name=plugin
            )
            await context.start()
            logger.debug(f"Context started: plugin={plugin!r}, connection={conn!r}")
            context.initialize()

            server_info = AdapterServerParams(
                backend=context.get_instance(),
                plugin_name=plugin_info["PluginName"],
                name=f"{conn}_server",
                connection_name=conn,
            )
            server = PluginAdapter.create_server(
                library_manager=library_manager,
                plugin_info=plugin_info,
                server_info=server_info,
                logger=logger,
            )
            host.add_server(server)
            logger.debug(f"Adapter registered: plugin={plugin!r}, connection={conn!r}")

        await host.run_async()
    finally:
        logger.debug("Stopping contexts")
        for context in reversed(contexts):
            await context.stop()


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

    asyncio.run(_run(args))
    print("Exiting...")


if __name__ == "__main__":
    main()
