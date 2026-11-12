from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import subprocess
from functools import partial
from dataclasses import dataclass
from typing import Any, Callable

from rpp_plugin_registrator.registry_config import get_setting
from rpp_py.component_call_executor import ComponentCallExecutor
from rpp_py.rpp_runtime_client_context import RppRuntimeClientContext
from rpp_py.plugin_runtime import PluginRuntimeClient
from rpp_py.logger import RppLogger


def _get_available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        return int(sock.getsockname()[1])


@dataclass(frozen=True)
class ExternalComponentSpec:
    component_path: str | Path
    plugin_name: str
    connection_name: str
    # C++ component servers construct a host-owned slot per connection and
    # therefore need its declaration on the command line.  Python servers
    # derive it from the loaded plugin metadata instead.
    slot_declaration: str = ""


class ExternalComponentProcess:
    """Own one external component server shared by adapter clients."""

    def __init__(
            self, *, components: list[tuple[Any, ExternalComponentSpec]],
            rpp_home: str | Path,
            command: str,
            host: str = "127.0.0.1", port: int | None = None,
            startup_timeout_ms: int = 5000,
            logger: RppLogger | None = None, allow_dynamic_components=False) -> None:
        if not components:
            raise ValueError("A component process requires at least one component.")

        self.components = list(components)
        self.command = command
        self.rpp_home = Path(rpp_home)
        self.host = host
        self.port = port or _get_available_port()
        self.startup_timeout_ms = startup_timeout_ms
        self.logger = logger or RppLogger()
        self.allow_dynamic_components = allow_dynamic_components

        self._process: subprocess.Popen | None = None
        self._runtime_client_context: RppRuntimeClientContext | None = None
        self._runtime_client: PluginRuntimeClient | None = None
        self._executor: ComponentCallExecutor | None = None
        self._started = False
        self._active_handles = 0

    def add_component(
            self, adapter_client: Any, component: ExternalComponentSpec) -> None:
        """Add an adapter and component before the shared process starts."""
        if self._started or self._process is not None:
            raise RuntimeError("Cannot add a component after process startup.")
        self.components.append((adapter_client, component))

    @property
    def command_args(self) -> list[str]:
        args = [self.command, "--host", self.host, "--port", str(self.port),
                "--home", str(self.rpp_home)]
        if self.allow_dynamic_components:
            args.append("--allow-dynamic-components")
        for _, component in self.components:
            args.extend([
                "--path", str(component.component_path),
                "--plugin", component.plugin_name,
                "--conn", component.connection_name,
            ])
        return args

    def prepare(self) -> None:
        if self._started:
            return

        try:
            self.logger.debug(f"Launching component process: {self.command_args!r}")
            self._process = subprocess.Popen(self.command_args)
            self._executor = ComponentCallExecutor()
            self._executor.start()
            self._executor.call(self._connect_adapters())
            self._started = True
        except Exception:
            try:
                self.close()
            except Exception as error:
                self.logger.error(f"Component process startup rollback failed: {error!r}")
            raise

    def close(self) -> None:
        errors = []
        if self._executor is not None:
            try:
                self._executor.call(self._disconnect_adapters())
            except Exception as error:
                errors.append(error)
            try:
                self._executor.stop()
                self._executor = None
            except Exception as error:
                errors.append(error)

        if self._process is not None:
            try:
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                        self._process.wait()
                self._process = None
            except Exception as error:
                errors.append(error)

        self._started = False
        if errors:
            for error in errors[1:]:
                self.logger.error(f"Additional component process shutdown failure: {error!r}")
            raise errors[0]

    async def _connect_adapters(self) -> None:
        self._runtime_client_context = RppRuntimeClientContext(
            host=self.host, port=self.port)
        connected, error = await self._runtime_client_context.start(
            self.startup_timeout_ms)
        if not connected:
            raise RuntimeError("Failed to connect to component process.") from error
        self.logger.debug(
            f"Component process runtime connected at {self.host}:{self.port}.")

        for adapter_client, _ in self.components:
            await adapter_client._connect_adapter_client_with_executor__(
                context=self._runtime_client_context,
                executor=self._executor)
        self._runtime_client = PluginRuntimeClient(logger=self.logger)
        await self._runtime_client.connect(context=self._runtime_client_context)

    def configure_component(self, name):
        if not self._started:
            raise RuntimeError("Component process is not prepared")
        return self._executor.call(self._runtime_client.configure_component(name))

    def cleanup_component(self, name):
        if not self._started:
            raise RuntimeError("Component process is not prepared")
        return self._executor.call(self._runtime_client.cleanup_component(name))

    def activate_component(self, name, state=None):
        if not self._started:
            raise RuntimeError("Component process is not prepared")
        return self._executor.call(self._runtime_client.activate_component(
            name, state, adapter=self._adapter_for(name)))

    def deactivate_component(self, name):
        if not self._started:
            raise RuntimeError("Component process is not prepared")
        return self._executor.call(self._runtime_client.deactivate_component(
            name, adapter=self._adapter_for(name)))

    def _adapter_for(self, connection_name):
        for adapter, component in self.components:
            if component.connection_name == connection_name:
                return adapter
        raise RuntimeError(f"No adapter registered for component connection {connection_name!r}")

    async def _disconnect_adapters(self) -> None:
        errors = []
        if self._runtime_client is not None:
            try:
                await self._runtime_client.shutdown()
            except Exception as error:
                self.logger.warn(f"Component runtime shutdown request failed: {error!r}")
            try:
                await self._runtime_client.disconnect()
            except Exception as error:
                errors.append(error)
            finally:
                self._runtime_client = None

        for adapter_client, _ in reversed(self.components):
            try:
                await adapter_client._disconnect_adapter_client_on_executor__()
            except Exception as error:
                errors.append(error)

        if self._runtime_client_context is not None:
            try:
                await self._runtime_client_context.stop()
            except Exception as error:
                errors.append(error)
            finally:
                self._runtime_client_context = None
        if errors:
            for error in errors[1:]:
                self.logger.error(f"Additional component disconnection failure: {error!r}")
            raise errors[0]

    def release(self) -> None:
        """Retain the last owner if shutdown fails so close can be retried."""
        if self._active_handles == 0:
            return
        if self._active_handles == 1:
            self.close()
        self._active_handles -= 1


class CppExternalComponentProcess(ExternalComponentProcess):
    """External process that locates the installed C++ component server."""

    _command = "rpp_component_server_cpp"

    def __init__(self, *, command: str | None = None, **kwargs: Any) -> None:
        super().__init__(command=command or self.resolve_command(), **kwargs)

    @property
    def command_args(self) -> list[str]:
        args = [self.command, "--host", self.host, "--port", str(self.port),
                "--home", str(self.rpp_home)]
        if self.allow_dynamic_components:
            args.append("--allow-dynamic-components")
        for _, component in self.components:
            if not component.slot_declaration:
                raise ValueError(
                    "A C++ hosted component requires a slot declaration.")
            args.extend([
                "--path", str(component.component_path),
                "--plugin", component.plugin_name,
                "--conn", component.connection_name,
                "--slot", component.slot_declaration,
            ])
        return args

    @classmethod
    def resolve_command(cls) -> str:
        command_path = shutil.which(cls._command)
        if command_path is not None:
            return command_path

        if not get_setting("USE_ROS2_COMPILATION"):
            raise FileNotFoundError(
                f"C++ component server '{cls._command}' was not found on PATH. "
                "Enable USE_ROS2_COMPILATION to search RPP_CPP_CORE_PATH."
            )

        rpp_cpp_core_path = get_setting("RPP_CPP_CORE_PATH")
        if not rpp_cpp_core_path:
            raise FileNotFoundError(
                "RPP_CPP_CORE_PATH is not configured while resolving the "
                f"C++ component server '{cls._command}'."
            ) from None

        executable = Path(rpp_cpp_core_path) / "component_server_cpp"
        if executable.is_file() and os.access(executable, os.X_OK):
            return str(executable)
        raise FileNotFoundError(
            "C++ component server is not executable at the configured "
            f"RPP_CPP_CORE_PATH: '{executable}'."
        )


class _SharedExternalComponentProcessHandle:
    def __init__(self, process: ExternalComponentProcess, connection_name: str) -> None:
        self._process = process
        self._connection_name = connection_name
        self._closed = False

    def prepare(self) -> None:
        if self._closed:
            raise RuntimeError("Cannot prepare a released component process handle.")
        self._process.prepare()

    def close(self) -> None:
        if self._closed:
            return
        self._process.release()
        self._closed = True

    def configure(self):
        if self._closed:
            raise RuntimeError("Cannot configure a released component process handle")
        return self._process.configure_component(self._connection_name)

    def cleanup(self):
        if self._closed:
            raise RuntimeError("Cannot clean up a released component process handle")
        return self._process.cleanup_component(self._connection_name)

    def activate(self, state=None):
        if self._closed:
            raise RuntimeError("Cannot activate a released component process handle")
        return self._process.activate_component(self._connection_name, state)

    def deactivate(self):
        if self._closed:
            raise RuntimeError("Cannot deactivate a released component process handle")
        return self._process.deactivate_component(self._connection_name)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class ExternalComponentProcessRegistry:
    """Create component subprocess owners by target language."""

    def __init__(self) -> None:
        self._factories: dict[str, tuple[Callable[..., Any], bool]] = {}
        self._shared_processes: dict[str, ExternalComponentProcess] = {}

    def begin_build(self) -> None:
        """Group hosts within this build; existing handles retain their owners."""
        self._shared_processes.clear()

    def clone(self):
        registry = ExternalComponentProcessRegistry()
        registry._factories = self._factories.copy()
        return registry

    def register(
            self, language: str, factory: Callable[..., Any], *,
            share_process: bool = False) -> None:
        normalized_language = language.strip().lower()
        if not normalized_language:
            raise ValueError("Component process language cannot be empty.")
        self._factories[normalized_language] = (factory, share_process)

    def register_server(self, language: str, command: str) -> None:
        """Register a server executable without launching it."""
        if not language.strip() or not command.strip():
            raise ValueError("External component server language and command are required")
        process_type = (CppExternalComponentProcess if language.strip().lower() == "cpp"
                        else ExternalComponentProcess)
        self.register(language, partial(process_type, command=command), share_process=True)

    def create(self, language: str, **kwargs):
        normalized_language = language.strip().lower()
        registration = self._factories.get(normalized_language)
        if registration is None:
            raise RuntimeError(
                "No component process is registered for source language "
                f"'{language}'."
            )
        factory, share_process = registration
        if share_process:
            process = self._shared_processes.get(normalized_language)
            if process is None:
                process = factory(**self._process_factory_kwargs(kwargs))
                if not isinstance(process, ExternalComponentProcess):
                    raise TypeError(
                        "A shared component process factory must return "
                        "an ExternalComponentProcess."
                    )
                self._shared_processes[normalized_language] = process
            else:
                process.add_component(
                    kwargs["adapter_client"],
                    ExternalComponentSpec(
                        kwargs["component_path"], kwargs["plugin_name"],
                        kwargs["connection_name"], kwargs.get("slot_declaration", ""),
                    ),
                )
            process._active_handles += 1
            return _SharedExternalComponentProcessHandle(process, kwargs["connection_name"])
        return factory(**kwargs)

    @staticmethod
    def _process_factory_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
        factory_kwargs = dict(kwargs)
        factory_kwargs["components"] = [(
            factory_kwargs.pop("adapter_client"),
            ExternalComponentSpec(
                factory_kwargs.pop("component_path"),
                factory_kwargs.pop("plugin_name"),
                factory_kwargs.pop("connection_name"),
                factory_kwargs.pop("slot_declaration", ""),
            ),
        )]
        return factory_kwargs


def default_external_component_process_registry() \
        -> ExternalComponentProcessRegistry:
    registry = ExternalComponentProcessRegistry()
    registry.register(
        "cpp",
        CppExternalComponentProcess,
        share_process=True,
    )
    return registry
