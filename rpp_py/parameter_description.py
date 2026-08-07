from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable


class ParameterDescription:
    
    def __init__(self, name: str, default_value: Any):
        self.name = name
        self.default_value = default_value



def ParamSet(*items: ParameterDescription) -> list[ParameterDescription]:
    return list(items)


@dataclass
class LogEntry:
    name: str
    eval_fn: Callable | str | None = None

    def __post_init__(self):
        if self.eval_fn is None:
            self.eval_fn = f"@(x) x.{self.name}"


@dataclass
class RegistryInfo:
    name: str
    visible: bool


@dataclass
class IOArgument:
    name: str
    dim: Any


class DataModel(SimpleNamespace):
    pass