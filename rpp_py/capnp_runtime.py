import capnp
class CapnpRuntime:

    def __init__(self):
        self.kj : capnp.KjLoop = None

    async def start(self):
        if self.kj is None:
            self.kj = capnp.kj_loop()
            await self.kj.__aenter__()

    async def stop(self):
        if self.kj is not None:
            await self.kj.__aexit__(None, None, None)
            self.kj = None

    async def sleep(self, seconds):
        if self.kj is not None:
            await self.kj.sleep(seconds)
        else:
            raise RuntimeError("CapnpRuntime is not running. Call start() before sleep().")

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.stop()