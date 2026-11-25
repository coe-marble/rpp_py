import math
import time


_MAX_TIMESTAMP_NS = (1 << 64) - 1


def _checked_nanoseconds(nanoseconds: int) -> int:
    if not 0 <= nanoseconds <= _MAX_TIMESTAMP_NS:
        raise ValueError("Clock time must fit unsigned 64-bit nanoseconds")
    return nanoseconds


def _seconds_to_nanoseconds(seconds: float) -> int:
    nanoseconds = seconds * 1e9
    if not math.isfinite(nanoseconds) or nanoseconds < 0:
        raise ValueError("Clock time must fit unsigned 64-bit nanoseconds")
    return _checked_nanoseconds(int(nanoseconds))


class ClockOptions:
    def __init__(self, clock_type='realtime', initial_time=0.0):
        self.clock_type = clock_type
        self.initial_time = initial_time

class Clock:
    def __init__(self, clock_options=None):
        if clock_options is None:
            clock_options = ClockOptions()
        self.clock_type = clock_options.clock_type
        self.initial_time = clock_options.initial_time

    def now_seconds(self):
        raise NotImplementedError("Subclasses should implement this method.")

    def now_nanoseconds(self) -> int:
        """Return unsigned 64-bit nanoseconds in this clock's domain."""
        raise NotImplementedError("Subclasses should implement this method.")

class RealtimeClock(Clock):
    def __init__(self, clock_options=None):
        super().__init__(clock_options)

        try:
            import rclpy
            self.use_ros_time = rclpy.ok()
            from rclpy.clock import Clock as RclpyClock
            self._ros_clock = RclpyClock()
        except ImportError:
            self.use_ros_time = False

    def now_seconds(self):
        if self.use_ros_time:
            return self._ros_clock.now().nanoseconds * 1e-9
        else:
            return time.time()

    def now_nanoseconds(self) -> int:
        if self.use_ros_time:
            return _checked_nanoseconds(self._ros_clock.now().nanoseconds)
        else:
            return _checked_nanoseconds(time.time_ns())



class MockClock(Clock):
    def __init__(self, clock_options=None):
        super().__init__(clock_options)
        self.set_time(self.initial_time)

    def set_time(self, new_time):
        _seconds_to_nanoseconds(new_time)
        self.current_time = new_time

    def now_seconds(self):
        return self.current_time

    def elapse(self, dt):
        self.set_time(self.current_time + dt)
        return self.current_time

    def now_nanoseconds(self) -> int:
        return _seconds_to_nanoseconds(self.current_time)


def clock_factory(clock_options=None):
    if clock_options is None:
        clock_options = ClockOptions()

    if clock_options.clock_type == 'realtime':
        return RealtimeClock(clock_options)
    elif clock_options.clock_type == 'mock':
        return MockClock(clock_options)
    else:
        raise ValueError(f"Invalid clock type: {clock_options.clock_type}. Valid types are 'realtime' and 'mock'.")
