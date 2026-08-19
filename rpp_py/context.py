
from typing import Dict

from rpp_py.clock import clock_factory, Clock
from rpp_py.logger import RppLogger
from rpp_py.parameter_handler import Parameters

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
        if slot_type.startswith("List[") and slot_type.endswith("]"):
            return slot_type[5:-1], True  # Extract the component type from "List[ComponentType]" and return True for allow_list
        elif slot_type.startswith("Options[") and slot_type.endswith("]"):
            return slot_type[8:-1], True  # Extract the component type from "Options[ComponentType]" and return True for allow_list
        else:
            return slot_type, False  # Return the original type and False for allow_list if it's a single component


    def __init__(self,
            instance=None,
            params: Parameters=None,
            subcomponents : Dict[str, 'ComponentContext']=None,
            spec: Dict[str, str]=None,
            clock_options=None, logger=None):
        self._instance = instance
        self._params = params or Parameters()
        self._subcomponents = subcomponents or {}
        self._spec = spec or {}
        self._clock = clock_factory(clock_options)
        self._logger = logger or RppLogger()


    def initialize(self):
        for subcontexts in self._subcomponents.values():
            if not isinstance(subcontexts, list):
                subcontexts = [subcontexts]
            for subcontext in subcontexts:
                if hasattr(subcontext, "initialize"):
                    subcontext.initialize()
        if hasattr(self._instance, "initialize"):
            self._instance.initialize(self)


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

    def list_subcomponents(self):
        return list(self._subcomponents.keys())

    def get_instance(self):
        return self._instance

    def get_subcomponent_context(self, slot_name) -> 'ComponentContext':
        return self._subcomponents.get(slot_name, None)
