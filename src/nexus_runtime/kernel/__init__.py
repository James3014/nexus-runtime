from .runtime import build_runtime, RuntimeExports
from .events.transport import build_transport, TransportExports
from .ports import RuntimeBindings, TransportBindings

def bind_runtime(values): return RuntimeBindings(values)
def bind_transport(values): return TransportBindings(values)
