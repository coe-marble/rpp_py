"""Application-facing component and script runtime handles.

Contexts belong to component data and are passed to plugins.  Runtime handles
own roots (or script slot collections) and are the only public surface for
slot administration.
"""

from __future__ import annotations

from typing import Callable

from rpp_py.lifecycle import CallbackResult


class _Runtime:
    """Common lifecycle and slot-administration facade."""

    def __init__(self, context, manager=None, name=None):
        self._context = context
        self._manager = manager
        self._name = name

    def get_context(self):
        return self._context

    def configure(self):
        self._context._check_guarded_call()
        if self._manager is None:
            return self._context._configure()
        return self._manager.configure(self._name, _context=self._context)

    def activate(self, state=None):
        self._context._check_guarded_call()
        if self._manager is None:
            return self._context._activate(state)
        return self._manager.activate(self._name, state, _context=self._context)

    def deactivate(self, *, timeout=None):
        self._context._check_guarded_call()
        if self._manager is None:
            return self._context._deactivate()
        return self._manager.deactivate(self._name, timeout=timeout, _context=self._context)

    def cleanup(self):
        self._context._check_guarded_call()
        if self._manager is None:
            return self._context._cleanup()
        return self._manager.cleanup(self._name, _context=self._context)

    def close(self, *, timeout=None):
        if self._manager is None:
            self._context._check_guarded_call()
            if self._context._has_active_work():
                self._context._deactivate()
            if self._context._configuration_attempted:
                self._context._cleanup()
            self._context._close()
            return CallbackResult()
        return self._manager.close(timeout=timeout)

    def process_deactivations(self, *, timeout=None):
        """Process queued self-deactivation requests throughout this tree."""
        return self._context._process_deactivations(timeout=timeout)

    def allows_dynamic_components(self, slot_name=None):
        if slot_name is None:
            return self._manager is not None and bool(self._manager._dynamic)
        return bool(self._context._slot(slot_name)._dynamic)

    def get_subcomponent_statuses(self, slot_name):
        return self._context._slot(slot_name).list()

    def load_component(self, slot_name, name, component):
        return self._context._slot(slot_name).load(name, component)

    def configure_component(self, slot_name, name):
        return self._context._slot(slot_name).configure(name)

    def activate_component(self, slot_name, name, state=None):
        return self._context._slot(slot_name).activate(name, state)

    def deactivate_component(self, slot_name, name, *, timeout=None):
        return self._context._slot(slot_name).deactivate(name, timeout=timeout)

    def cleanup_component(self, slot_name, name):
        return self._context._slot(slot_name).cleanup(name)

    def unload_component(self, slot_name, name, *, timeout=None):
        return self._context._slot(slot_name).unload(name, timeout=timeout)

    def switch_component(self, slot_name, source, target, *, transfer_state=True, timeout=None):
        return self._context._slot(slot_name).switch_components(
            source, target, transfer_state=transfer_state, timeout=timeout)

    def reload_component(self, slot_name, name, *, transfer_state=False, timeout=None,
                         refresh: Callable | None = None):
        return self._context._slot(slot_name).reload(
            name, transfer_state=transfer_state, timeout=timeout, refresh=refresh)

    def set_registration_validator(self, slot_name, validator):
        return self._context._slot(slot_name).set_registration_validator(validator)


class ComponentRuntime(_Runtime):
    """Lifecycle handle for one root component registered in a root slot."""

    def get_instance(self):
        instance = self._context._instance
        if instance is None:
            raise RuntimeError("This component runtime has no plugin instance")
        return instance


class ScriptRuntime(_Runtime):
    """Lifecycle handle for a script's root slot collection.

    Scripts deliberately do not expose ``get_instance``: they have no root
    plugin instance, only services and subcomponent slots through their context.
    """

