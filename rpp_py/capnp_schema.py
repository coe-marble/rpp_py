from rpp_plugin_registrator.registry_config import get_app_registry_path
from rpp_plugin_registrator.plugin_descriptors.capnp import load_capnp_schema_from_file

_PLUGIN_CAPNP_REGISTRY = {}


def _capnp_file_path(plugin_name: str, file_name: str):
    lib_name, _ = plugin_name.split("::")
    return get_app_registry_path() / "capnp" / lib_name / file_name


def _register_capnp_plugin(plugin_name: str, file_name: str):
    """Register a Cap'n Proto plugin class in the global registry."""
    _, class_name = plugin_name.split("::")
    capnp_file_path = _capnp_file_path(plugin_name, file_name)
    if not capnp_file_path.exists():
        raise FileNotFoundError(f"Cap'n Proto file '{capnp_file_path}' does not exist.")

    parser, loaded = load_capnp_schema_from_file(capnp_file_path, \
            relative_to_source=False,
            with_random_schema_id=False,
            use_global_parser=False)

    class_obj = getattr(loaded, class_name, None)
    if class_obj is None:
        raise ValueError(f"Class '{class_name}' not found in Cap'n Proto schema '{capnp_file_path}'.")
    # Keep the parser alive for the lifetime of the generated Cap'n Proto type.
    # Unlike capnp.load(), SchemaParser does not share mutable global state across
    # temporary RPP homes used in the same Python process.
    _PLUGIN_CAPNP_REGISTRY[(plugin_name, capnp_file_path.resolve())] = \
        (parser, class_obj)
    return class_obj

def get_client_class(plugin_name: str, file_name: str):
    capnp_file_path = _capnp_file_path(plugin_name, file_name).resolve()
    entry = _PLUGIN_CAPNP_REGISTRY.get((plugin_name, capnp_file_path), None)
    if entry is not None:
        return entry[1]
    return _register_capnp_plugin(plugin_name, file_name)


def get_server_class(plugin_name: str, file_name: str):
    capnp_file_path = _capnp_file_path(plugin_name, file_name).resolve()
    entry = _PLUGIN_CAPNP_REGISTRY.get((plugin_name, capnp_file_path), None)
    if entry is not None:
        return entry[1].Server
    return _register_capnp_plugin(plugin_name, file_name).Server


__all__ = ["get_client_class", "get_server_class"]
