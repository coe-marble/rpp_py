import argparse
import asyncio
import sys

from rpp_plugin_registrator.library_manager import LibraryManager
from rpp_py.build_requests import (
    ComponentRequest, ComponentServerRequest, ComponentServerSlotRequest,
)
from rpp_py.context_builder import ComponentRuntimeBuilder
from rpp_py.data_manager import DataManager
from rpp_py.logger import RppLogger, LoggerOptions


async def _run(args):
    logger = RppLogger(LoggerOptions(name="rpp_component_server_python"))
    logger.debug(f"Starting component server: host={args.host!r}, port={args.port}, "
                f"home={args.home!r}, components={len(args.path)}")
    library_manager = LibraryManager(rpp_home=args.home)
    data_manager = DataManager(library_manager=library_manager)
    runtime_builder = ComponentRuntimeBuilder(data_manager=data_manager, logger=logger,
        allow_dynamic_components=getattr(args, "allow_dynamic_components", False))
    host = None

    try:
        slots = {}
        for path, plugin, connection in zip(args.path, args.plugin, args.conn):
            slots.setdefault(connection, []).append(ComponentRequest(path, plugin))
        logger.debug(f"Building roots: {list(zip(args.path, args.plugin))!r}")
        slot_requests = []
        for connection, components in slots.items():
            plugin_types = {
                library_manager.get_plugin_info_from_lib(component.plugin_name)["PluginType"]
                for component in components
            }
            if len(plugin_types) != 1:
                raise ValueError(
                    f"Hosted slot {connection!r} contains incompatible plugin types")
            slot_requests.append(ComponentServerSlotRequest(
                connection_name=connection, declaration=f"List[{plugin_types.pop()}]",
                components=tuple(components)))
        request = ComponentServerRequest(
            host=args.host, port=args.port,
            slots=tuple(slot_requests))
        host = runtime_builder.build_component_server(request)

        await host.run_async()
    finally:
        logger.debug("Stopping component runtimes")
        active_error = sys.exc_info()[1]
        errors = []
        if host is not None:
            try:
                host.close()
            except Exception as error:
                errors.append(error)
                logger.error(f"Component shutdown failed: {error!r}")
        if errors and active_error is None:
            raise errors[0]


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
    argument_parser.add_argument('--allow-dynamic-components', action='store_true')
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
