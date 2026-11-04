from __future__ import annotations
from copy import deepcopy

from rpp_py.lifecycle import CallbackResult, DeactivationResult, LifecycleError
from rpp_common.parameter_description import ParameterDescription


class Plugin:
    """Plugin callbacks with validated, owned application-state handover.

    Stateful plugin-type bases set HANDOVER_STATE_TYPE and override on_activate
    to accept an optional instance of that type. Stateless callbacks take no
    arguments. Transition ordering belongs to the manager, not this dispatcher.
    """

    HANDOVER_STATE_TYPE: type | None = None

    def on_configure(self, context) -> CallbackResult:
        return CallbackResult()

    def on_cleanup(self) -> CallbackResult:
        return CallbackResult()

    def configure(self, context) -> CallbackResult:
        result = Plugin._invoke_lifecycle("on_configure", self.on_configure, context)
        Plugin._require_callback_result(result, "on_configure")
        return result

    def cleanup(self) -> CallbackResult:
        result = Plugin._invoke_lifecycle("on_cleanup", self.on_cleanup)
        Plugin._require_callback_result(result, "on_cleanup")
        return result

    def on_activate(self) -> CallbackResult:
        return CallbackResult()

    def on_deactivate(self) -> CallbackResult:
        return CallbackResult()

    def _validate_state(self, state):
        if state is None:
            return
        if self.HANDOVER_STATE_TYPE is None:
            raise TypeError("This plugin accepts no state")
        if not isinstance(state, self.HANDOVER_STATE_TYPE):
            raise TypeError(f"Expected handover state {self.HANDOVER_STATE_TYPE.__name__}")

    @staticmethod
    def _invoke_lifecycle(operation, callback, *args):
        try:
            return callback(*args)
        except Exception as error:
            raise LifecycleError(f"{operation} failed: {error}") from error

    @staticmethod
    def _require_callback_result(result, operation):
        if not isinstance(result, CallbackResult):
            raise LifecycleError(f"{operation} must return CallbackResult")
        result.require_success(operation)

    def activate_state(self, state=None) -> CallbackResult:
        self._validate_state(state)
        args = () if self.HANDOVER_STATE_TYPE is None else (deepcopy(state),)
        result = self._invoke_lifecycle("on_activate", self.on_activate, *args)
        self._require_callback_result(result, "on_activate")
        return result

    def deactivate_state(self) -> DeactivationResult:
        result = self._invoke_lifecycle("on_deactivate", self.on_deactivate)
        if self.HANDOVER_STATE_TYPE is None:
            self._require_callback_result(result, "on_deactivate")
            return DeactivationResult(result)
        if not isinstance(result, DeactivationResult):
            raise LifecycleError("on_deactivate must return DeactivationResult")
        self._require_callback_result(result.result, "on_deactivate")
        self._validate_state(result.state)
        return DeactivationResult(result.result, deepcopy(result.state))

    def reset(self):
        pass

