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

Applications create contexts through the builder. The build-time option controls
whether the resulting tree may change its component population or selection:

```python
from rpp_py.context_builder import ComponentRuntimeBuilder

builder = ComponentRuntimeBuilder(
    data_manager=data_manager,
    allow_dynamic_components=True,  # Default: False.
)
context = builder.build_script_from_path(script_path)
try:
    context.configure()
    context.activate()
    controller = context.get_component("controller")
    # Use the controller from the application's normal loop.
finally:
    context.close()
```

Each slot owns an internal `ComponentManager`; application code does not create
or retrieve it. `get_parameter`, `get_component`, `get_components`, and
`list_subcomponents` remain context methods. `get_subcomponent_context(slot)`
returns the selected context; `get_subcomponent_contexts(slot)` returns the
collection. The singular accessor also accepts a component entry name.
Single and `Option[T]` slots allow at most one active entry; `List[T]` allows
several. Only list slots return a list from Python `get_component`.

With dynamic changes disabled, initial construction, ordinary lifecycle, and
whole-tree closure remain available. Runtime load/unload, replacement, and
switching to another exclusive candidate return failure. With changes enabled,
use context operations such as `load_component(slot, name, component_path)`,
`configure_component`, `activate_component`, `unload_component`, and
`switch_component(slot, source, target, transfer_state=True)`.

Returned component references must not be retained across unloading/replacement.
When calls can overlap management, borrow with
`with context.active_component(slot, name) as component:`; this scope can span
an `await` on a component method. No async manager facade is required.

Registration validators receive the unconfigured candidate context and a tuple
of existing contexts, including inactive candidates, and return `CallbackResult`.
Use parameters/plugin metadata for admission; validators must not perform
lifecycle operations. Supply initial validators to the builder as
`registration_validators={str(component_path): {slot: validator}}`, or install
a rule for future admissions through
`context.set_registration_validator(slot, validator)`. Installing a new rule
does not evict or revalidate existing entries. Rejected candidates are released
without replacing existing entries; a destructive reload still reports its
actual remaining state if final admission fails.

Clock/logger configuration belongs to the builder. Transport handles, factories,
and component identity are internal construction data; the public context
constructor does not expose them. Adapted configure/cleanup is supported;
adapted activation/deactivation and typed handover remain pending.

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
