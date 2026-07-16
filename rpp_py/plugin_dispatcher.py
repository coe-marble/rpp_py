

class PluginDispatcher:
    def __init__(self, plugin_path: str):
        super().__init__(plugin_path)
        self.plugin_module = None
