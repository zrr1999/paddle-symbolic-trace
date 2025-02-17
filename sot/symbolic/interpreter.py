from __future__ import annotations

from typing import TYPE_CHECKING

import paddle
from paddle.utils import to_sequence

from ..utils import InnerError, map_if, map_if_extend
from .statement_ir import SIRRuntimeCache, Symbol

if TYPE_CHECKING:
    from .statement_ir import Statement, StatementIR
    from .symbolic_context import SymbolicTraceContext


def replace_symbol(
    values: list[Symbol] | list[object], state: dict[str, Symbol]
):
    """
    Replaces Symbol objects with their corresponding values.

    Args:
        values: A list of values that may contain Symbol objects.
        state: A dict mapping Symbol names to their corresponding values.

    Returns:
        A new list with Symbol objects replaced by their corresponding values in the state dict.
    """
    # deal with list / map etc.
    values = map_if_extend(
        values,
        pred=lambda x: isinstance(x, Symbol),
        true_fn=lambda x: state[x.name],
        false_fn=lambda x: x,
    )
    return values


def _append_opstack_between(start, end, stack):
    # NOTE(xiongkun): we don't sync for speed. careful!!
    # [start, end)
    from paddle.fluid import core

    op_maker = core.op_proto_and_checker_maker
    callstack_attr_name = op_maker.kOpCreationCallstackAttrName()
    for op in for_each_ops_between(start, end):
        op._set_attr(callstack_attr_name, stack)


def for_each_ops_between(start, end):
    # NOTE(xiongkun): we don't sync for speed. careful!!
    # [start, end)
    program = paddle.static.default_main_program()
    ops = program.current_block().ops[start:end]
    yield from ops


def opnum_in_program():
    # NOTE(xiongkun): we don't sync for speed. careful!!
    program = paddle.static.default_main_program()
    return len(program.current_block().ops)


class Interpreter:
    """
    Interpreter is used to interpret and execute SIR.
    """

    def __init__(self, symbolic_context: SymbolicTraceContext):
        self._context = symbolic_context

    def get_sir(self, name: str) -> StatementIR:
        """
        Returns the StatementIR object by given name.

        Args:
            name: The name of the StatementIR.

        Returns:
            The StatementIR object with the given name.
        """
        return self._context.get_sir(name)

    def run_sir(self, name: str, state: dict[str, Symbol]):
        """
        Runs the StatementIR with the given name using the provided state.

        Args:
            name: The name of the given StatementIR to run.
            state: A dict mapping Symbol names to their corresponding values.

        Returns:
            A list of the Symbol of the StatementIR after execution.
        """
        SIR = self.get_sir(name)
        for stmt in SIR.statements:
            stmt: Statement
            before_stmt_opnum = opnum_in_program()
            inputs = replace_symbol(stmt.inputs, state)
            outs = getattr(self, stmt.type)(stmt, inputs)

            def _set(v, s):
                state[s.name] = v

            if len(to_sequence(outs)) != len(to_sequence(stmt.outputs)):
                raise InnerError("Number output mismatch, some error happen.")

            _append_opstack_between(
                before_stmt_opnum, opnum_in_program() + 1, stmt.stmt_stack
            )

            map_if(
                outs,
                stmt.outputs,
                pred=lambda v, s: isinstance(s, Symbol),
                true_fn=lambda v, s: _set(v, s),
                false_fn=lambda v, s: None,
            )
        # fetch outputs
        return replace_symbol(SIR.outputs, state)

    def call(self, stmt: Statement, inputs):
        SIR = self.get_sir(stmt.name)
        state = prepare_state(SIR, inputs)
        return self.run_sir(stmt.name, state)

    def api(self, stmt, inputs):
        args, kwargs = inputs
        return stmt.name(*args, **kwargs)

    def method(self, stmt, inputs):
        args, kwargs = inputs
        var = args[0]
        return getattr(var, stmt.name)(*args[1:], **kwargs)

    def layer(self, stmt, inputs):
        args, kwargs = inputs
        layer, args = args[0], args[1:]
        return layer(*args, **kwargs)

    def delete(self, stmt, inputs):
        pass


def compile_sir(context: SymbolicTraceContext, name: str):
    """
    Compile a SIR to a new function

    Args:
        context: The context to compile
        name: The name of the sir to compile

    """

    @paddle.jit.not_to_static
    def wrapper(args):
        """
        This function will be decorated by paddle.to_static.
        so the args is variables, not eager tensors.
        """
        interpreter = Interpreter(context)
        SIR = interpreter.get_sir(name)
        state = prepare_state(SIR, args)
        return interpreter.run_sir(name, state)

    return wrapper


def prepare_state(SIR, inputs):
    state = {}

    # update free vars if exsits
    if SIRRuntimeCache().has_key(SIR.name):
        free_var_seeker = SIRRuntimeCache().get_free_vars(SIR.name)
        if free_var_seeker:
            state = free_var_seeker()

    # bind inputs
    for sir_inp, inp in zip(SIR.inputs, inputs):
        state[sir_inp.name] = inp

    return state
