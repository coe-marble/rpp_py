from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import socket
import subprocess
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


class ExternalComponentProcess:
    """Own one external component server shared by adapter clients."""

    def __init__(
            self, *, components: list[tuple[Any, ExternalComponentSpec]],
            rpp_home: str | Path,
            command: str,
            host: str = "127.0.0.1", port: int | None = None,
            startup_timeout_ms: int = 5000,
            logger: RppLogger | None = None) -> None:
        if not components:
            raise ValueError("A component process requires at least one component.")

        self.components = list(components)
        self.command = command
        self.rpp_home = Path(rpp_home)
        self.host = host
        self.port = port or _get_available_port()
        self.startup_timeout_ms = startup_timeout_ms
        self.logger = logger or RppLogger()

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
        for _, component in self.components:
            args.extend([
                "--path", str(component.component_path),
                "--plugin", component.plugin_name,
                "--conn", component.connection_name,
            ])
        return args

    async def start(self) -> None:
        if self._started:
            return

        try:
            self.logger.debug(f"Launching component process: {self.command_args!r}")
            self._process = subprocess.Popen(self.command_args)
            self._executor = ComponentCallExecutor()
            self._executor.start()
            await asyncio.wrap_future(
                self._executor.submit(self._connect_adapters()))
            self._started = True
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self._executor is not None:
            try:
                await asyncio.wrap_future(
                    self._executor.submit(self._disconnect_adapters()))
            finally:
                self._executor.stop()
                self._executor = None

        if self._process is not None:
            try:
                await asyncio.to_thread(self._process.wait, timeout=2)
            except subprocess.TimeoutExpired:
                self._process.terminate()
                try:
                    await asyncio.to_thread(self._process.wait, timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    await asyncio.to_thread(self._process.wait)
            self._process = None

        self._started = False

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

    async def _disconnect_adapters(self) -> None:
        if self._runtime_client is not None:
            try:
                await self._runtime_client.shutdown()
            except Exception:
                pass
            await self._runtime_client.disconnect()
            self._runtime_client = None

        for adapter_client, _ in reversed(self.components):
            await adapter_client._disconnect_adapter_client_on_executor__()

        if self._runtime_client_context is not None:
            await self._runtime_client_context.stop()
            self._runtime_client_context = None

    async def release(self) -> None:
        """Release one owner without stopping a shared process prematurely."""
        if self._active_handles == 0:
            return
        self._active_handles -= 1
        if self._active_handles == 0:
            await self.stop()


class CppExternalComponentProcess(ExternalComponentProcess):
    """External process that locates the installed C++ component server."""

    _command = "rpp_component_server_cpp"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(command=self.resolve_command(), **kwargs)

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
    def __init__(self, process: ExternalComponentProcess) -> None:
        self._process = process
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        await self._process.start()
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        await self._process.release()


class ExternalComponentProcessRegistry:
    """Create component subprocess owners by target language."""

    def __init__(self) -> None:
        self._factories: dict[str, tuple[Callable[..., Any], bool]] = {}
        self._shared_processes: dict[str, ExternalComponentProcess] = {}

    def register(
            self, language: str, factory: Callable[..., Any], *,
            share_process: bool = False) -> None:
        normalized_language = language.strip().lower()
        if not normalized_language:
            raise ValueError("Component process language cannot be empty.")
        self._factories[normalized_language] = (factory, share_process)

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
                        kwargs["connection_name"],
                    ),
                )
            process._active_handles += 1
            return _SharedExternalComponentProcessHandle(process)
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
