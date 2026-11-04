"""Synchronous managed ownership and scoped access to component instances."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import inspect
from threading import Condition, RLock, get_ident

from rpp_py.lifecycle import (
    CallbackResult, ComponentInfo, DeactivationResult, LifecycleError, LifecycleState,
)


@dataclass
class _Entry:
    name: str
    component: str = ""
    context: object = None
    generation: int = 0
    state: LifecycleState = LifecycleState.UNLOADED
    transition: str | None = None
    operation_thread: int | None = None
    last_result: CallbackResult = CallbackResult()
    gate: bool = False
    in_flight: int = 0
    configured: bool = False
    may_be_active: bool = False
    operation: object = field(default_factory=RLock)
    condition: object = field(default_factory=lambda: Condition(RLock()))
    callers: dict = field(default_factory=dict)


class PluginManager:
    """Own named component trees and serialize transitions per tree.

    Domain access is borrowed for the duration of with_active(). Callers must
    not retain plugin references or start management operations on that entry
    from inside its domain call. Low-level builders/contexts remain public.
    Synchronous native use allocates no event loops or executor threads.
    """

    def __init__(self, workspace=None, *, builder=None):
        self._workspace = workspace
        self._builder = builder
        self._entries = {}
        self._lock = RLock()
        self._build_lock = RLock()
        self._closing = False

    def _entry(self, name):
        with self._lock:
            if name not in self._entries:
                raise LifecycleError(f"Unknown component {name!r}")
            return self._entries[name]

    def get_info(self, name) -> ComponentInfo:
        entry = self._entry(name)
        with entry.condition:
            return ComponentInfo(entry.name, entry.component, entry.generation,
                entry.state, entry.transition, entry.last_result, entry.gate, entry.in_flight)

    def get_state(self, name) -> LifecycleState:
        return self.get_info(name).state

    def list(self) -> list[ComponentInfo]:
        with self._lock:
            names = list(self._entries)
        return [self.get_info(name) for name in names]

    @staticmethod
    def _invalid(entry, reason):
        result = CallbackResult.failure(reason)
        if entry.transition is None:
            entry.last_result = result
        return result

    def _begin(self, entry, operation, allowed, *, during_close=False):
        with entry.condition:
            if entry.callers.get(get_ident()):
                return self._invalid(entry, "Cannot manage an entry from inside its guarded call")
            if entry.transition is not None:
                return self._invalid(entry, f"{entry.transition} is already in progress")
            if self._closing and not during_close:
                return self._invalid(entry, "Plugin manager is closing")
            if entry.state not in allowed:
                return self._invalid(entry, f"Cannot {operation} from {entry.state.value}")
            entry.transition = operation
            entry.operation_thread = get_ident()
            entry.gate = False
        return None

    @staticmethod
    def _finish(entry, state, result):
        with entry.condition:
            entry.state = state
            entry.transition = None
            entry.operation_thread = None
            entry.last_result = result
            entry.gate = state is LifecycleState.ACTIVE and result.success
            entry.condition.notify_all()
        return result

    @staticmethod
    def _capture(operation, callback):
        try:
            result = callback()
            if not isinstance(result, CallbackResult):
                return CallbackResult.failure(f"{operation} must return CallbackResult")
            return result
        except Exception as error:
            return CallbackResult.failure(f"{operation} failed: {error}")

    @staticmethod
    def _drain(entry, timeout):
        with entry.condition:
            if not entry.condition.wait_for(lambda: entry.in_flight == 0, timeout):
                return CallbackResult.failure("Timed out draining active calls; instance retained")
        return CallbackResult()

    def _build(self, component):
        with self._build_lock:
            if self._builder is None:
                from rpp_py.context_builder import ComponentContextBuilder
                from rpp_py.data_manager import DataManager
                self._builder = ComponentContextBuilder(
                    data_manager=DataManager(workspace_path=self._workspace))
            return self._builder.build_component_from_path(component)

    def load(self, name, component=None) -> CallbackResult:
        if not isinstance(name, str) or not name.strip():
            return CallbackResult.failure("Component name must not be empty")
        with self._lock:
            if self._closing:
                return CallbackResult.failure("Plugin manager is closing")
            entry = self._entries.setdefault(name, _Entry(name))
        with entry.operation:
            failure = self._begin(entry, "load", {LifecycleState.UNLOADED})
            if failure is not None:
                return failure
            entry.component = str(component) if component is not None else entry.component
            if not entry.component:
                return self._finish(entry, LifecycleState.UNLOADED,
                    CallbackResult.failure("A component path is required"))
            try:
                entry.context = self._build(entry.component)
                if entry.context is None:
                    raise LifecycleError("Builder returned no context")
                entry.generation += 1
                entry.configured = False
                entry.may_be_active = False
            except Exception as error:
                return self._finish(entry, LifecycleState.ERROR, CallbackResult.failure(str(error)))
            return self._finish(entry, LifecycleState.UNCONFIGURED, CallbackResult())

    def configure(self, name) -> CallbackResult:
        entry = self._entry(name)
        with entry.operation:
            failure = self._begin(entry, "configure", {LifecycleState.UNCONFIGURED})
            if failure is not None:
                return failure
            entry.configured = True  # cleanup must handle partial configuration
            result = self._capture("configure", entry.context.configure)
            return self._finish(entry, LifecycleState.INACTIVE if result.success else LifecycleState.ERROR, result)

    def activate(self, name, state=None) -> CallbackResult:
        entry = self._entry(name)
        with entry.operation:
            with entry.condition:
                if entry.state is LifecycleState.ACTIVE and entry.transition is None and not self._closing:
                    return CallbackResult()
            failure = self._begin(entry, "activate", {LifecycleState.INACTIVE})
            if failure is not None:
                return failure
            entry.may_be_active = True
            result = self._capture("activate", lambda: entry.context.activate(state))
            return self._finish(entry, LifecycleState.ACTIVE if result.success else LifecycleState.ERROR, result)

    def _stop(self, entry):
        try:
            result = entry.context.deactivate()
            if not isinstance(result, DeactivationResult):
                raise LifecycleError("deactivate must return DeactivationResult")
            if result.result.success:
                entry.may_be_active = False
            return result
        except Exception as error:
            return DeactivationResult(CallbackResult.failure(f"deactivate failed: {error}"))

    def deactivate(self, name, *, timeout=None) -> DeactivationResult:
        entry = self._entry(name)
        with entry.operation:
            with entry.condition:
                if entry.state is LifecycleState.INACTIVE and entry.transition is None:
                    return DeactivationResult()
                if entry.state is LifecycleState.ERROR and not entry.may_be_active:
                    return DeactivationResult(self._invalid(entry,
                        "Failed configuration requires cleanup before reconfiguration"))
            failure = self._begin(entry, "deactivate", {LifecycleState.ACTIVE, LifecycleState.ERROR})
            if failure is not None:
                return DeactivationResult(failure)
            drained = self._drain(entry, timeout)
            result = self._stop(entry) if drained.success else DeactivationResult(drained)
            self._finish(entry, LifecycleState.INACTIVE if result.result.success else LifecycleState.ERROR,
                         result.result)
            return result if result.result.success else DeactivationResult(result.result)

    def cleanup(self, name) -> CallbackResult:
        entry = self._entry(name)
        with entry.operation:
            with entry.condition:
                if entry.may_be_active:
                    return self._invalid(entry, "Deactivate the component before cleanup")
            failure = self._begin(entry, "cleanup", {LifecycleState.INACTIVE, LifecycleState.ERROR})
            if failure is not None:
                return failure
            result = self._capture("cleanup", entry.context.cleanup)
            if result.success:
                entry.configured = False
            return self._finish(entry, LifecycleState.UNCONFIGURED if result.success else LifecycleState.ERROR, result)

    def unload(self, name, *, timeout=None) -> CallbackResult:
        entry = self._entry(name)
        with entry.operation:
            failure = self._begin(entry, "unload", set(LifecycleState), during_close=True)
            if failure is not None:
                return failure
            result = self._drain(entry, timeout)
            if result.success and entry.context is not None:
                if entry.may_be_active:
                    result = self._stop(entry).result
                if result.success and entry.configured:
                    result = self._capture("cleanup", entry.context.cleanup)
                    if result.success:
                        entry.configured = False
                if result.success:
                    try:
                        entry.context.close()
                        entry.context = None
                    except Exception as error:
                        result = CallbackResult.failure(f"unload failed: {error}")
            return self._finish(entry, LifecycleState.UNLOADED if result.success else LifecycleState.ERROR, result)

    @contextmanager
    def _borrow(self, name, generation):
        entry = self._entry(name)
        thread = get_ident()
        with entry.condition:
            if generation is not None and generation != entry.generation:
                raise LifecycleError("Stale component generation")
            if not entry.gate or entry.state is not LifecycleState.ACTIVE:
                raise LifecycleError(f"Component {name!r} is not available for active calls")
            plugin = entry.context.get_instance()
            if plugin is None:
                raise LifecycleError("Component has no root plugin instance")
            entry.in_flight += 1
            entry.callers[thread] = entry.callers.get(thread, 0) + 1
        try:
            yield plugin
        finally:
            del plugin
            with entry.condition:
                entry.in_flight -= 1
                entry.callers[thread] -= 1
                if entry.callers[thread] == 0:
                    del entry.callers[thread]
                entry.condition.notify_all()

    def active(self, name, *, generation=None):
        """Borrow a component within a with block, including across await calls."""
        return self._borrow(name, generation)

    def with_active(self, name, callback, *, generation=None):
        with self._borrow(name, generation) as plugin:
            result = callback(plugin)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise LifecycleError("Use an active() scope while awaiting component methods")
            if result is plugin:
                raise LifecycleError("Managed plugin references cannot escape a guarded call")
            return result

    def switch_plugins(self, source, target, *, transfer_state=True, timeout=None) -> CallbackResult:
        if source == target:
            return CallbackResult.failure("Switch source and target must differ")
        first, second = sorted((self._entry(source), self._entry(target)), key=lambda e: e.name)
        with first.operation, second.operation:
            for entry in (first, second):
                with entry.condition:
                    if entry.callers.get(get_ident()):
                        return CallbackResult.failure("Cannot switch from inside a guarded call")
            if self.get_state(source) is not LifecycleState.ACTIVE:
                return CallbackResult.failure("Switch source must be active")
            target_entry = self._entry(target)
            if target_entry.state is LifecycleState.UNLOADED:
                loaded = self.load(target)
                if not loaded.success:
                    return loaded
            if target_entry.state is LifecycleState.UNCONFIGURED:
                configured = self.configure(target)
                if not configured.success:
                    return configured
            if target_entry.state is not LifecycleState.INACTIVE:
                return CallbackResult.failure("Switch target must be inactive")
            if transfer_state:
                source_type = self._entry(source).context.get_instance().HANDOVER_STATE_TYPE
                target_type = target_entry.context.get_instance().HANDOVER_STATE_TYPE
                if source_type is not target_type:
                    return CallbackResult.failure("Incompatible handover state types")
            stopped = self.deactivate(source, timeout=timeout)
            if not stopped.result.success:
                return stopped.result
            return self.activate(target, stopped.state if transfer_state else None)

    def reload(self, name, *, transfer_state=False, timeout=None, refresh=None) -> CallbackResult:
        entry = self._entry(name)
        with entry.operation:
            was_active = entry.state is LifecycleState.ACTIVE
            snapshot = None
            if was_active:
                stopped = self.deactivate(name, timeout=timeout)
                if not stopped.result.success:
                    return stopped.result
                if transfer_state:
                    snapshot = stopped.state
            unloaded = self.unload(name, timeout=timeout)
            if not unloaded.success:
                return unloaded
            if refresh is not None:
                try:
                    refresh(entry.component)
                except Exception as error:
                    return self._finish(entry, LifecycleState.UNLOADED, CallbackResult.failure(str(error)))
            for action in (lambda: self.load(name), lambda: self.configure(name)):
                result = action()
                if not result.success:
                    return result
            return self.activate(name, snapshot) if was_active else CallbackResult()

    def close(self, *, timeout=None) -> CallbackResult:
        with self._lock:
            entries = list(self._entries.values())
            for entry in entries:
                with entry.condition:
                    if entry.callers.get(get_ident()) or entry.operation_thread == get_ident():
                        return CallbackResult.failure("Cannot close manager from inside managed plugin code")
            self._closing = True
        result = CallbackResult()
        for entry in reversed(entries):
            stopped = self.unload(entry.name, timeout=timeout)
            if not stopped.success and result.success:
                result = stopped
        return result

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        result = self.close()
        if exc_type is None:
            result.require_success("close")

