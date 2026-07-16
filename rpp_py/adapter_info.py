

from dataclasses import dataclass
from rpp_common import RPP_Plugin


@dataclass
class AdapterServerParams:
    """Information about the adapter server."""
    host: str
    port: int
    backend: RPP_Plugin
    plugin_name: str
    name: str = None

@dataclass
class AdapterClientParams:
    """Information about the adapter client."""
    host: str
    port: int
    name: str = None

class AdapterServerInfo:
    """Information about the adapter server."""
    def __init__(self,
            name: str=None, plugin_name: str = None,
            plugin_type: str = None, created_at: str = None):
        self.name = name
        self.plugin_name = plugin_name
        self.plugin_type = plugin_type
        self.created_at = created_at

class AdapterClientInfo:
    """Information about the adapter client."""
    def __init__(self,
            name: str=None, plugin_name: str = None,
            plugin_type: str = None, connected_at: str = None):
        self.name = name
        self.plugin_name = plugin_name
        self.plugin_type = plugin_type
        self.connected_at = connected_at