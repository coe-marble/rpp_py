

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Dict

from rpp_plugin_registrator.library_manager import LibraryManager
from rpp_plugin_registrator.registry_config import get_app_interfaces_path
from rpp_py.adapter_info import AdapterServerParams, AdapterClientParams

def import_module_from_path(module_path: str | Path, allow_relative_imports: bool = False):
    path = Path(module_path).resolve()
    if not path.is_file():
        raise ValueError(f"Module path '{module_path}' does not exist.")
    module_name = path.stem
    if not allow_relative_imports:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ValueError(f"Could not find module '{module_name}' at path '{module_path}'.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    parent_dir = path.parent
    path_hash = hashlib.sha256(str(parent_dir).encode("utf-8")).hexdigest()[:16]
    package_name = f"_rpp_plugin_{path_hash}"
    qualified_name = f"{package_name}.{module_name}"

    cached_module = sys.modules.get(qualified_name)
    cached_path = getattr(cached_module, "__file__", None)
    if cached_module is not None and cached_path and Path(cached_path).resolve() == path:
        return cached_module

    package_prefix = f"{package_name}."
    previous_modules = {
        name for name in sys.modules
        if name == package_name or name.startswith(package_prefix)
    }
    if package_name not in sys.modules:
        package = ModuleType(package_name)
        package.__path__ = [str(parent_dir)]
        package.__package__ = package_name
        package.__file__ = str(parent_dir / "__init__.py")
        sys.modules[package_name] = package

    try:
        spec = importlib.util.spec_from_file_location(
            qualified_name,
            str(path)
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Unable to create specification for module {module_path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified_name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        added_modules = [
            name for name in sys.modules
            if (name == package_name or name.startswith(package_prefix))
            and name not in previous_modules
        ]
        # Remove children first, including their attributes on retained packages.
        for name in sorted(added_modules, key=lambda name: name.count("."), reverse=True):
            removed = sys.modules.pop(name, None)
            parent_name, _, attribute = name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None and removed is not None:
                if getattr(parent, attribute, None) is removed:
                    delattr(parent, attribute)
        raise


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
    def _load_class_from_module(client_class_name: str, library_name: str, plugin_type: str):
        interfaces_path = get_app_interfaces_path() / "python"
        interfaces_path_str = str(interfaces_path)
        if interfaces_path_str not in sys.path:
            raise ImportError(
                f"RPP Python interfaces path is not available: {interfaces_path_str}"
            )

        client_path = interfaces_path / "rpp_plugin_types" / library_name / f"{client_class_name}.py"

        module = import_module_from_path(str(client_path), allow_relative_imports=True)

        # Get the class from the module and create an instance
        client_class = getattr(module, client_class_name, None)

        if client_class is None:
            raise AttributeError(
                f"Client class for plugin type '{plugin_type}' not found in '{client_path}'"
            )
        return client_class


    @staticmethod
    def create_client(
            library_manager: LibraryManager, plugin_info: Dict[str, str],
            client_info: AdapterClientParams, logger=None):
        library_name = plugin_info["PluginTypeLibrary"]
        plugin_class_name = plugin_info["PluginTypeClassName"]
        plugin_type = plugin_info["PluginType"]
        client_class_name = f"{plugin_class_name}_AdapterClient"

        instance = PluginAdapter._load_class_from_module(
                client_class_name, library_name, plugin_type)(logger=logger)
        instance.configure_adapter_client__(client_info)
        return instance

    @staticmethod
    def create_server(
            library_manager: LibraryManager, plugin_info: Dict[str, str],
            server_info: AdapterServerParams, logger=None):
        library_name = plugin_info["PluginTypeLibrary"]
        plugin_class_name = plugin_info["PluginTypeClassName"]
        plugin_type = plugin_info["PluginType"]

        server_class_name = f"{plugin_class_name}_AdapterServer"
        instance = PluginAdapter._load_class_from_module(
            server_class_name, library_name, plugin_type)(logger=logger)
        instance.configure_adapter_server__(server_info)
        return instance
