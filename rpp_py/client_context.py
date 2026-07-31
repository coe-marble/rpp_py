import asyncio

import capnp

from rpp_py.capnp_runtime import CapnpRuntime


class ClientContext:
    def __init__(self, host: str, port: int, runtime=None):
        self.host = host
        self.port = port
        self._stream = None
        self._rpc_client = None
        if runtime is None:
            self.runtime = CapnpRuntime()
        else:
            self.runtime = runtime



    def get_runtime(self):
        return self.runtime

    def get_client(self):
        return self._rpc_client.bootstrap()

    async def start(self, timeout=None): # in ms
        await self.runtime.start()
        while True:
            try:
                self._stream = await capnp.AsyncIoStream.create_connection(self.host, self.port)
                self._rpc_client = capnp.TwoPartyClient(self._stream)
                return True
            except Exception:
                if timeout is not None and timeout <= 0:
                    return False
                if timeout is not None:
                    timeout -= 100  # Decrease timeout by 100 ms
                await asyncio.sleep(0.1)  # Wait for 100 ms before retrying


    async def stop(self):
        await self.runtime.stop()


    async def __aenter__(self):
        await self.runtime.start()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.runtime.stop()
