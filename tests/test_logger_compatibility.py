from rpp_common import logger as shared
from rpp_py import logger as legacy


def test_legacy_logger_imports_share_the_common_implementation():
    assert legacy.RppLogger is shared.RppLogger
    assert legacy.LoggerOptions is shared.LoggerOptions
    assert legacy.LogLevel is shared.LogLevel
