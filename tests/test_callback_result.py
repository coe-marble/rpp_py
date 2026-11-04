import pytest

from rpp_common.lifecycle import CallbackResult, LifecycleError


def test_require_success_accepts_successful_result():
    assert CallbackResult().require_success("on_activate") is None


def test_require_success_reports_operation_and_failure_reason():
    result = CallbackResult.failure("state rejected")

    with pytest.raises(LifecycleError, match="on_activate.*state rejected"):
        result.require_success("on_activate")
