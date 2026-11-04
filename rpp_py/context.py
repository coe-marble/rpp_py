
from typing import Dict

from rpp_common.component_slots import parse_component_slot_type
from rpp_py.clock import clock_factory, Clock
from rpp_py.logger import RppLogger
from rpp_py.parameter_handler import Parameters
from rpp_py.lifecycle import CallbackResult, DeactivationResult, LifecycleError
from rpp_py.plugin import Plugin

class ComponentContext:


    @classmethod
    def parse_component_slot_type(cls, slot_type: str) -> tuple[str, bool]:
        """Parse the component slot type to determine if it allows a list of components.
        It can be one of the following:
        - "ComponentType" (single component)
        - "List[ComponentType]" (list of components)
        - "Options[ComponentType]" (options for component)

        Args:
            slot_type (str): The type of the component slot.

        Returns:
            tuple[str, bool]: A tuple containing the component type and
            a boolean indicating if it allows a list of components.
        """
        return parse_component_slot_type(slot_type)


    def __init__(self,
            instance=None,
            params: Parameters=None,
            subcomponents : Dict[str, 'ComponentContext']=None,
            spec: Dict[str, str]=None,
            clock_options=None, logger=None, runtime=None):
        self._instance = instance
        self._params = params or Parameters()
        self._subcomponents = subcomponents or {}
        self._spec = spec or {}
        self._clock = clock_factory(clock_options)
        self._logger = logger or RppLogger()
        self._runtime = runtime
        self._runtime_closed = False
        self._activation_outstanding = False


    def close(self):
        """Release owned hosts; lifecycle cleanup is an explicit prior operation."""
        errors = []
        if self._runtime is not None and not self._runtime_closed:
            try:
                self._runtime.close()
                self._runtime_closed = True
            except Exception as error:
                errors.append(error)
        for children in reversed(list(self._subcomponents.values())):
            if not isinstance(children, list):
                children = [children]
            for child in reversed(children):
                try:
                    child.close()
                except Exception as error:
                    errors.append(error)
        if errors:
            for error in errors[1:]:
                self._logger.error(f"Additional component release failure: {error!r}")
            raise errors[0]
        self._instance = None
        self._subcomponents.clear()

    def __del__(self):
        # Explicit close reports errors and releases resources even with cycles.
        runtime = getattr(self, "_runtime", None)
        if runtime is not None and not getattr(self, "_runtime_closed", False):
            try:
                runtime.close()
            except Exception:
                pass


    def configure(self) -> CallbackResult:
        for subcontexts in self._subcomponents.values():
            if not isinstance(subcontexts, list):
                subcontexts = [subcontexts]
            for subcontext in subcontexts:
                subcontext.configure()
        if self._runtime is None and self._instance is not None:
            return Plugin.configure(self._instance, self)
        return CallbackResult()

    def cleanup(self) -> CallbackResult:
        errors = []
        if self._runtime is None and self._instance is not None:
            try:
                Plugin.cleanup(self._instance)
            except Exception as error:
                errors.append(error)
        for subcontexts in reversed(list(self._subcomponents.values())):
            if not isinstance(subcontexts, list):
                subcontexts = [subcontexts]
            for subcontext in reversed(subcontexts):
                try:
                    subcontext.cleanup()
                except Exception as error:
                    errors.append(error)
        if errors:
            for error in errors[1:]:
                self._logger.error(f"Additional component cleanup failure: {error!r}")
            raise errors[0]
        return CallbackResult()

    def activate(self, state=None) -> CallbackResult:
        """Activate children cold, then activate this root with its handover state.

        The manager owns transition validation and call gates. This traversal
        rolls back only children whose activation completed successfully.
        """
        if self._runtime is not None:
            raise LifecycleError("Adapted lifecycle activation is not supported yet")
        if self._instance is not None:
            Plugin._validate_state(self._instance, state)
        elif state is not None:
            raise TypeError("A context without a plugin accepts no state")
        activated = []
        try:
            for subcontexts in self._subcomponents.values():
                if not isinstance(subcontexts, list):
                    subcontexts = [subcontexts]
                for subcontext in subcontexts:
                    subcontext.activate()
                    activated.append(subcontext)
            if self._instance is not None:
                self._activation_outstanding = True
                return self._instance.activate_state(state)
            return CallbackResult()
        except Exception:
            for subcontext in reversed(activated):
                try:
                    subcontext.deactivate()
                except Exception as error:
                    self._logger.error(f"Component activation rollback failed: {error!r}")
            raise

    def deactivate(self) -> DeactivationResult:
        """Stop parent before children; export root state only if all succeeded."""
        errors = []
        result = DeactivationResult()
        try:
            if self._runtime is not None:
                raise LifecycleError("Adapted lifecycle deactivation is not supported yet")
            if self._instance is not None and self._activation_outstanding:
                result = self._instance.deactivate_state()
                self._activation_outstanding = False
        except Exception as error:
            errors.append(error)
        for subcontexts in reversed(list(self._subcomponents.values())):
            if not isinstance(subcontexts, list):
                subcontexts = [subcontexts]
            for subcontext in reversed(subcontexts):
                try:
                    subcontext.deactivate()
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

    def get_component(self, slot_name):
        component_spec = self._spec.get(slot_name)
        if component_spec is None:
            raise RuntimeError(f"Component slot '{slot_name}' not found in the context.")

        component_context = self.get_subcomponent_context(slot_name)
        if component_context is None:
            return []
        spec_type = self._spec.get(slot_name)
        _, allow_list = ComponentContext.parse_component_slot_type(spec_type)
        if allow_list:
            return [subcontext.get_instance() for subcontext in component_context]
        return component_context[0].get_instance()

    def get_components(self, slot_name):
        component_contexts = self.get_subcomponent_contexts(slot_name)
        return [context.get_instance() for context in component_contexts]

    def list_subcomponents(self):
        return list(self._subcomponents.keys())

    def get_instance(self):
        return self._instance

    def get_subcomponent_context(self, slot_name) -> 'ComponentContext':
        return self._subcomponents.get(slot_name, None)

    def get_subcomponent_contexts(self, slot_name):
        return self._subcomponents.get(slot_name, [])
