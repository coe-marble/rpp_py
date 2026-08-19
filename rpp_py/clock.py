import time


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

    def now_nanoseconds(self):
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

    def now_nanoseconds(self):
        if self.use_ros_time:
            return self._ros_clock.now().nanoseconds
        else:
            return int(time.time() * 1e9)



class MockClock(Clock):
    def __init__(self, clock_options=None):
        super().__init__(clock_options)
        self.current_time = self.initial_time

    def set_time(self, new_time):
        self.current_time = new_time

    def now_seconds(self):
        return self.current_time

    def elapse(self, dt):
        self.current_time += dt
        return self.current_time

    def now_nanoseconds(self):
        return int(self.current_time * 1e9)


def clock_factory(clock_options=None):
    if clock_options is None:
        clock_options = ClockOptions()

    if clock_options.clock_type == 'realtime':
        return RealtimeClock(clock_options)
    elif clock_options.clock_type == 'mock':
        return MockClock(clock_options)
    else:
        raise ValueError(f"Invalid clock type: {clock_options.clock_type}. Valid types are 'realtime' and 'mock'.")