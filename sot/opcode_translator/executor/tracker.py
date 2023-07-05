from __future__ import annotations

import builtins
from typing import TYPE_CHECKING

from ...utils import InnerError, NameGenerator
from .guard import StringifyExpression, union_free_vars

if TYPE_CHECKING:
    from .pycode_generator import PyCodeGen
    from .variables import VariableBase


class Tracker:
    """
    Tracker is a base class responsible for tracking variables or objects in Python code.
    It is used to identify how a variable is derived from the initial state of the frame.

    Args:
        inputs: The list of variables to be tracked.

    Note:
        It serves as an abstract class and should not be instantiated directly.
    """

    inputs: list[VariableBase]
    name_generator = NameGenerator("tracker_")

    def __init__(self, inputs: list[VariableBase]):
        self.inputs = inputs
        self.id = Tracker.name_generator.next()

    def gen_instructions(self, codegen: PyCodeGen) -> None:
        """
        Generate instructions based on the tracked variables.

        Args:
            codegen (PyCodeGen): An instance of PyCodeGen to generate instructions.
        """
        raise NotImplementedError()

    def trace_value_from_frame(self) -> StringifyExpression:
        """
        Trace the value of the tracked variables from the frame. It used for generating the guard.

        Returns:
            The value of the tracked variables.
        """
        raise NotImplementedError()

    def is_traceable(self) -> bool:
        """
        Determine if all the tracked variables can be traced from the frame.

        Returns:
            bool, True if all tracked variables are traceable, False otherwise.
        """
        for input in self.inputs:
            if not input.tracker.is_traceable():
                return False
        return True


class DummyTracker(Tracker):
    """
    DummyTracker is a subclass of Tracker that specifically tracks variables cannot be reproduced from the frame.
    It is mostly generated by complex operations (instructions).

    Args:
        inputs (list[VariableBase]): The input variables associated with the generated variables.
    """

    def __init__(self, inputs: list[VariableBase]):
        super().__init__(inputs)

    def gen_instructions(self, codegen: PyCodeGen):
        raise InnerError("DummyTracker has no instructions")

    def trace_value_from_frame(self):
        raise InnerError("DummyTracker can't trace value from frame")

    def is_traceable(self):
        return False

    def __repr__(self) -> str:
        return f"DummyTracker(num_inputs={len(self.inputs)})"


class DanglingTracker(Tracker):
    """
    DanglingTracker is a subclass of Tracker that specifically tracks variables that are not in the frame.
    Variables whose tracker is DanglingTracker should not be placed on the stack, except for DummyVariable.
    DanglingTracker is often used in conjunction with BuiltinVariable to reuse the dispatch mechanism.

    Examples:
        >>> import operator
        >>> from sot.opcode_translator.executor.variables import BuiltinVariable, ConstantVariable
        >>> a = ConstantVariable.wrap_literal(1, None)
        >>> b = ConstantVariable.wrap_literal(2, None)
        >>> c = BuiltinVariable(operator.add, None, DanglingTracker())(a, b)
        >>> c.value
        3
    """

    def __init__(self):
        super().__init__([])

    def gen_instructions(self, codegen: PyCodeGen):
        raise InnerError("DanglingTracker has no instructions")

    def trace_value_from_frame(self):
        raise InnerError("DanglingTracker can't trace value from frame")

    def is_traceable(self):
        return False

    def __repr__(self) -> str:
        return "DanglingTracker()"


class LocalTracker(Tracker):
    """
    LocalTracker is a subclass of Tracker that specifically tracks variables from f_locals of frame.

    Args:
        name (str): The name of the variable in f_locals to be tracked.
    """

    def __init__(self, name: str):
        super().__init__([])
        self.name = name

    def gen_instructions(self, codegen: PyCodeGen) -> None:
        codegen.gen_load_fast(self.name)

    def trace_value_from_frame(self) -> StringifyExpression:
        return StringifyExpression(f"frame.f_locals['{self.name}']", {})

    def __repr__(self) -> str:
        return f"LocalTracker(name={self.name})"


class CellTracker(LocalTracker):
    def gen_instructions(self, codegen: PyCodeGen):
        codegen.gen_load_deref(self.name)

    def trace_value_from_frame(self):
        return StringifyExpression(f"frame.f_locals['{self.name}']", {})

    def __repr__(self) -> str:
        return f"CellTracker(name={self.name})"


class GlobalTracker(Tracker):
    """
    GlobalTracker is a subclass of Tracker that specifically tracks variables from f_globals of frame.

    Args:
        name (str): The name of the variable in f_globals to be tracked.
    """

    def __init__(self, name: str):
        super().__init__([])
        self.name = name

    def gen_instructions(self, codegen: PyCodeGen) -> None:
        codegen.gen_load_global(self.name)

    def trace_value_from_frame(self) -> StringifyExpression:
        return StringifyExpression(f"frame.f_globals['{self.name}']", {})

    def __repr__(self) -> str:
        return f"GlobalTracker(name={self.name})"


class BuiltinTracker(Tracker):
    """
    BuiltinTracker is a subclass of Tracker that specifically tracks variables from f_builtins of frame.

    Args:
        name (str): The name of the variable in f_builtins to be tracked.
    """

    def __init__(self, name: str):
        super().__init__([])
        self.name = name

    def gen_instructions(self, codegen: PyCodeGen) -> None:
        codegen.gen_load_global(self.name)

    def trace_value_from_frame(self) -> StringifyExpression:
        return StringifyExpression(
            f"builtins.__dict__[{self.name}]", {"builtins": builtins}
        )

    def __repr__(self) -> str:
        return f"BuiltinTracker(name={self.name})"


class ConstTracker(Tracker):
    """
    ConstTracker is a subclass of Tracker that specifically tracks a constant value.

    Args:
        value (Any): The value of the constant.
    """

    def __init__(self, value):
        super().__init__([])
        self.value = value

    def gen_instructions(self, codegen: PyCodeGen):
        codegen.gen_load_const(self.value)

    def trace_value_from_frame(self):
        return StringifyExpression(f"{self.value!r}", {})

    def __repr__(self) -> str:
        return f"ConstTracker(value={self.value})"


class GetAttrTracker(Tracker):
    """
    GetAttrTracker is a subclass of Tracker that specifically tracks the attribute access of an variable.

    Args:
        obj (VariableBase): The object whose attribute is to be tracked.
        attr (str): The attribute to be tracked.
    """

    def __init__(self, obj: VariableBase, attr: str):
        super().__init__([obj])
        self.obj = obj
        self.attr = attr

    def gen_instructions(self, codegen: PyCodeGen):
        self.obj.tracker.gen_instructions(codegen)
        codegen.gen_load_attr(self.attr)

    def trace_value_from_frame(self):
        obj_tracer = self.obj.tracker.trace_value_from_frame()
        if self.attr.isidentifier():
            expr = f"{obj_tracer.expr}.{self.attr}"
        else:
            expr = f"getattr({obj_tracer.expr}, '{self.attr}')"
        return StringifyExpression(
            expr,
            union_free_vars(obj_tracer.free_vars),
        )

    def __repr__(self) -> str:
        return f"GetAttrTracker(attr={self.attr})"


class GetItemTracker(Tracker):
    """
    GetItemTracker is a subclass of Tracker that specifically tracks item access of a container variable.

    It generates instructions and traces the item value from the frame.

    Args:
        container_var (VariableBase): The container object whose item is to be tracked.
        key: The key/index of the item to be tracked.
    """

    def __init__(self, container_var: VariableBase, key: object):
        super().__init__([container_var])
        self.container = container_var
        self.key = key

    def gen_instructions(self, codegen: PyCodeGen):
        self.container.tracker.gen_instructions(codegen)
        codegen.gen_load_const(self.key)
        codegen.gen_subscribe()

    def trace_value_from_frame(self):
        container_tracer = self.container.tracker.trace_value_from_frame()
        return StringifyExpression(
            f"{container_tracer.expr}[{self.key!r}]",
            union_free_vars(container_tracer.free_vars),
        )

    def __repr__(self) -> str:
        return f"GetItemTracker(key={self.key!r})"


class GetIterTracker(Tracker):
    """
    GetIterTracker is a subclass of Tracker that specifically tracks iteration of a variable.

    It generates instructions and traces the iterator from the frame.

    Args:
        iter_source (VariableBase): The source variable to be iterated.
    """

    def __init__(self, iter_source: VariableBase):
        super().__init__([iter_source])
        self.iter_source = iter_source

    def gen_instructions(self, codegen: PyCodeGen):
        self.iter_source.tracker.gen_instructions(codegen)
        codegen._add_instr("GET_ITER")

    def trace_value_from_frame(self):
        iter_source_tracer = self.iter_source.tracker.trace_value_from_frame()
        return StringifyExpression(
            f"iter({self.iter_source.expr})",
            union_free_vars(iter_source_tracer.free_vars),
        )

    def __repr__(self) -> str:
        return "GetIterTracker()"
