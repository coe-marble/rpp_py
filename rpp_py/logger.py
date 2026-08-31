import inspect

class LogLevel:
    DEBUG = 0
    INFO = 1
    WARN = 2
    ERROR = 3

class LoggerOptions:
    def __init__(self,
            level=LogLevel.DEBUG, name="rpp_logger"):
        self.level = level
        self.name = name


class RppLogger:
    def __init__(self, options : LoggerOptions | str | None=None):
        if options is None:
            options = LoggerOptions()
        elif isinstance(options, str):
            options = LoggerOptions(name=options)
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
        try:
            caller_frame = frame.f_back.f_back if frame and frame.f_back else None
            if caller_frame is None:
                return "<unknown>", 0
            return caller_frame.f_code.co_filename, caller_frame.f_lineno
        finally:
            del frame


    def debug(self, message):
        if self.options.level <= LogLevel.DEBUG:
            if self.use_ros_logging:
                self._ros_logger.debug(message)
            else:
                file, line = self._inspect_caller()
                print(f"[DEBUG] [{self.options.name}] ({file}:{line})\n{message}")

    def info(self, message):
        if self.options.level <= LogLevel.INFO:
            if self.use_ros_logging:
                self._ros_logger.info(message)
            else:
                file, line = self._inspect_caller()
                print(f"[INFO] [{self.options.name}] ({file}:{line})\n{message}")

    def warn(self, message):
        if self.options.level <= LogLevel.WARN:
            if self.use_ros_logging:
                self._ros_logger.warn(message)
            else:
                file, line = self._inspect_caller()
                print(f"[WARN] [{self.options.name}] ({file}:{line})\n{message}")

    def warning(self, message):
        self.warn(message)

    def error(self, message):
        if self.options.level <= LogLevel.ERROR:
            if self.use_ros_logging:
                self._ros_logger.error(message)
            else:
                file, line = self._inspect_caller()
                print(f"[ERROR] [{self.options.name}] ({file}:{line})\n{message}")
