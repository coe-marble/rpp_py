
class PluginHandler:
    def __init__(self, plugin_path: str):
        self.plugin_path = plugin_path
        self.plugin_module = None