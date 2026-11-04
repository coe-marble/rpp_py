"""Native lifecycle callbacks and typed, owned handover values."""

from dataclasses import dataclass, field
import importlib
import importlib.util
from unittest.mock import Mock

import pytest

from rpp_py.plugin import Plugin


@dataclass
class Estimate:
    position: list[float] = field(default_factory=list)


def lifecycle_contract():
    assert importlib.util.find_spec("rpp_common.lifecycle") is not None, (
        "Lifecycle contract is not implemented yet; this is the test-first milestone"
    )
    return importlib.import_module("rpp_common.lifecycle")


def localizer_type(api):
    class Localizer(Plugin):
        HANDOVER_STATE_TYPE = Estimate

        def __init__(self, position):
            self.estimate = Estimate(position)

        def on_configure(self, context):
            return api.CallbackResult()

        def on_activate(self, state=None):
            if state is not None:
                self.estimate = state
            return api.CallbackResult()

        def on_deactivate(self):
            return api.DeactivationResult(state=self.estimate)

    return Localizer


def test_snapshot_is_owned_and_can_activate_another_implementation():
    api = lifecycle_contract()
    Localizer = localizer_type(api)
    first = Localizer([1.0, 2.0])
    snapshot = first.deactivate_state().state
    first.estimate.position.append(99.0)
    del first
    class AlternativeLocalizer(Localizer):
        pass

    second = AlternativeLocalizer([])

    second.activate_state(snapshot)
    snapshot.position.append(100.0)

    assert second.estimate.position == [1.0, 2.0]


def test_cold_activation_is_distinct_from_a_default_estimate():
    api = lifecycle_contract()
    plugin = localizer_type(api)([3.0])
    plugin.activate_state()
    assert plugin.estimate.position == [3.0]
    plugin.activate_state(Estimate())
    assert plugin.estimate.position == []


def test_incompatible_state_is_rejected_before_callback():
    api = lifecycle_contract()
    plugin = localizer_type(api)([1.0])
    plugin.on_activate = Mock()
    with pytest.raises(TypeError, match="Expected handover state Estimate"):
        plugin.activate_state({"position": [2.0]})
    plugin.on_activate.assert_not_called()


def test_failed_deactivation_cannot_export_state():
    api = lifecycle_contract()
    plugin = localizer_type(api)([1.0])
    plugin.on_deactivate = Mock(return_value=api.DeactivationResult(
        api.CallbackResult.failure("still updating"), plugin.estimate))
    with pytest.raises(api.LifecycleError, match="still updating"):
        plugin.deactivate_state()


def test_stateless_plugin_rejects_a_handover_payload():
    lifecycle_contract()
    with pytest.raises(TypeError, match="no state"):
        Plugin().activate_state(Estimate())


def test_stateless_callbacks_receive_no_state_argument():
    api = lifecycle_contract()
    plugin = Plugin()
    plugin.on_activate = Mock(return_value=api.CallbackResult())
    plugin.on_deactivate = Mock(return_value=api.CallbackResult())

    assert plugin.activate_state().success
    assert plugin.deactivate_state().state is None

    plugin.on_activate.assert_called_once_with()
    plugin.on_deactivate.assert_called_once_with()


@pytest.mark.parametrize("phase", ["activate", "deactivate"])
@pytest.mark.parametrize("kind", ["missing-result", "failure", "exception"])
def test_dispatch_reports_callback_errors(phase, kind):
    api = lifecycle_contract()
    plugin = localizer_type(api)([])
    if kind == "exception":
        callback = Mock(side_effect=RuntimeError("callback exploded"))
    else:
        result = None if kind == "missing-result" else api.CallbackResult.failure("rejected")
        if phase == "deactivate" and result is not None:
            result = api.DeactivationResult(result)
        callback = Mock(return_value=result)
    setattr(plugin, f"on_{phase}", callback)

    with pytest.raises(api.LifecycleError, match=f"on_{phase}"):
        getattr(plugin, f"{phase}_state")()


def test_wrong_export_type_is_rejected():
    api = lifecycle_contract()
    plugin = localizer_type(api)([])
    plugin.on_deactivate = Mock(return_value=api.DeactivationResult(state="wrong"))
    with pytest.raises(TypeError, match="Expected handover state Estimate"):
        plugin.deactivate_state()


def test_configure_visits_children_before_parent():
    api = lifecycle_contract()
    from rpp_py.context import ComponentContext

    events = []

    class Recorder(Plugin):
        def __init__(self, name):
            self.name = name

        def on_configure(self, context):
            events.append(self.name)
            return api.CallbackResult()

    child = ComponentContext(instance=Recorder("child"), logger=Mock())
    parent = ComponentContext(instance=Recorder("parent"),
                              subcomponents={"children": [child]}, logger=Mock())

    assert parent.configure().success
    assert events == ["child", "parent"]


@pytest.mark.parametrize("kind", ["missing-result", "failure"])
def test_configure_rejects_missing_or_failed_callback_result(kind):
    api = lifecycle_contract()
    from rpp_py.context import ComponentContext

    result = None if kind == "missing-result" else api.CallbackResult.failure("rejected")
    plugin = Mock(on_configure=Mock(return_value=result))
    context = ComponentContext(instance=plugin, logger=Mock())
    with pytest.raises(api.LifecycleError):
        context.configure()
