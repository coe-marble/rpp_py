import inspect

class LogLevel:
    DEBUG = 0
    INFO = 1
    WARN = 2
    ERROR = 3

class LoggerOptions:
    def __init__(self,
            level=LogLevel.INFO, name="rpp_logger"):
        self.level = level
        self.name = name


class RppLogger:
    def __init__(self, options=None):
        if options is None:
            options = LoggerOptions()
        self.options = options

        try:
            import rclpy
            self.use_ros_logging = rclpy.ok()
            from rclpy.logging import get_logger
            self._ros_logger = get_logger(self.options.name)
        except ImportError:
            self.use_ros_logging = False

    def _inspect_caller(self):
        frame = inspect.currentframe()
        caller_frame = frame.f_back.f_back
        file = caller_frame.filename  # Dohvaća putanju/ime datoteke pozivatelja
        line = caller_frame.lineno
        return file, line


    def debug(self, message):
        if self.options.level <= LogLevel.DEBUG:
            if self.use_ros_logging:
                self._ros_logger.debug(message)
            else:
                file, line = self._inspect_caller()
                print(f"[DEBUG] ({file}:{line})\n{message}")

    def info(self, message):
        if self.options.level <= LogLevel.INFO:
            if self.use_ros_logging:
                self._ros_logger.info(message)
            else:
                file, line = self._inspect_caller()
                print(f"[INFO] ({file}:{line})\n{message}")

    def warn(self, message):
        if self.options.level <= LogLevel.WARN:
            if self.use_ros_logging:
                self._ros_logger.warn(message)
            else:
                file, line = self._inspect_caller()
                print(f"[WARN] ({file}:{line})\n{message}")

    def warning(self, message):
        self.warn(message)

    def error(self, message):
        if self.options.level <= LogLevel.ERROR:
            if self.use_ros_logging:
                self._ros_logger.error(message)
            else:
                file, line = self._inspect_caller()
                print(f"[ERROR] ({file}:{line})\n{message}")
