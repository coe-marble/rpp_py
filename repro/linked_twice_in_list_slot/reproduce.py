"""Build a parent whose list slot holds two links to one saved component, with the public builder."""
import os
import sys
from pathlib import Path

os.environ.setdefault("RPP_WHITELIST_PLUGIN_TYPES",
                      "rpp_testing::MotionController2D;rpp_testing::DisturbanceGenerator2D")

import rpp_py
from rpp_cli.testing import setup_tmp_rpp_with_test_plugins
from rpp_py.clock import ClockOptions
from rpp_py.context_builder import ComponentRuntimeBuilder
from rpp_py.data_manager import DataManager

PARENT = (Path(__file__).parent / "workspace" / ".rppws" / "parts"
          / "test_lib__component_plugin_complex_py" / "7bfa9573-ed97-425e-be6a-4334b40cb404")

print("rpp_py from", Path(rpp_py.__file__).parent)
# registers the two example plugins from rpp_testing in a throw-away RPP home (as rpp_py's tests do)
handle = setup_tmp_rpp_with_test_plugins(
    component_whitelist=["example_plugin_simple_py.py", "example_plugin_complex_py.py"])
builder = ComponentRuntimeBuilder(
    data_manager=DataManager(library_manager=handle.library_manager), clock_options=ClockOptions())
try:
    runtime = builder.build_component_from_path(str(PARENT))
except Exception as error:
    print(f"{type(error).__name__}: {error}")
    sys.exit(1)
print("built; slots:", runtime.get_context().list_subcomponents())
