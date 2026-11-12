

from pathlib import Path
from copy import deepcopy
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
from .runtime import ComponentRuntime, ScriptRuntime
from .component_manager import ComponentManager
from .component_slots import parse_component_slot_type
from .build_requests import (
    ComponentRequest, ComponentServerRequest, ComponentServerSlotRequest,
    LibraryScript, ScriptDescriptionPath, ScriptPath, ScriptRequest,
)
from .plugin_loader import PluginAdapter, PythonPluginLoader
from rpp_py.logger import RppLogger


class ComponentRuntimeBuilder:

    ComponentRoot = ComponentRequest
    ParameterOverrides = dict[str, dict[str, object]]

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
            logger: RppLogger = None, *, allow_dynamic_components=False,
            registration_validators=None):
        self.data_manager = data_manager or DataManager()
        self.clock_options = clock_options or ClockOptions()
        self.external_component_process_registry = \
            external_component_process_registry \
            or default_external_component_process_registry()
        self.logger = logger or RppLogger()
        self.allow_dynamic_components = allow_dynamic_components
        self.registration_validators = {kind: dict(rules)
                                        for kind, rules in (registration_validators or {}).items()}
        self._parameter_overrides: ComponentRuntimeBuilder.ParameterOverrides = {}
        self._prepared_components: dict[str, ComponentRuntimeBuilder._PreparedComponent] = {}

    def set_parameter_overrides(self, overrides: ParameterOverrides) -> None:
        """Replace overrides for future builds, keyed by qualified plugin and parameter name.

        Applies to matching native Python components, including nested components.
        Names must be declared by the plugin. Existing runtimes retain their
        resolved values; adapted components reject overrides, as in rpp_cpp.
        """
        self._parameter_overrides = deepcopy(overrides)

    def register_external_component_server(self, language: str, command: str) -> None:
        """Select the server executable for future adapted component builds."""
        self.external_component_process_registry.register_server(language, command)

    def _context_options(self, path, *, allow_dynamic_components=None):
        """Snapshot construction policy for these slots and their future loads."""
        dynamic = (self.allow_dynamic_components if allow_dynamic_components is None
                   else allow_dynamic_components)
        return dict(allow_dynamic_components=dynamic,
                    component_factory=self._dynamic_root_loader(),
                    named_registration_validators={kind: dict(rules)
                        for kind, rules in self.registration_validators.items()})


    def build_component_from_path(
            self, component_path: str | Path) -> ComponentRuntime:
        """Build one component runtime whose context exposes its services."""
        return self._root_runtime(self._build_root_context(component_path))

    def build_component(self, request: ComponentRequest) -> ComponentRuntime:
        return self._root_runtime(self._build_root_context(request.path, request.plugin_name))

    def build_components(self, requests: list[ComponentRequest]) -> list[ComponentRuntime]:
        return self.build_from_component_roots(requests)

    def build_from_component_roots(
            self, roots: list[ComponentRequest]) -> list[ComponentRuntime]:
        """Build root component runtimes after preprocessing their trees."""
        root_nodes = [
            self._make_component_node(
                root.path, root.path, root.plugin_name)
            for root in roots
        ]
        contexts = self._build_prepared_contexts(root_nodes, lambda: [
            self._build_component_from_path(
                root.path, root.path, root.plugin_name)
            for root in root_nodes
        ])
        return [self._root_runtime(context) for context in contexts]

    def _build_prepared_contexts(self, roots, build):
        try:
            self._prepare_component_processes(roots)
            for prepared in self._prepared_components.values():
                prepared.process.prepare()
            return build()
        except BaseException:
            for prepared in reversed(list(self._prepared_components.values())):
                try:
                    prepared.process.close()
                except Exception as error:
                    self.logger.error(f"Component build rollback failed: {error!r}")
            raise
        finally:
            # Ownership transfers to returned contexts, not the reusable builder.
            self._prepared_components.clear()

    def build_script_from_path(
            self, script_path: str | Path, *,
            configuration: str | None = None,
            parts_folder: str | Path | None = None) -> ScriptRuntime:
        """Build a local script using the description beside its workspace."""
        description_path = self.data_manager.\
            get_default_script_description_path(script_path)
        return self._build_script_from_description_path(
            description_path, configuration=configuration, parts_folder=parts_folder
        )

    def build_script(self, request: ScriptRequest) -> ScriptRuntime:
        source = request.source
        if isinstance(source, ScriptPath):
            return self.build_script_from_path(source.path, configuration=request.configuration,
                                               parts_folder=request.parts_folder)
        if isinstance(source, LibraryScript):
            return self.build_script_from_library(
                source.library_name, source.script_name, configuration=request.configuration,
                parts_folder=request.parts_folder)
        if isinstance(source, ScriptDescriptionPath):
            return self.build_script_from_description_path(
                source.path, configuration=request.configuration, parts_folder=request.parts_folder)
        raise TypeError(f"Unsupported script source: {type(source).__name__}")

    def build_script_from_library(
            self, library_name: str, script_name: str, *,
            configuration: str | None = None,
            parts_folder: str | Path | None = None) -> ScriptRuntime:
        """Build a local or linked script from the current workspace."""
        description_path = self.data_manager.\
            get_script_description_path_from_library(library_name, script_name)
        return self._build_script_from_description_path(
            description_path, configuration=configuration, parts_folder=parts_folder
        )

    def build_script_from_description_path(
            self, script_description_path: str | Path, *,
            configuration: str | None = None,
            parts_folder: str | Path | None = None) -> ScriptRuntime:
        """Build a script context from an explicit description path."""
        return self._build_script_from_description_path(
            script_description_path, configuration=configuration, parts_folder=parts_folder
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

        def build():
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

            return ComponentContext._create(subcomponents=subcomponents, \
                    clock_options=self.clock_options, spec=script_description.spec,
                    logger=self.logger,
                    **self._context_options(
                        script_description_path,
                        allow_dynamic_components=self.allow_dynamic_components))

        return ScriptRuntime(self._build_prepared_contexts(roots, build))

    def _build_root_context(self, component_path, plugin_name="") -> ComponentContext:
        return self._build_prepared_contexts([
            self._make_component_node(component_path, component_path, plugin_name)
        ], lambda: self._build_component_from_path(component_path, component_path, plugin_name))

    def _root_runtime(self, context: ComponentContext) -> ComponentRuntime:
        """Put every root in a fixed root slot, exactly as C++ does."""
        manager = ComponentManager(
            allow_dynamic_components=False)
        name = context._component_name or "root"
        result = manager.adopt(name, context, context._component_reference, initial=True)
        result.require_success(f"register root {name}")
        return ComponentRuntime(context, manager, name)

    def _dynamic_root_loader(self):
        """Capture construction resources without retaining this builder."""
        data_manager = self.data_manager
        clock_options = self.clock_options
        logger = self.logger
        registry = self.external_component_process_registry.clone()
        dynamic = self.allow_dynamic_components
        validators = {kind: dict(rules) for kind, rules in self.registration_validators.items()}
        overrides = self._parameter_overrides

        def build(component_path, plugin_name=""):
            builder = ComponentRuntimeBuilder(
                data_manager=data_manager, clock_options=clock_options, logger=logger,
                external_component_process_registry=registry.clone(),
                allow_dynamic_components=dynamic, registration_validators=validators)
            builder.set_parameter_overrides(overrides)
            return builder._build_root_context(component_path, plugin_name)
        return build

    def build_component_server(self, request: ComponentServerRequest):
        """Build a host whose connections each own one top-level slot."""
        from rpp_py.adapter_info import AdapterServerParams
        from rpp_py.plugin_loader import PluginAdapter
        from rpp_py.rpp_server_host import RppServerHost

        if not request.slots:
            raise ValueError("A component server requires at least one hosted slot")
        if len({slot.connection_name for slot in request.slots}) != len(request.slots):
            raise ValueError("Component server slot connection names must be unique")
        nodes = []
        for slot in request.slots:
            if not slot.connection_name or not slot.components:
                raise ValueError("Every hosted slot requires a connection name and an initial component")
            for component in slot.components:
                nodes.append((slot, self._make_component_node(
                    component.path, component.path, component.plugin_name)))

        def build_contexts():
            result = {}
            for slot, node in nodes:
                result.setdefault(slot.connection_name, []).append(
                    self._build_component_from_path(node.path, node.path, node.plugin_name))
            return result

        contexts = self._build_prepared_contexts([node for _, node in nodes], build_contexts)
        host = RppServerHost(host=request.host, port=request.port, logger=self.logger)
        dynamic_root_loader = self._dynamic_root_loader()
        for slot_request in request.slots:
            slot_type = parse_component_slot_type(slot_request.declaration)
            top_level_components = None
            for component in slot_request.components:
                record = self.resolve_component(
                    component.path, component.path, component.plugin_name)
                top_level = record.parent_component_info is None
                if top_level_components is not None and top_level_components != top_level:
                    raise RuntimeError(
                        "A hosted slot cannot mix top-level components and subcomponents")
                top_level_components = top_level
            requested_dynamic = (self.allow_dynamic_components(slot_request.connection_name, slot_type)
                if callable(self.allow_dynamic_components) else self.allow_dynamic_components)
            allows_dynamic_operations = bool(requested_dynamic and top_level_components)
            manager = ComponentManager(
                factory=dynamic_root_loader,
                activation_policy=slot_type.activation_policy,
                allow_dynamic_components=allows_dynamic_operations,
                plugin_type=slot_type.plugin_type)
            for context in contexts[slot_request.connection_name]:
                result = manager.adopt(context._component_name, context,
                                       context._component_reference, initial=True)
                result.require_success(f"register hosted component {context._component_name}")

            first_request = slot_request.components[0]
            initial_record = self.resolve_component(
                first_request.path, first_request.path, first_request.plugin_name)
            info = self.data_manager.get_plugin_info_from_lib(initial_record.plugin_name)
            adapter = PluginAdapter.create_server(
                library_manager=self.data_manager.lm, plugin_info=info,
                server_info=AdapterServerParams(
                    backend=manager.selected_context()._instance,
                    plugin_name=info["PluginName"],
                    name=f"{slot_request.connection_name}_server",
                    connection_name=slot_request.connection_name), logger=self.logger)

            def load(component_path, plugin_name, *, manager=manager, loader=dynamic_root_loader):
                candidate = loader(component_path, plugin_name)
                return manager.adopt(candidate._component_name, candidate,
                                     str(component_path), initial=False)

            host.add_slot(
                adapter, manager, load,
                allows_dynamic_operations=allows_dynamic_operations)
        return host

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
        return ComponentContext._create(instance=instance, params=params, spec=spec,
            subcomponents=subcomponents, clock_options=self.clock_options,
            logger=self.logger, component_name=record.name,
            component_reference=str(record.folder),
            plugin_type=plugin_info.get("PluginType", getattr(record, "plugin_type", "")),
            **self._context_options(record.folder))

    def _build_adapted_component(self,
            record: ComponentRecord, plugin_info: Dict[str, str]):
        prepared = self._prepared_components.get(str(record.folder))
        if prepared is None:
            raise RuntimeError(
                f"Adapted component '{record.folder}' was not prepared.")
        return ComponentContext._create(
            instance=prepared.instance,
            params=self._load_parameters(record, plugin_info),
            spec=record.subcomponent_spec,
            subcomponents={},
            clock_options=self.clock_options,
            logger=self.logger,
            runtime=prepared.process,
            component_name=record.name, component_reference=str(record.folder),
            plugin_type=plugin_info.get("PluginType", getattr(record, "plugin_type", "")),
            **self._context_options(record.folder),
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
        self.external_component_process_registry.begin_build()
        groups: dict[str, list[ComponentRuntimeBuilder._ComponentNode]] = {}
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
                    slot_declaration=node.plugin_info["PluginType"],
                    rpp_home=self.data_manager.lm.rpp_home,
                    logger=self.logger,
                    allow_dynamic_components=(self.allow_dynamic_components
                        if isinstance(self.allow_dynamic_components, bool) else False),
                )
                self._prepared_components[str(node.record.folder)] = \
                    self._PreparedComponent(adapter_client, runtime)

    def _load_parameters(self, record, plugin_info):
        overrides = self._parameter_overrides.get(record.plugin_name)
        metadata = plugin_info.get("PluginMetadata", {})
        declarations = metadata.get("Parameters", {})
        if overrides is not None:
            if plugin_info["SourceLanguage"].strip().lower() != "python":
                raise ValueError(
                    f"Parameter overrides require a native Python plugin: {record.plugin_name}")
            for name in overrides:
                if name not in declarations:
                    raise ValueError(
                        f"Undeclared parameter override '{name}' for plugin '{record.plugin_name}'")
        with ParameterHandler(record.folder) as param_handler:
            loaded = param_handler.load_parameters_from_python_module()
        params = ParameterHandler.resolve_params(declarations, loaded)
        if overrides is not None:
            params.params.update(deepcopy(overrides))
        return params
