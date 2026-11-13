# Reproduction: two links to one saved component in a list slot cannot be built

`workspace/.rppws/parts/` holds:

- `test_lib__component_plugin_complex_py/7bfa9573-...`: the parent. Its slot `ctl_main` is declared `List[rpp_testing::MotionController2D]` and holds two **link** records, `Fin 1` and `Fin 2` (in its `subcomponents/`).
- `test_lib__component_plugin_simple_py/f9c33ed9-...`: the one saved leaf component, named `saved_leaf`. Both links have this `LinkedComponentId`.

The plugins are the two example plugins of `rpp_testing` (`test_lib::ComponentPluginComplexPy`, `test_lib::ComponentPluginSimplePy`), registered by `setup_tmp_rpp_with_test_plugins` as in the tests of this repository. Nothing has to be registered by hand.

## Run

In the CI container of this repository (`rpp_common`, `rpp_plugin_registrator`, `rpp_orchestrator`, `rpp_py`, `rpp_cli`, `rpp_testing` installed with `pip install --no-deps -e`):

```
bash repro/linked_twice_in_list_slot/reproduce.sh
```

`reproduce.py` builds the parent with `ComponentRuntimeBuilder.build_component_from_path`.

## Result on develop (bfd2cbe)

```
LifecycleError: register ctl_main/saved_leaf failed: Component name must be nonempty and unique within its slot
```

The child is registered as `saved_leaf` (the name of the target), not `Fin 1`.

## Why

`resolve_component` (`rpp_py/context_builder.py:372`) returns the record of the link target, and `_build_native_component` names the child `record.name` (`:409`). Both links therefore produce a child called `saved_leaf`, and `ComponentManager.adopt` (`rpp_py/component_manager.py:153`) refuses the second one. The names `Fin 1` / `Fin 2` of the link records are never used.
