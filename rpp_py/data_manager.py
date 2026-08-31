from pathlib import Path

from rpp_plugin_registrator.library_manager import LibraryManager
from rpp_orchestrator.component_storage import (
    ComponentRecord, LinkedComponentRecord,
)
from rpp_orchestrator.workspace import ScriptDescription, Workspace
import json


class DataManager:
    def __init__(
            self, library_manager=None,
            workspace_path: str | Path | None = None):
        self.lm = library_manager if library_manager is not None else LibraryManager()
        self.workspace_path = self._normalize_workspace_path(workspace_path)

    def load_script_description(self, script_path: str) -> ScriptDescription:
        with open(script_path, 'r', encoding='utf-8') as description_file:
            description_json = json.load(description_file)
            return ScriptDescription(
                script_path=Path(script_path),
                language=description_json.get("Language", ""),
                configurations=description_json.get("Configurations", {}),
                active_configuration=description_json.get(
                    "ActiveConfiguration", ""
                ),
                spec=description_json.get("Spec", {})
            )

    def load_component_info(self, component_path: str) -> ComponentRecord | LinkedComponentRecord:
        description_path = Workspace.part_description_path(Path(component_path))
        with open(description_path, 'r', encoding='utf-8') as description_file:
            description_json = json.load(description_file)
            if "LinkedComponentId" in description_json:
                return LinkedComponentRecord.from_dict(description_json, description_path)
            else:
                return ComponentRecord.from_dict(description_json, description_path)

    def get_plugin_info_from_lib(self,
            plugin_name: str, lib_name: str | None= None):
        return self.lm.get_plugin_info_from_lib(plugin_name, lib_name)


    def get_default_script_description_path(self, script_path: str) -> str:
        ws_folder = self._search_for_workspace_folder(script_path)
        name = Path(script_path).stem
        return str(ws_folder / "script_descriptions" / f"{name}.json")

    def get_script_description_path_from_library(
            self, library_name: str, script_name: str) -> str:
        if self.workspace_path is None:
            raise RuntimeError(
                "A workspace path is required to resolve a script by library and name."
            )

        if "::" in script_name:
            script_library, unqualified_name = script_name.split("::", 1)
            if script_library != library_name:
                raise ValueError(
                    f"Script '{script_name}' does not belong to library "
                    f"'{library_name}'."
                )
        else:
            unqualified_name = script_name
        qualified_name = f"{library_name}::{unqualified_name}"
        descriptions_path = self.workspace_path / "script_descriptions"
        if not descriptions_path.is_dir():
            raise RuntimeError(
                f"Script descriptions folder not found: {descriptions_path}"
            )

        matches = []
        for description_path in sorted(descriptions_path.glob("*.json")):
            try:
                with description_path.open(encoding="utf-8") as description_file:
                    description = json.load(description_file)
            except (OSError, json.JSONDecodeError):
                continue

            stored_library = description.get("ScriptLibrary")
            stored_name = description.get("ScriptName")
            if stored_library == library_name and stored_name in {
                    unqualified_name, qualified_name}:
                matches.append(description_path)

        if not matches:
            raise RuntimeError(
                f"Script '{qualified_name}' is not present in workspace "
                f"'{self.workspace_path.parent}'."
            )
        if len(matches) > 1:
            raise RuntimeError(
                f"Multiple descriptions found for script '{qualified_name}' in "
                f"workspace '{self.workspace_path.parent}'."
            )
        return str(matches[0])

    def get_default_script_parts_folder_path(self, script_path: str) -> str:
        ws_folder = self._search_for_workspace_folder(script_path)
        return str(ws_folder / "parts")

    def get_default_script_parts_folder_path_from_description(self, script_description_path:str) -> str:
        script_dir = Path(script_description_path).parent.parent
        parts_dir = script_dir / "parts"
        return str(parts_dir)

    def get_component_path_in_parts_folder(
        self, parts_folder: str, plugin_name: str, component_id: str
    ) -> str:
        plugin_id = self.lm.plugin_id_from_name(plugin_name)
        full_path = Path(parts_folder) / plugin_id / component_id
        if not full_path.exists():
            raise RuntimeError(f"Component folder not found at path: {full_path}")
        return str(full_path)

    def get_linked_component_folder_path(
        self, parent_component_path: str, plugin_name: str, linked_component_id: str
    ) -> str:
        plugin_id = self.lm.plugin_id_from_name(plugin_name)
        full_path = Path(parent_component_path).parent.parent / plugin_id / linked_component_id
        if not full_path.exists():
            raise RuntimeError(f"Linked component folder not found at path: {full_path}")
        return str(full_path)

    def get_subcomponent_folder_path(
        self, parent_component_path: str, subcomponent_id: str
    ) -> str:
        return str(Path(parent_component_path) / "subcomponents" / subcomponent_id)


    def _search_for_workspace_folder(self, script_path: str) -> Path:
        current_path = Path(script_path).parent
        while True:
            if (current_path / ".rppws").exists():
                return current_path / ".rppws"
            if current_path.parent == current_path:
                raise RuntimeError(f"Could not find .rppws folder in parent directories of {script_path}")
            current_path = current_path.parent

    @staticmethod
    def _normalize_workspace_path(
            workspace_path: str | Path | None) -> Path | None:
        if workspace_path is None:
            return None
        path = Path(workspace_path).expanduser().resolve()
        return path if path.name == ".rppws" else path / ".rppws"
