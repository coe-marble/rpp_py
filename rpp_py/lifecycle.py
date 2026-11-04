"""Python lifecycle states, results, and manager snapshots."""

from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar


State = TypeVar("State")


class LifecycleState(str, Enum):
    UNLOADED = "UNLOADED"
    UNCONFIGURED = "UNCONFIGURED"
    INACTIVE = "INACTIVE"
    ACTIVE = "ACTIVE"
    ERROR = "ERROR"


class LifecycleError(RuntimeError):
    """A plugin lifecycle callback reported failure."""


@dataclass(frozen=True)
class CallbackResult:
    success: bool = True
    message: str = ""

    @classmethod
    def failure(cls, message: str) -> "CallbackResult":
        return cls(success=False, message=message)

    def require_success(self, operation: str) -> None:
        if not self.success:
            raise LifecycleError(f"{operation} failed: {self.message}")


@dataclass(frozen=True)
class DeactivationResult(Generic[State]):
    result: CallbackResult = CallbackResult()
    state: State | None = None


@dataclass(frozen=True)
class ComponentInfo:
    name: str
    component: str
    generation: int
    state: LifecycleState
    transition: str | None
    last_result: CallbackResult
    callable: bool
    in_flight: int
