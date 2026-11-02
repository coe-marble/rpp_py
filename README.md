# rpp_py

`rpp_py` is the Python runtime for RPP plugins and component servers. It loads
Python plugins, provides generated adapter support, and exposes the
`rpp_component_server_python` executable for cross-language execution.

Install it as part of the `rpp_dev` setup, or install this package alongside
`rpp-common`, `rpp-plugin-registrator`, and `rpp-orchestrator`.

Plugin authors normally subclass `rpp_py.plugin.Plugin` and declare parameters
with `ParameterDescription`. See the integration documentation for plugin
types and workspace workflows.
