from __future__ import annotations


from rpp_plugin_types.rpp_common import MotionController2D
from rpp_plugin_types.rpp_common import DisturbanceGenerator2D


COMPONENTS = {
    "ctl_main": "rpp_common::MotionController2D",
    "ctl_disturbance": "rpp_common::DisturbanceGenerator2D",
}

class ComponentPluginPy(MotionController2D):
    def __init__(self, name: str):
        super().__init__(name)


    def step(self, dt: float, disturbance: DisturbanceGenerator2D) -> None:
        # Implement the control logic here
        pass
