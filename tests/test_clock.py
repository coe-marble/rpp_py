import pytest

from rpp_py.clock import MockClock, RealtimeClock


@pytest.mark.parametrize("seconds", [-1.0, float("inf"), float("nan"), (1 << 64) / 1e9])
def test_mock_clock_rejects_unrepresentable_time_without_changing_it(seconds):
    clock = MockClock()
    clock.set_time(1.0)

    with pytest.raises(ValueError, match="unsigned 64-bit nanoseconds"):
        clock.set_time(seconds)

    assert clock.now_nanoseconds() == 1_000_000_000


def test_mock_clock_can_move_backwards_without_crossing_zero():
    clock = MockClock()
    clock.set_time(1.0)

    with pytest.raises(ValueError):
        clock.elapse(-2.0)
    assert clock.now_nanoseconds() == 1_000_000_000

    clock.elapse(-0.5)
    assert clock.now_nanoseconds() == 500_000_000


@pytest.mark.parametrize("timestamp", [(1 << 63) + 1, (1 << 64) - 1])
def test_realtime_clock_preserves_integer_nanoseconds(monkeypatch, timestamp):
    clock = RealtimeClock()
    clock.use_ros_time = False
    monkeypatch.setattr("rpp_py.clock.time.time_ns", lambda: timestamp)

    assert clock.now_nanoseconds() == timestamp
