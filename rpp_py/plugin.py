from __future__ import annotations
from rpp_common.parameter_description import ParameterDescription
class Plugin:

    def reset(self):
        pass

    def initialize(self, **kwargs):
        raise NotImplementedError("This method should be implemented in the plugin class.")
