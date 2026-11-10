import pytest

from rpp_py.lifecycle import CallbackResult, LifecycleError


def test_require_success_accepts_successful_result():
    assert CallbackResult().require_success() is None


def test_require_success_uses_failure_message_without_requiring_a_label():
    with pytest.raises(LifecycleError, match="^state rejected$"):
        CallbackResult.failure("state rejected").require_success()


def test_require_success_supplies_message_for_empty_failure():
    with pytest.raises(LifecycleError, match="^Operation failed$"):
        CallbackResult.failure("").require_success()


def test_require_success_reports_operation_and_failure_reason():
    result = CallbackResult.failure("state rejected")

    with pytest.raises(LifecycleError, match="on_activate.*state rejected"):
        result.require_success("on_activate")
