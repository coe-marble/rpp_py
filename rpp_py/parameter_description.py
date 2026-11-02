from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

from rpp_common.parameter_description import ParameterDescription, ParamSet


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
