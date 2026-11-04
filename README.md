# rpp_py

`rpp_py` is the Python runtime for RPP plugins and component servers. It loads
Python plugins, provides generated adapter support, and exposes the
`rpp_component_server_python` executable for cross-language execution.

Install it as part of the `rpp_dev` setup, or install this package alongside
`rpp-common`, `rpp-plugin-registrator`, and `rpp-orchestrator`.

Plugin authors normally subclass `rpp_py.plugin.Plugin` and declare parameters
with `ParameterDescription`. See the integration documentation for plugin
types and workspace workflows.

The builder returns a tree with external hosts and adapter connections already
prepared. Building a purely native Python tree creates no event loop, RPC
executor, or external process. `ComponentContext` has no infrastructure
`start()` or `stop()` operations. Configure local plugins explicitly and use
ordinary plugin method calls:

```python
context = builder.build_component_from_path(component_path)
try:
    context.configure()
    plugin = context.get_instance()
    # Use the plugin from the application's normal loop.
finally:
    try:
        context.cleanup()
    finally:
        context.close()
```

`close()` synchronously releases the tree's owned host resources, continuing
after errors and reporting the first failure. Destruction supplies best-effort
fallback release; explicit closure is needed for deterministic shutdown and
error reporting, particularly with reference cycles. Reusing a builder does
not retain ownership of, or shut down, an earlier tree. Shared hosts stay alive
until their last owning context releases them. Async RPC processing remains
inside the adapted runtime's executor. Plugin activation remains a separate
lifecycle operation.

Plugins configure resources in `on_configure(context)` and release them in
`on_cleanup()`. Both callbacks return `rpp_py.lifecycle.CallbackResult`;
the default implementations succeed without doing work. `context.configure()`
configures children before their parent and raises `LifecycleError` on failure.
`context.cleanup()` visits the parent before children in reverse order and
continues cleanup after errors, then raises the first error. Adapted instances
run these callbacks in their hosting process. Plugin authors must make cleanup
safe after partially completed configuration. The old `initialize()` API has
been removed; regenerate interfaces and migrate implementations together.

For native trees, `context.activate(state=None)` activates children first, then
the root. The optional handover value goes only to the root; children activate
cold. A failure rolls back successfully activated child subtrees in reverse
order, preserving the activation error and logging rollback errors.
`context.deactivate()` visits the parent before children and continues after
failures. It returns the root's owned `DeactivationResult` only when the whole
tree stops successfully. Adapted activation/deactivation is rejected until
runtime lifecycle control is implemented. These traversal methods do not enforce
lifecycle states or guard application calls; those are manager responsibilities.
