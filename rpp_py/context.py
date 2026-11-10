
from typing import Dict
import weakref

from rpp_py.component_slots import ComponentSlot, SlotKind, parse_component_slot_type
from rpp_py.component_manager import ComponentManager
from rpp_py.clock import clock_factory, Clock
from rpp_py.logger import RppLogger
from rpp_py.parameter_handler import Parameters
from rpp_py.lifecycle import CallbackResult, DeactivationResult, LifecycleError, LifecycleState
from rpp_py.plugin import Plugin

class SubcomponentContext:
    """Parent-facing lifecycle and identity view of one subcomponent."""

    def __init__(self, context):
        self._context = context

    def get_instance(self):
        instance = self._context._instance
        if instance is None:
            raise LifecycleError("Subcomponent instance is not loaded")
        return instance

    def get_instance_id(self):
        manager = self._context._owning_manager()
        if manager is None:
            raise LifecycleError("The subcomponent is not owned by a slot")
        return manager.get_info(self._context._owner_name).instance_id

    def is_native(self):
        return self._context._is_native

    def is_active(self):
        manager = self._context._owning_manager()
        return manager is not None and manager.get_state(self._context._owner_name) is LifecycleState.ACTIVE

    def activate(self, state=None):
        manager = self._context._owning_manager()
        if manager is None:
            raise LifecycleError("The subcomponent is not owned by a slot")
        return manager.activate(self._context._owner_name, state, _context=self._context)

    def deactivate(self, *, timeout=None):
        manager = self._context._owning_manager()
        if manager is None:
            raise LifecycleError("The subcomponent is not owned by a slot")
        return manager.deactivate(self._context._owner_name, timeout=timeout, _context=self._context)


class ComponentContext:

    def __init__(self, instance=None, params=None, subcomponents=None, spec=None):
        """Construct a static low-level context; applications normally use the builder."""
        self._initialize(instance=instance, params=params, subcomponents=subcomponents, spec=spec)

    @classmethod
    def _create(cls, **construction):
        """Builder-only construction with prepared transports and slot configuration."""
        context = cls.__new__(cls)
        context._initialize(**construction)
        return context

    @classmethod
    def parse_component_slot_type(cls, slot_type: str) -> ComponentSlot:
        """Describe a slot's plugin type, construction kind, and activation policy."""
        return parse_component_slot_type(slot_type)


    def _initialize(self,
            instance=None,
            params: Parameters=None,
            subcomponents : Dict[str, 'ComponentContext']=None,
            spec: Dict[str, str]=None,
            clock_options=None, logger=None, runtime=None, *,
            allow_dynamic_components=False, component_factory=None,
            component_name="", component_reference="", plugin_type="",
            registration_validators=None):
        self._instance = instance
        self._params = params or Parameters()
        spec = spec or {}
        self._slot_types = {}
        self._dynamic = allow_dynamic_components
        self._component_name = component_name
        self._component_reference = component_reference
        self._plugin_type = plugin_type
        self._owner = None
        self._owner_name = None
        self._slots = {}
        self._clock = clock_factory(clock_options)
        self._logger = logger or RppLogger()
        self._runtime = runtime
        self._is_native = runtime is None
        self._runtime_closed = False
        self._activation_outstanding = False
        self._tree_active = False
        self._configuration_attempted = False
        try:
            supplied = subcomponents or {}
            for name in dict.fromkeys([*supplied, *spec]):
                children = supplied.get(name, [])
                if not isinstance(children, list):
                    children = [children]
                slot = (parse_component_slot_type(spec[name]) if name in spec
                        else ComponentSlot("", SlotKind.LIST if len(children) > 1 else SlotKind.SINGLE))
                self._slot_types[name] = slot
                if slot.kind is SlotKind.SINGLE and len(children) > 1:
                    raise LifecycleError(f"Single slot {name!r} must be constructed with one component")
                manager = ComponentManager(factory=component_factory,
                    activation_policy=slot.activation_policy,
                    allow_dynamic_components=allow_dynamic_components, plugin_type=slot.plugin_type,
                    validator=(registration_validators or {}).get(name))
                self._slots[name] = manager
                for index, child in enumerate(children):
                    entry_name = child._component_name or str(index)
                    result = manager.adopt(entry_name, child, child._component_reference, initial=True)
                    result.require_success(f"register {name}/{entry_name}")
        except BaseException:
            for manager in reversed(list(self._slots.values())):
                manager.close()
            raise

    def _bind(self, manager, name):
        if self._owner is not None and self._owner() is not None:
            raise LifecycleError("A component context already belongs to a slot")
        self._owner = weakref.ref(manager)
        self._owner_name = name

    def _owning_manager(self):
        if self._owner is None:
            return None
        manager = self._owner()
        if manager is None:
            raise LifecycleError("The component's owning slot has been released")
        return manager

    def _slot(self, name):
        if name not in self._slots:
            raise LifecycleError(f"Component slot {name!r} not found")
        return self._slots[name]

    def _check_guarded_call(self):
        for manager in self._slots.values():
            if manager.called_from_guarded_code():
                raise LifecycleError("Cannot manage a context from inside its guarded component call")
            for child in manager.contexts():
                child._check_guarded_call()

    def _has_active_work(self):
        return self._activation_outstanding or any(slot.deactivation_contexts() for slot in self._slots.values())

    def _close(self):
        """Release resources after managed lifecycle teardown."""
        errors = []
        if self._runtime is not None and not self._runtime_closed:
            try:
                self._runtime.close()
                self._runtime_closed = True
            except Exception as error:
                errors.append(error)
        for manager in reversed(list(self._slots.values())):
            result = manager.close()
            if not result.success:
                errors.append(LifecycleError(result.message))
        if errors:
            for error in errors[1:]:
                self._logger.error(f"Additional component release failure: {error!r}")
            raise errors[0]
        self._instance = None
        self._slots.clear()

    def __del__(self):
        # Explicit close reports errors and releases resources even with cycles.
        runtime = getattr(self, "_runtime", None)
        if runtime is not None and not getattr(self, "_runtime_closed", False):
            try:
                runtime.close()
            except Exception:
                pass


    def _configure(self) -> CallbackResult:
        self._configuration_attempted = True
        for manager in self._slots.values():
            for subcontext in manager.contexts():
                manager.configure(subcontext._owner_name).require_success("configure subcomponent")
        if self._runtime is not None:
            result = self._runtime.configure()
            Plugin._require_callback_result(result, "remote configure")
            return result
        if self._instance is not None:
            return Plugin.configure(self._instance, self)
        return CallbackResult()

    def _cleanup(self) -> CallbackResult:
        if self._has_active_work():
            raise LifecycleError("Deactivate the context before cleanup")
        self._configuration_attempted = True
        errors = []
        if self._runtime is not None:
            try:
                result = self._runtime.cleanup()
                Plugin._require_callback_result(result, "remote cleanup")
            except Exception as error:
                errors.append(error)
        elif self._instance is not None:
            try:
                Plugin.cleanup(self._instance)
            except Exception as error:
                errors.append(error)
        for manager in reversed(list(self._slots.values())):
            for subcontext in reversed(manager.contexts()):
                try:
                    manager.cleanup(subcontext._owner_name).require_success("cleanup subcomponent")
                except Exception as error:
                    errors.append(error)
        if errors:
            for error in errors[1:]:
                self._logger.error(f"Additional component cleanup failure: {error!r}")
            raise errors[0]
        self._configuration_attempted = False
        return CallbackResult()

    def _activate(self, state=None) -> CallbackResult:
        """Activate this component; plugins explicitly control subcomponents."""
        if self._tree_active:
            return CallbackResult()
        if self._runtime is not None:
            result = self._runtime.activate(state)
            Plugin._require_callback_result(result, "remote activate")
            self._activation_outstanding = result.success
            self._tree_active = result.success
            return result
        if self._instance is not None:
            Plugin._validate_state(self._instance, state)
        elif state is not None:
            raise TypeError("A context without a plugin accepts no state")
        if self._instance is not None:
            self._activation_outstanding = True
            result = self._instance.activate_state(state)
        else:
            result = CallbackResult()
        self._tree_active = True
        return result

    def _deactivate(self) -> DeactivationResult:
        """Stop parent before children; export root state only if all succeeded."""
        errors = []
        self._tree_active = False
        result = DeactivationResult()
        try:
            if self._runtime is not None:
                result = self._runtime.deactivate()
                if not isinstance(result, DeactivationResult):
                    raise LifecycleError("remote deactivate must return DeactivationResult")
                Plugin._require_callback_result(result.result, "remote deactivate")
                self._activation_outstanding = False
                return result
            if self._instance is not None and self._activation_outstanding:
                result = self._instance.deactivate_state()
                self._activation_outstanding = False
        except Exception as error:
            errors.append(error)
        if errors:
            for error in errors[1:]:
                self._logger.error(f"Additional component deactivation failure: {error!r}")
            raise errors[0]
        return result


    def get_clock(self) -> Clock:
        return self._clock

    def get_logger(self) -> RppLogger:
        return self._logger

    def get_parameter(self, param_name, default_value=None):
        return self._params.get(param_name, default_value)

    def get_parameter_as(self, param_name, expected_type, default_value=None):
        return self._params.get_as(param_name, expected_type, default_value)

    def get_subcomponent_instance(self, slot_name, component_name=None):
        slot = self._slot_types.get(slot_name)
        if slot is None:
            raise RuntimeError(f"Component slot '{slot_name}' not found in the context.")
        if slot.kind is SlotKind.LIST and component_name is None:
            raise LifecycleError(f"List slot {slot_name!r} requires a component name")
        context = self._slot(slot_name).selected_context() if component_name is None else \
            self._slot(slot_name)._entry(component_name).context
        if context is None:
            raise LifecycleError(f"Subcomponent {slot_name!r} is not loaded")
        if not context._is_native:
            raise LifecycleError("An adapted subcomponent does not expose a native Python instance")
        instance = context._instance
        if instance is None:
            raise LifecycleError("Subcomponent instance is not loaded")
        return instance

    def enumerate_subcomponent_instances(self, slot_name):
        for context in self._slot(slot_name).contexts():
            yield self.get_subcomponent_instance(slot_name, context._owner_name)

    def list_subcomponents(self):
        return list(self._slots)

    def get_subcomponent_context(self, slot_name, component_name=None):
        slot = self._slot_types.get(slot_name)
        if slot is None:
            raise LifecycleError(f"Component slot {slot_name!r} not found")
        if slot.kind is SlotKind.LIST and component_name is None:
            raise LifecycleError(f"List slot {slot_name!r} requires a component name")
        manager = self._slots[slot_name]
        context = manager.selected_context() if component_name is None else manager._entry(component_name).context
        if context is None:
            raise LifecycleError(f"Subcomponent {slot_name!r} is not loaded")
        return SubcomponentContext(context)

    def enumerate_subcomponent_contexts(self, slot_name):
        for context in self._slot(slot_name).contexts():
            yield SubcomponentContext(context)

    def is_native_subcomponent(self, slot_name, component_name=None):
        return self.get_subcomponent_context(slot_name, component_name).is_native()

    def activate_subcomponents(self):
        """Activate selected exclusive children or all loaded list children."""
        activated = []
        try:
            for manager in self._slots.values():
                with manager._mutation:
                    for child in manager.activation_contexts():
                        was_active = manager.get_state(child._owner_name) is LifecycleState.ACTIVE
                        result = manager.activate(child._owner_name)
                        result.require_success("activate subcomponent")
                        if not was_active:
                            activated.append((manager, child._owner_name))
            return CallbackResult()
        except Exception as error:
            for manager, name in reversed(activated):
                try:
                    manager.deactivate(name).result.require_success(
                        "deactivate rolled-back subcomponent")
                except Exception as rollback_error:
                    self._logger.error(f"Component activation rollback failed: {rollback_error!r}")
            return CallbackResult.failure(str(error))

    def deactivate_subcomponents(self, *, timeout=None):
        """Deactivate active children in reverse registration order."""
        first_failure = CallbackResult()
        for manager in reversed(list(self._slots.values())):
            for child in reversed(manager.deactivation_contexts()):
                result = manager.deactivate(child._owner_name, timeout=timeout).result
                if not result.success and first_failure.success:
                    first_failure = result
        return first_failure

    def with_active_component(self, slot_name, component_name, callback, *,
                              expected_instance_id=None):
        """Run a callback under a named active subcomponent's slot guard."""
        return self._slot(slot_name).with_active(
            component_name, callback, expected_instance_id=expected_instance_id)
