"""Public construction inputs and private resolved build requests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ComponentRequest:
    path: str | Path
    plugin_name: str = ""


@dataclass(frozen=True)
class ScriptPath:
    path: str | Path


@dataclass(frozen=True)
class LibraryScript:
    library_name: str
    script_name: str


@dataclass(frozen=True)
class ScriptDescriptionPath:
    path: str | Path


@dataclass(frozen=True)
class ScriptRequest:
    source: ScriptPath | LibraryScript | ScriptDescriptionPath
    configuration: str | None = None
    parts_folder: str | Path | None = None


@dataclass(frozen=True)
class ComponentServerSlotRequest:
    connection_name: str
    declaration: str
    components: tuple[ComponentRequest, ...]


@dataclass(frozen=True)
class ComponentServerRequest:
    host: str = "localhost"
    port: int = 8080
    slots: tuple[ComponentServerSlotRequest, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ComponentBuildRequest:
    """Internal resolved input for one component registration."""

    name: str
    path: str | Path
    parent_path: str | Path
    plugin_name: str
    slot_declaration: str = ""
