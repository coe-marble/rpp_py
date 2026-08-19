

import os
import importlib.util
import sys
from pathlib import Path
from typing import Dict

from rpp_plugin_registrator.library_manager import LibraryManager
from rpp_plugin_registrator.registry_config import get_app_interfaces_path
from rpp_py.adapter_info import AdapterServerParams, AdapterClientParams

def import_module_from_path(module_path: str, allow_relative_imports=False):
    if not os.path.exists(module_path):
        raise ValueError(f"Module path '{module_path}' does not exist.")
    module_name = os.path.splitext(os.path.basename(module_path))[0]
    if not allow_relative_imports:
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise ValueError(f"Could not find module '{module_name}' at path '{module_path}'.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    path = Path(module_path).resolve()
    module_name = path.stem
    parent_dir = path.parent
    package_name = parent_dir.name

    path_added = False
    if str(parent_dir) not in sys.path:
        path_added = True
        sys.path.insert(0, str(parent_dir))

    spec = importlib.util.spec_from_file_location(
        f"{package_name}.{module_name}",
        str(path)
    )

    if spec is None:
        raise ImportError(f"Unable to create specification for module {module_path}")

    module = importlib.util.module_from_spec(spec)

    module.__package__ = package_name
    module.__path__ = [str(parent_dir)]
    sys.modules[package_name] = sys.modules.get(package_name, module)
    sys.modules[f"{package_name}.{module_name}"] = module
    try:
        spec.loader.exec_module(module)
        return module
    except Exception as e:
        # Čišćenje u slučaju greške
        sys.modules.pop(f"{package_name}.{module_name}", None)
        if path_added:
            sys.path = sys.path[1:]
        raise e


class PythonPluginLoader:

    def __init__(self, library_manager: LibraryManager, available_plugins: list=None):
        self._lm = library_manager
        self.available_plugins = available_plugins
        self.plugin_modules = {}

    def create_instance(self, plugin_name: str):
        if plugin_name not in self.plugin_modules:
            self._load_plugin(plugin_name)

        plugin_module = self.plugin_modules[plugin_name]
        class_name = plugin_name.split("::")[-1]  # Assuming the class name is the
        plugin_class = getattr(plugin_module, class_name, None)
        if plugin_class is None:
            raise AttributeError(f"Plugin class '{class_name}' not found in module '{plugin_name}'")

        return plugin_class()

    def _load_plugin(self, plugin_name: str):
        import importlib.util
        import sys

        if self.available_plugins is not None:
            if plugin_name not in self.available_plugins:
                raise ImportError(f"Plugin {plugin_name} is not available.")
            plugin_info = self.available_plugins[plugin_name]
        else:
            plugin_info = self._lm.get_plugin_info_from_lib(plugin_name)
        source_file = plugin_info["SourceFile"]
        library_name = plugin_info["Library"]
        module_path = self._lm.get_plugin_path_absolute(source_file, library_name)  # Ensure the plugin path is valid

        module = import_module_from_path(module_path, allow_relative_imports=True)
        self.plugin_modules[plugin_name] = module


class PluginAdapter:


    @staticmethod
    def _load_class_from_module(client_class_name: str, library_name: str, plugin_type: str, module_id: str):
        interfaces_path = get_app_interfaces_path() / "python"
        interfaces_path_str = str(interfaces_path)
        if interfaces_path_str not in sys.path:
            assert False, f"Interfaces path {interfaces_path_str} is not in sys.path. Current sys.path: {sys.path}"

        client_path = interfaces_path / "rpp_plugin_types" / library_name / f"{client_class_name}.py"

        if module_id in sys.modules:
            module = sys.modules[module_id]
        else:
            module = import_module_from_path(str(client_path), allow_relative_imports=True)

        # Get the class from the module and create an instance
        client_class = getattr(module, client_class_name, None)

        if client_class is None:
            raise AttributeError(f"Client class for plugin type '{plugin_type}' not found in module '{module_id}'")
        return client_class


    @staticmethod
    def create_client(library_manager: LibraryManager, plugin_info: Dict[str, str], client_info: AdapterClientParams):
        library_name = plugin_info["PluginTypeLibrary"]
        plugin_class_name = plugin_info["PluginTypeClassName"]
        plugin_type = plugin_info["PluginType"]
        module_id = f"{library_manager.plugin_id_from_name(plugin_type)}_client"

        client_class_name = f"{plugin_class_name}_AdapterClient"

        instance = PluginAdapter._load_class_from_module( \
                client_class_name, library_name, plugin_type, module_id)()
        instance.configure_adapter_client__(client_info)
        return instance

    @staticmethod
    def create_server(library_manager: LibraryManager, plugin_info: Dict[str, str], server_info: AdapterServerParams):
        library_name = plugin_info["PluginTypeLibrary"]
        plugin_class_name = plugin_info["PluginTypeClassName"]
        plugin_type = plugin_info["PluginType"]

        module_id = f"{library_manager.plugin_id_from_name(plugin_type)}_server"
        server_class_name = f"{plugin_class_name}_AdapterServer"
        instance = PluginAdapter._load_class_from_module( \
            server_class_name, library_name, plugin_type, module_id)()
        instance.configure_adapter_server__(server_info)
        return instance