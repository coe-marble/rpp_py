import importlib.util
import sys
import os
from typing import List

from rpp_py.parameter_description import ParameterDescription


class Parameters:

    def __init__(self, parameters_dict=None):
        self.params = parameters_dict or {}

    def get(self, param_name, default_value=None):
        return self.params.get(param_name, default_value)

    def get_as(self, param_name, expected_type, default_value=None):
        value = self.get(param_name, default_value)
        if not isinstance(expected_type, type):
            raise TypeError(f"Expected type must be a type, got {type(expected_type).__name__} instead.")
        if not isinstance(value, dict):
            raise TypeError(f"Parameter '{param_name}' is not a dictionary, cannot convert to {expected_type.__name__}.")
        return_value = expected_type()
        for key, val in value.items():
            if not hasattr(return_value, key):
                raise AttributeError(f"'{expected_type.__name__}' object has no attribute '{key}'")
            setattr(return_value, key, val)
        return return_value

class ParameterHandler:

    def __init__(self, folder):
        self.folder = folder


    def load_parameters_from_python_module(self):
        # Load the parameters.py module from the specified folder
        parameters_module_path = os.path.join(self.folder, "params", "parameters.py")
        if not os.path.exists(parameters_module_path):
            raise RuntimeError(f"Parameters module not found at path: {parameters_module_path}")
        spec = importlib.util.spec_from_file_location("parameters", parameters_module_path)
        parameters_module = importlib.util.module_from_spec(spec)
        sys.modules["parameters__"] = parameters_module
        spec.loader.exec_module(parameters_module)

        if not hasattr(parameters_module, "ComponentParameters"):
            raise RuntimeError("The parameters.py module does not contain a 'ComponentParameters' class.")

        return parameters_module.ComponentParameters

    @classmethod
    def resolve_params(cls, description: dict | List[ParameterDescription], module_parameters: dict):
        resolved_parameters = {}
        if isinstance(description, dict):
            for param_name, param_info in description.items():
                default_value = cls.resolve_default_value(param_info)
                if hasattr(module_parameters, param_name):
                    resolved_parameters[param_name] = \
                        getattr(module_parameters, param_name)
                else:
                    resolved_parameters[param_name] = default_value
        else:
            for param_info in description:
                default_value = param_info.default_value
                if hasattr(module_parameters, param_info.name):
                    resolved_parameters[param_info.name] = \
                        getattr(module_parameters, param_info.name)
                else:
                    resolved_parameters[param_info.name] = default_value
        return Parameters(resolved_parameters)


    @classmethod
    def resolve_default_value(cls, param_info):
        # Handle custom class types by instantiating them with the provided parameters

        if param_info['type'] == 'array':
            return [cls.resolve_default_value(x) \
                    for x in param_info.get('default_value', [])]
        if param_info['type'] == 'object':
            resolved = {}
            for f in param_info.get('fields', []).values():
                resolved[f['name']] = cls.resolve_default_value(f)
            return resolved
        if 'default_value' in param_info:
            return param_info['default_value']

        raise RuntimeError(f"Parameter info does not contain a default value: {param_info}")



    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if "parameters__" in sys.modules:
            del sys.modules["parameters__"]