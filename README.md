# rpp_py

`rpp_py` is the Python runtime for RPP plugins and component servers. It loads
Python plugins, provides generated adapter support, and exposes the
`rpp_component_server_python` executable for cross-language execution.

Install it as part of the `rpp_dev` setup, or install this package alongside
`rpp-common`, `rpp-plugin-registrator`, and `rpp-orchestrator`.

Plugin authors normally subclass `rpp_py.plugin.Plugin` and declare parameters
with `ParameterDescription`. See the integration documentation for plugin
types and workspace workflows.

## Contexts and managed slots

Applications build `ComponentRuntime` or `ScriptRuntime` handles through
`ComponentRuntimeBuilder`. Runtime handles own lifecycle and slot administration;
plugins receive `ComponentContext` for parameters, services, and child access.

```python
from rpp_py.context_builder import ComponentRuntimeBuilder

builder = ComponentRuntimeBuilder(data_manager=data_manager, allow_dynamic_components=True)
runtime = builder.build_script_from_path(script_path)
try:
    runtime.configure().require_success()
    runtime.get_context().activate_subcomponents().require_success()
    # Use runtime.load_component(), switch_component(), etc. as needed.
finally:
    runtime.close().require_success()
```

`allow_dynamic_components` defaults to `False`. It accepts a boolean or a
callback `(slot_name, ComponentSlot) -> bool`, evaluated once for each slot,
including nested component slots. Component root handles remain fixed.
`Single` and `Option[T]` slots allow at most one active entry; `List[T]` slots
allow several. The callback is local and is not passed to external hosts.

`get_subcomponent_context(slot, component_name=None)` returns a child view.
List slots require the component name. Use `enumerate_subcomponent_contexts()`
to visit loaded children. Initial registrations use workspace component names,
not their storage IDs. Self and child views expose `get_instance_id()` and
`is_active()`; instance IDs survive lifecycle changes and change on replacement.

A plugin can call `context.request_deactivation()` from a guarded call or worker.
This records a request without invoking callbacks. The application calls
`runtime.process_deactivations(timeout=...)` from its lifecycle thread to process
the tree. Repeated requests coalesce; failed requests remain pending for retry.
Script contexts have no component of their own to deactivate.

Initial validators use
`registration_validators={plugin_type: {component_name: validator}}` and apply
to matching names in every slot, including later dynamic loads. A validator
receives the candidate `ComponentRuntime` and a tuple of registered runtime
views; inspect parameters through `runtime.get_context()`. Validators must not
perform lifecycle operations. `runtime.set_registration_validator(slot, validator)`
sets an additional rule for future admissions in that slot. Both rules run;
existing registrations are not revalidated.

`ScriptRequest.parts_folder` selects an alternative parts directory for any
script source. The `build_script_from_*` convenience methods accept the same
`parts_folder` keyword. `builder.register_external_component_server(language,
command)` selects an executable for future adapted builds without launching it;
existing runtimes and their loaders keep their registered server commands.

Clock/logger configuration belongs to the builder. Transport handles, factories,
and component identity are internal construction data; the public context
constructor does not expose them. Adapted configure/cleanup is supported;
adapted activation/deactivation and typed handover remain pending.

`builder.set_parameter_overrides({"library::Plugin": {"parameter": value}})`
replaces the overrides used for future builds. Values override workspace
parameters for every matching native Python component, including nested
components. Undeclared parameter names and overrides targeting adapted plugins
are rejected, matching `rpp_cpp`'s native-component restriction. Input values are
copied; existing runtimes and their future dynamic loads retain the overrides
captured when they were built. Pass `{}` to clear overrides for new builds.

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

`close()` deactivates active native components, cleans up configured resources,
then releases owned host resources. A failed lifecycle phase is reported before
destroying the affected instance. Destruction supplies best-effort
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

Component servers now start with unconfigured roots. For an adapted context,
`configure()` and `cleanup()` send addressed requests through
`plugin_runtime.capnp`; callbacks execute in the hosting process and return
their success/message to the caller. Runtime clients require the same protocol
version. Repeating a configure request returns its previous result until cleanup
succeeds, including after partial configuration failure. Successful cleanup is
not repeated at host shutdown. A low-level host can register a context with
`host.add_server(adapter, context=context)` to expose these operations.

For native trees, `context.activate(state=None)` activates children first, then
the root. The optional handover value goes only to the root; children activate
cold. A failure rolls back successfully activated child subtrees in reverse
order, preserving the activation error and logging rollback errors.
`context.deactivate()` visits the parent before children and continues after
failures. It returns the root's owned `DeactivationResult` only when the whole
tree stops successfully. Adapted activation/deactivation and state handover are
still pending. These traversal methods do not enforce
lifecycle states or guard application calls; those are manager responsibilities.
