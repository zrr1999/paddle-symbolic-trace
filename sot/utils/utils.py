from __future__ import annotations

import builtins
import inspect
import os
import time
import types
from typing import Any, Generic, Iterable, Iterator, TypeVar
from weakref import WeakValueDictionary

import paddle
from paddle.framework import Program
from paddle.utils import flatten, map_structure

from .paddle_api_config import (
    break_graph_set,
    paddle_api_list,
    paddle_api_module_prefix,
)

T = TypeVar("T")


class Singleton(Generic[T]):
    def __init__(self, cls: type[T]):
        self._cls = cls
        self._instance = {}

    def __call__(self) -> T:
        if self._cls not in self._instance:
            self._instance[self._cls] = self._cls()
        return self._instance[self._cls]


class NameGenerator:
    def __init__(self, prefix):
        self.counter = 0
        self.prefix = prefix

    def next(self):
        name = self.prefix + str(self.counter)
        self.counter += 1
        return name

    def match_name(self, name: str) -> bool:
        return name.startswith(self.prefix)


@Singleton
class ResumeFnNameFactory:
    def __init__(self) -> None:
        self.gen = NameGenerator('resume_')

    def next(self):
        name = self.gen.next()
        return name


def log(level, *args):
    cur_level = int(os.environ.get("LOG_LEVEL", "0"))
    if level <= cur_level:
        print(*args, end="")


def log_do(level, fn):
    cur_level = int(os.environ.get("LOG_LEVEL", "0"))
    if level <= cur_level:
        fn()


def no_eval_frame(func):
    def no_eval_frame_func(*args, **kwargs):
        old_cb = paddle.framework.core.set_eval_frame(None)
        try:
            retval = func(*args, **kwargs)
        except:
            raise
        finally:
            paddle.framework.core.set_eval_frame(old_cb)
        return retval

    return no_eval_frame_func


def is_paddle_api(func):
    if isinstance(func, paddle.nn.Layer):  # ignore all the classes
        return False
    if hasattr(func, "__self__"):  # ignore all the methods
        return False
    if inspect.isclass(
        func
    ):  # paddle.Tensor should not be wrapped, but how about other situations?
        return False
    return in_paddle_module(func) or func in paddle_api_list


def is_builtin_fn(fn):
    if isinstance(fn, types.BuiltinFunctionType):
        return True
    for member_name, member in inspect.getmembers(builtins):
        if member is fn and isinstance(member, type):
            return True
    return False


def in_paddle_module(func):
    if hasattr(func, "__module__"):
        module_str = func.__module__
        if module_str is None:
            return False
        log(5, "find paddle function with __module__: ", module_str, "\n")
        if hasattr(func, "__name__"):
            log(
                5, "                     with __name__  : ", func.__name__, "\n"
            )
        log(5, "                     with results   : ")
        for prefix in paddle_api_module_prefix:
            if module_str.startswith(prefix):
                log(5, " True\n")
                return True
    log(5, " False\n")
    return False


def is_break_graph_api(func):
    return func in break_graph_set


def map_if(*structures, pred, true_fn, false_fn):
    def replace(*args):
        if pred(*args):
            return true_fn(*args)
        return false_fn(*args)

    return map_structure(replace, *structures)


def flatten_extend(structure):
    for item in flatten(structure):
        if isinstance(item, slice):
            yield item.start
            yield item.stop
            yield item.step
        else:
            yield item


def map_if_extend(structure, pred, true_fn, false_fn):
    """support extended structures like slice and SliceVariable"""

    def wrapped_pred(x):
        if isinstance(x, slice):
            return True
        return pred(x)

    def wrapped_true_fn(x):
        if isinstance(x, (slice)):
            l = [x.start, x.stop, x.step]
            l = map_if_extend(l, pred, true_fn, false_fn)
            return slice(*l)
        return true_fn(x)

    return map_if(
        structure, pred=wrapped_pred, true_fn=wrapped_true_fn, false_fn=false_fn
    )


def count_if(*structures, pred):
    def is_true(*args):
        if pred(*args):
            return 1
        return 0

    return sum(flatten(map_structure(is_true, *structures)))


class Cache:
    def __init__(self, weak=False):
        if not weak:
            self.cache = {}
        else:
            self.cache = WeakValueDictionary()
        self.hit_num = 0

    def __call__(self, *args, **kwargs):
        cache_key = self.key_fn(*args, **kwargs)
        if cache_key is None:
            return self.value_fn(*args, **kwargs)
        if cache_key in self.cache:
            log(5, "cache hit: ", cache_key, "\n")
            self.hit_num += 1
            return self.cache[cache_key]
        value = self.value_fn(*args, **kwargs)
        self.cache[cache_key] = value
        return value

    def clear(self):
        self.cache.clear()
        self.hit_num = 0

    def key_fn(self, *args, **kwargs):
        raise NotImplementedError()

    def value_fn(self, *args, **kwargs):
        raise NotImplementedError()


def execute_time(func):
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        execution_time = end_time - start_time
        print("Execute time:", execution_time)
        return result

    return wrapper


def meta_str(shape, dtype, stop_gradient):
    return f"(shape: {shape}, dtype: {dtype}, stop_gradient: {stop_gradient})"


def is_strict_mode():
    return os.environ.get("STRICT_MODE", "0") == "1"


def show_trackers() -> str | None:
    return os.environ.get("SHOW_TRACKERS", None)


def is_clean_code() -> bool:
    return os.environ.get('CLEAN_CODE', "False") == "True"


def list_find_index_by_id(li: list[Any], item: Any) -> int:
    return [id(it) for it in li].index(id(item))


def list_contain_by_id(li: list[Any], item: Any) -> int:
    return id(item) in [id(it) for it in li]


def get_unbound_method(obj, name):
    # TODO(dev): Consider the case of patching methods to instances
    return getattr(obj.__class__, name)


@Singleton
class GraphLogger:
    graph_num: int
    op_num: int
    graphs: list[Program]
    ops: list[paddle.fluid.framework.Operator]

    def __init__(self):
        self.clear()

    def clear(self):
        self.graph_num = 0
        self.op_num = 0
        self.graphs: list = []
        self.ops: list = []

    def get_graph_num(self):
        return self.graph_num

    def get_op_num(self):
        return self.op_num

    def add_subgraph(self, program: Program):
        self.graph_num += 1
        self.graphs.append(program)

        for block in program.blocks:
            sub_op = []
            for op in block.ops:
                self.op_num += 1
                sub_op.append(op)
            self.ops.append(sub_op)

    def add_subgprah_info(self, strs):
        for i in range(len(self.graphs)):
            strs.append(
                "------------------------------------------------------"
            )

            strs.append(f"subgraph {i}, OpNum: {len(self.ops[i])}")
            strs.append(f"{self.graphs[i]}")

    def __str__(self):
        strs = []
        strs.append("---------------- PaddleSOT graph info ----------------")
        strs.append(f"SubgraphNum: {self.get_graph_num()}")
        strs.append(f"OpNum: {self.get_op_num()}")

        # We can display every subgraph info
        log_do(5, lambda: self.add_subgprah_info(strs))

        strs.append("---------------- PaddleSOT graph info ----------------")
        return "\n".join(strs)

    def __repr__(self):
        return self.__str__()

    def print_info(self):
        print(self)


@Singleton
class SotUndefinedVar:
    pass


def hashable(obj):
    try:
        hash(obj)
        return True
    except TypeError as e:
        return False


class OrderedSet(Generic[T]):
    """
    A set that preserves the order of insertion.
    """

    _data: dict[T, None]

    def __init__(self, items: Iterable[T] | None = None):
        """
        Examples:
            >>> s = OrderedSet([1, 2, 3])
            >>> s
            OrderedSet(1, 2, 3)
            >>> s = OrderedSet()
            >>> s
            OrderedSet()
        """
        self._data = dict.fromkeys(items) if items is not None else {}

    def __iter__(self) -> Iterator[T]:
        """
        Examples:
            >>> s = OrderedSet([1, 2, 3])
            >>> for item in s:
            ...     print(item)
            1
            2
            3
        """
        return iter(self._data)

    def __or__(self, other: OrderedSet[T]) -> OrderedSet[T]:
        """
        Union two sets.

        Args:
            other: Another set to be unioned.

        Returns:
            The union of two sets.

        Examples:
            >>> s1 = OrderedSet([1, 2, 3])
            >>> s2 = OrderedSet([2, 3, 4])
            >>> s1 | s2
            OrderedSet(1, 2, 3, 4)
        """
        return OrderedSet(list(self) + list(other))

    def __ior__(self, other: OrderedSet[T]):
        """
        Union two sets in place.

        Args:
            other: Another set to be unioned.

        Examples:
            >>> s1 = OrderedSet([1, 2, 3])
            >>> s2 = OrderedSet([2, 3, 4])
            >>> s1 |= s2
            >>> s1
            OrderedSet(1, 2, 3, 4)
        """
        self._data.update(dict.fromkeys(other))
        return self

    def __and__(self, other: OrderedSet[T]) -> OrderedSet[T]:
        """
        Intersect two sets.

        Args:
            other: Another set to be intersected.

        Returns:
            The intersection of two sets.

        Examples:
            >>> s1 = OrderedSet([1, 2, 3])
            >>> s2 = OrderedSet([2, 3, 4])
            >>> s1 & s2
            OrderedSet(2, 3)
        """
        return OrderedSet([item for item in self if item in other])

    def __iand__(self, other: OrderedSet[T]):
        """
        Intersect two sets in place.

        Args:
            other: Another set to be intersected.

        Examples:
            >>> s1 = OrderedSet([1, 2, 3])
            >>> s2 = OrderedSet([2, 3, 4])
            >>> s1 &= s2
            >>> s1
            OrderedSet(2, 3)
        """
        self._data = {item: None for item in self if item in other}
        return self

    def __sub__(self, other: OrderedSet[T]) -> OrderedSet[T]:
        """
        Subtract two sets.

        Args:
            other: Another set to be subtracted.

        Returns:
            The subtraction of two sets.

        Examples:
            >>> s1 = OrderedSet([1, 2, 3])
            >>> s2 = OrderedSet([2, 3, 4])
            >>> s1 - s2
            OrderedSet(1)
        """
        return OrderedSet([item for item in self if item not in other])

    def __isub__(self, other: OrderedSet[T]):
        """
        Subtract two sets in place.

        Args:
            other: Another set to be subtracted.

        Examples:
            >>> s1 = OrderedSet([1, 2, 3])
            >>> s2 = OrderedSet([2, 3, 4])
            >>> s1 -= s2
            >>> s1
            OrderedSet(1)
        """
        self._data = {item: None for item in self if item not in other}
        return self

    def add(self, item: T):
        """
        Add an item to the set.

        Args:
            item: The item to be added.

        Examples:
            >>> s = OrderedSet([1, 2, 3])
            >>> s.add(4)
            >>> s
            OrderedSet(1, 2, 3, 4)
        """
        self._data.setdefault(item)

    def remove(self, item: T):
        """
        Remove an item from the set.

        Args:
            item: The item to be removed.

        Examples:
            >>> s = OrderedSet([1, 2, 3])
            >>> s.remove(2)
            >>> s
            OrderedSet(1, 3)
        """
        del self._data[item]

    def __contains__(self, item: T) -> bool:
        """
        Examples:
            >>> s = OrderedSet([1, 2, 3])
            >>> 1 in s
            True
            >>> 4 in s
            False
        """
        return item in self._data

    def __len__(self) -> int:
        """
        Examples:
            >>> s = OrderedSet([1, 2, 3])
            >>> len(s)
            3
        """
        return len(self._data)

    def __bool__(self) -> bool:
        """
        Examples:
            >>> s = OrderedSet([1, 2, 3])
            >>> bool(s)
            True
            >>> s = OrderedSet()
            >>> bool(s)
            False
        """
        return bool(self._data)

    def __eq__(self, other: object) -> bool:
        """
        Examples:
            >>> s1 = OrderedSet([1, 2, 3])
            >>> s2 = OrderedSet([1, 2, 3])
            >>> s1 == s2
            True
            >>> s3 = OrderedSet([3, 2, 1])
            >>> s1 == s3
            False
        """
        if not isinstance(other, OrderedSet):
            return NotImplemented
        return list(self) == list(other)

    def __repr__(self) -> str:
        data_repr = ", ".join(map(repr, self._data))
        return f"OrderedSet({data_repr})"
