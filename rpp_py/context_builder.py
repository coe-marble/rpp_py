

from typing import Dict

from rpp_orchestrator.component_storage import ComponentRecord, LinkedComponentRecord

from rpp_py.parameter_handler import ParameterHandler

from .data_manager import DataManager
from .clock import ClockOptions
from .context import ComponentContext
from .plugin_loader import PythonPluginLoader


class ComponentContextBuilder:

    def __init__(self, data_manager : DataManager = None, clock_options=None):
        self.data_manager = data_manager or DataManager()
        self.clock_options = clock_options or ClockOptions()


    def build_from_component_path(self, component_path):
        return self.build_for_component(component_path)


    def build_from_script(self, script_path, parts_folder=""):
        description_path = self.data_manager.\
            get_default_script_description_path(script_path)
        return self.build_from_script_description(description_path, parts_folder)

    def build_from_script_description(self, script_description_path, parts_folder=""):
        script_description = self.data_manager.load_script_description(script_description_path)
        if not script_description.components:
            raise RuntimeError("Script description does not contain any components.")

        subcomponents = {}
        if not parts_folder:
            parts_folder = self.data_manager\
                .get_default_script_parts_folder_path_from_description(
                    script_description_path)

        for slot_name, components in script_description.components.items():
            if not isinstance(components, list):
                components = [components]
            subcomponents[slot_name] = []
            for component in components:
                component_path = self.data_manager.get_component_path_in_parts_folder(
                    parts_folder, component["PluginName"], component["Id"])
                subcomponents[slot_name].append(self.build_for_component(component_path))

        return ComponentContext(subcomponents=subcomponents, \
                clock_options=self.clock_options, spec=script_description.spec)

    def build_for_component(self, component_path, parent_component_path="", plugin_name=""):
        if not parent_component_path:
            parent_component_path = component_path

        record = self.resolve_component(component_path, parent_component_path, plugin_name)
        plugin_info = self.data_manager.get_plugin_info_from_lib(record.plugin_name)

        if plugin_info["SourceLanguage"] == "python":
            return self.handle_python_component(record, plugin_info, parent_component_path)
        else:
            raise RuntimeError(f"Unsupported source language: {plugin_info['SourceLanguage']}")

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

    def handle_python_component(self,
            record: ComponentRecord, plugin_info: Dict[str, str],
            parent_component_path: str):
        instance = PythonPluginLoader(self.data_manager.lm)\
            .create_instance(record.plugin_name)

        with ParameterHandler(record.folder) as param_handler:
            loaded = param_handler.load_parameters_from_python_module()
        metadata = plugin_info.get("PluginMetadata", {})
        params = ParameterHandler.resolve_params(metadata.get("Parameters", {}), loaded)


        subcomponents = {}
        for slot_name, subcomponent_infos in record.subcomponents.items():
            if not isinstance(subcomponent_infos, list):
                subcomponent_infos = [subcomponent_infos]
            subcomponents[slot_name] = []
            for subcomponent_info in subcomponent_infos:
                subcomponent_path = self.data_manager\
                    .get_subcomponent_folder_path(record.folder, subcomponent_info.id)
                subcomponents[slot_name].append(self.build_for_component(
                    subcomponent_path, parent_component_path, subcomponent_info.plugin_name))
        spec = record.subcomponent_spec
        return ComponentContext(instance=instance, params=params, spec=spec,
            subcomponents=subcomponents, clock_options=self.clock_options)