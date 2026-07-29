import capnp

from rpp_plugin_registrator.registry_config import get_app_registry_path
from rpp_plugin_registrator.plugin_descriptors.capnp import load_capnp_schema_from_file

_PLUGIN_CAPNP_REGISTRY = {}


def _register_capnp_plugin(plugin_name: str, file_name: str):
    """Register a Cap'n Proto plugin class in the global registry."""
    lib_name, class_name = plugin_name.split("::")
    python_capnp_autogen_path = get_app_registry_path() / "capnp"

    capnp_file_path = python_capnp_autogen_path / lib_name / file_name
    if not capnp_file_path.exists():
        raise FileNotFoundError(f"Cap'n Proto file '{capnp_file_path}' does not exist.")

    loaded = load_capnp_schema_from_file(capnp_file_path, \
            relative_to_source=False,
            with_random_schema_id=False,
            use_global_parser=True)

    class_obj = getattr(loaded, class_name, None)
    if class_obj is None:
        raise ValueError(f"Class '{class_name}' not found in Cap'n Proto schema '{capnp_file_path}'.")
    _PLUGIN_CAPNP_REGISTRY[plugin_name] = class_obj
    return class_obj

def get_client_class(plugin_name: str, file_name: str):
    class_obj = _PLUGIN_CAPNP_REGISTRY.get(plugin_name, None)
    if class_obj is not None:
        return class_obj
    return _register_capnp_plugin(plugin_name, file_name)


def get_server_class(plugin_name: str, file_name: str):
    class_obj = _PLUGIN_CAPNP_REGISTRY.get(plugin_name, None)
    if class_obj is not None:
        return class_obj.Server
    return _register_capnp_plugin(plugin_name, file_name).Server


__all__ = ["get_client_class", "get_server_class"]