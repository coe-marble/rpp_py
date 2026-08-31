

from pathlib import Path
from dataclasses import dataclass
from typing import Dict

from rpp_orchestrator.component_storage import ComponentRecord, LinkedComponentRecord

from rpp_py.parameter_handler import ParameterHandler
from rpp_py.adapter_info import AdapterClientParams
from rpp_py.external_component_process import (
    ExternalComponentProcessRegistry,
    default_external_component_process_registry,
)

from .data_manager import DataManager
from .clock import ClockOptions
from .context import ComponentContext
from .plugin_loader import PluginAdapter, PythonPluginLoader
from rpp_py.logger import RppLogger


class ComponentContextBuilder:

    @dataclass(frozen=True)
    class ComponentRoot:
        path: str | Path
        plugin_name: str = ""

    @dataclass
    class _ComponentNode:
        path: str | Path
        parent_component_path: str | Path
        plugin_name: str
        record: ComponentRecord
        plugin_info: Dict[str, str]

    @dataclass
    class _PreparedComponent:
        instance: object
        process: object


    def __init__(
            self, data_manager: DataManager = None, clock_options=None,
            external_component_process_registry: ExternalComponentProcessRegistry = None,
            logger: RppLogger = None):
        self.data_manager = data_manager or DataManager()
        self.clock_options = clock_options or ClockOptions()
        self.external_component_process_registry = \
            external_component_process_registry \
            or default_external_component_process_registry()
        self.logger = logger or RppLogger()
        self._prepared_components: dict[str, ComponentContextBuilder._PreparedComponent] = {}


    def build_component_from_path(
            self, component_path: str | Path) -> ComponentContext:
        """Build a context whose root is the component at ``component_path``."""
        self._prepare_component_processes([
            self._make_component_node(component_path, component_path, "")
        ])
        return self._build_component_from_path(component_path)

    def build_from_component_roots(
            self, roots: list[ComponentRoot]) -> list[ComponentContext]:
        """Build all root contexts after preprocessing their complete trees."""
        root_nodes = [
            self._make_component_node(
                root.path, root.path, root.plugin_name)
            for root in roots
        ]
        self._prepare_component_processes(root_nodes)
        return [
            self._build_component_from_path(
                root.path, root.parent_component_path, root.plugin_name)
            for root in root_nodes
        ]

    def build_script_from_path(
            self, script_path: str | Path, *,
            configuration: str | None = None) -> ComponentContext:
        """Build a local script using the description beside its workspace."""
        description_path = self.data_manager.\
            get_default_script_description_path(script_path)
        return self._build_script_from_description_path(
            description_path, configuration=configuration
        )

    def build_script_from_library(
            self, library_name: str, script_name: str, *,
            configuration: str | None = None) -> ComponentContext:
        """Build a local or linked script from the current workspace."""
        description_path = self.data_manager.\
            get_script_description_path_from_library(library_name, script_name)
        return self._build_script_from_description_path(
            description_path, configuration=configuration
        )

    def _build_script_from_description_path(
            self, script_description_path, parts_folder="", configuration=None):
        script_description = self.data_manager.load_script_description(script_description_path)
        components = self._get_script_components(script_description, configuration)

        if not parts_folder:
            parts_folder = self.data_manager\
                .get_default_script_parts_folder_path_from_description(
                    script_description_path)

        roots = []
        for assigned_components in components.values():
            if not isinstance(assigned_components, list):
                assigned_components = [assigned_components]
            for component in assigned_components:
                component_path = self.data_manager.get_component_path_in_parts_folder(
                    parts_folder, component["PluginName"], component["Id"])
                roots.append(self._make_component_node(
                    component_path, component_path, component["PluginName"]
                ))
        self._prepare_component_processes(roots)

        subcomponents = {}
        for slot_name, assigned_components in components.items():
            if not isinstance(assigned_components, list):
                assigned_components = [assigned_components]
            subcomponents[slot_name] = []
            for component in assigned_components:
                component_path = self.data_manager.get_component_path_in_parts_folder(
                    parts_folder, component["PluginName"], component["Id"])
                subcomponents[slot_name].append(
                    self._build_component_from_path(component_path)
                )

        return ComponentContext(subcomponents=subcomponents, \
                clock_options=self.clock_options, spec=script_description.spec,
                logger=self.logger)

    @staticmethod
    def _get_script_components(script_description, configuration=None):
        """Return assignments from a configuration-based script description.

        The ``components`` fallback keeps callers using the former lightweight
        description object working while they migrate to ``ScriptDescription``.
        """
        configurations = getattr(script_description, "configurations", None)
        if configurations is None:
            return getattr(script_description, "components", {})
        if not configurations:
            raise RuntimeError("Script description does not define configurations.")

        selected = configuration or script_description.active_configuration
        if selected not in configurations:
            raise RuntimeError(
                f"Script configuration '{selected}' is not defined."
            )

        selected_configuration = configurations[selected]
        components = selected_configuration.get("Components")
        if not isinstance(components, dict):
            raise RuntimeError(
                f"Script configuration '{selected}' does not define Components."
            )
        return components

    def _build_component_from_path(
            self, component_path, parent_component_path="", plugin_name=""):
        if not parent_component_path:
            parent_component_path = component_path

        record = self.resolve_component(component_path, parent_component_path, plugin_name)
        plugin_info = self.data_manager.get_plugin_info_from_lib(record.plugin_name)
        source_language = plugin_info["SourceLanguage"].lower()

        if source_language == "python":
            return self._build_native_component(
                record, plugin_info
            )
        return self._build_adapted_component(
            record, plugin_info
        )

    def resolve_component(self, component_path, parent_component_path, plugin_name=""):
        component_record = self.data_manager.load_component_info(component_path)
        if isinstance(component_record, LinkedComponentRecord):
            linked_record = component_record
            if not plugin_name:
                raise RuntimeError("Plugin name must be provided for linked components.")
            linked_component_path = self.data_manager.get_linked_component_folder_path(
                parent_component_path, plugin_name, linked_record.linked_component_id)
            linked_component_record = self.data_manager.load_component_info(linked_component_path)
            if isinstance(linked_component_record, ComponentRecord):
                return linked_component_record
            else:
                raise RuntimeError("Doubly linked components are not supported.")
        elif isinstance(component_record, ComponentRecord):
            return component_record
        else:
            raise RuntimeError("Invalid component record type.")

    def _build_native_component(self,
            record: ComponentRecord, plugin_info: Dict[str, str]):
        instance = PythonPluginLoader(self.data_manager.lm)\
            .create_instance(record.plugin_name)
        params = self._load_parameters(record, plugin_info)

        subcomponents = {}
        for slot_name, subcomponent_infos in record.subcomponents.items():
            if not isinstance(subcomponent_infos, list):
                subcomponent_infos = [subcomponent_infos]
            subcomponents[slot_name] = []
            for subcomponent_info in subcomponent_infos:
                subcomponent_path = self.data_manager\
                    .get_subcomponent_folder_path(record.folder, subcomponent_info.id)
                subcomponents[slot_name].append(self._build_component_from_path(
                    subcomponent_path, record.folder, subcomponent_info.plugin_name))
        spec = record.subcomponent_spec
        return ComponentContext(instance=instance, params=params, spec=spec,
            subcomponents=subcomponents, clock_options=self.clock_options,
            logger=self.logger)

    def _build_adapted_component(self,
            record: ComponentRecord, plugin_info: Dict[str, str]):
        prepared = self._prepared_components.get(str(record.folder))
        if prepared is None:
            raise RuntimeError(
                f"Adapted component '{record.folder}' was not prepared.")
        return ComponentContext(
            instance=prepared.instance,
            params=self._load_parameters(record, plugin_info),
            spec=record.subcomponent_spec,
            subcomponents={},
            clock_options=self.clock_options,
            logger=self.logger,
            runtime=prepared.process,
        )

    def _make_component_node(
            self, component_path, parent_component_path, plugin_name):
        record = self.resolve_component(
            component_path, parent_component_path, plugin_name)
        plugin_info = self.data_manager.get_plugin_info_from_lib(record.plugin_name)
        return self._ComponentNode(
            component_path, parent_component_path, plugin_name,
            record, plugin_info)

    def _collect_component_nodes(self, node):
        source_language = node.plugin_info["SourceLanguage"].strip().lower()
        if source_language != "python":
            yield node
            return

        for subcomponent_infos in node.record.subcomponents.values():
            if not isinstance(subcomponent_infos, list):
                subcomponent_infos = [subcomponent_infos]
            for subcomponent_info in subcomponent_infos:
                subcomponent_path = self.data_manager.get_subcomponent_folder_path(
                    node.record.folder, subcomponent_info.id)
                child = self._make_component_node(
                    subcomponent_path, node.record.folder,
                    subcomponent_info.plugin_name)
                yield from self._collect_component_nodes(child)

    def _prepare_component_processes(self, roots):
        self._prepared_components.clear()
        groups: dict[str, list[ComponentContextBuilder._ComponentNode]] = {}
        for root in roots:
            for node in self._collect_component_nodes(root):
                source_language = node.plugin_info["SourceLanguage"].strip().lower()
                groups.setdefault(source_language, []).append(node)

        for source_language, nodes in groups.items():
            for node in nodes:
                connection_name = f"{node.record.name}_connection"
                client_info = AdapterClientParams(
                    plugin_name=node.plugin_info["PluginName"],
                    name=f"{node.record.id}_client",
                    connection_name=connection_name,
                )
                adapter_client = PluginAdapter.create_client(
                    library_manager=self.data_manager.lm,
                    plugin_info=node.plugin_info,
                    client_info=client_info,
                    logger=self.logger,
                )
                runtime = self.external_component_process_registry.create(
                    source_language,
                    adapter_client=adapter_client,
                    component_path=node.record.folder,
                    plugin_name=node.record.plugin_name,
                    connection_name=connection_name,
                    rpp_home=self.data_manager.lm.rpp_home,
                    logger=self.logger,
                )
                self._prepared_components[str(node.record.folder)] = \
                    self._PreparedComponent(adapter_client, runtime)

    @staticmethod
    def _load_parameters(record, plugin_info):
        with ParameterHandler(record.folder) as param_handler:
            loaded = param_handler.load_parameters_from_python_module()
        metadata = plugin_info.get("PluginMetadata", {})
        return ParameterHandler.resolve_params(
            metadata.get("Parameters", {}), loaded
        )
