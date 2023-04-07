import contextlib
import paddle
from .opcode_translator import eval_frame_callback
from .symbolic_trace import SymbolicTraceContext
from .proxy_tensor import ProxyTensorContext, ProxyTensor
from .statement_ir import Symbol

def symbolic_trace(func):
    def wrapped(*args, **kw):
        ProxyTensorContext().reset()
        with SymbolicTraceContext() as ctx:
            paddle.fluid.core.set_eval_frame(eval_frame_callback)
            returns = func(*args, **kw)
            paddle.fluid.core.set_eval_frame(None)

        # TODO( output analysis, we can get out symbols here. )
        if returns is None:
            return None
        return SymbolicTraceContext().start_compile(
            ProxyTensorContext().get_runtime(),
            outputs=paddle.utils.map_structure(lambda x: Symbol(x.name), paddle.utils.to_sequence(returns)),
            is_return=True
        )
    return wrapped