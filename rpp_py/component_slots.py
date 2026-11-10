"""Component-slot metadata and parsing for runtime and workspace consumers."""

from dataclasses import dataclass
from enum import Enum


class ActivationPolicy(str, Enum):
    EXCLUSIVE = "exclusive"
    MULTIPLE = "multiple"


class SlotKind(str, Enum):
    SINGLE = "single"
    OPTION = "option"
    LIST = "list"


@dataclass(frozen=True)
class ComponentSlot:
    plugin_type: str
    kind: SlotKind

    @property
    def accepts_component_list(self) -> bool:
        """Whether initial construction accepts several component entries."""
        return self.kind is not SlotKind.SINGLE

    @property
    def activation_policy(self) -> ActivationPolicy:
        if self.kind is SlotKind.LIST:
            return ActivationPolicy.MULTIPLE
        return ActivationPolicy.EXCLUSIVE


def parse_component_slot_type(slot_type: str) -> ComponentSlot:
    """Describe construction and activation independently for T/Option[T]/List[T]."""
    declaration = slot_type.strip()
    kind = SlotKind.SINGLE
    plugin_type = declaration
    for wrapper, wrapped_kind in (("Option", SlotKind.OPTION), ("List", SlotKind.LIST)):
        if declaration.startswith(f"{wrapper}[") and declaration.endswith("]"):
            kind = wrapped_kind
            plugin_type = declaration[len(wrapper) + 1:-1].strip()
            break
    if not plugin_type or "[" in plugin_type or "]" in plugin_type:
        raise ValueError(f"Invalid component slot type {slot_type!r}; expected T, Option[T], or List[T]")
    return ComponentSlot(plugin_type, kind)
