"""最小堆，A* 寻路用。

移植自原项目 convex/util/minheap.ts。这里用比较器 + 标准库 heapq 实现：
``priority(a, b)`` 为 True 表示 a 比 b "更大"，b 应优先出队。
"""
from __future__ import annotations

import heapq
import itertools
from typing import Callable, Generic, List, TypeVar

T = TypeVar("T")


class MinHeap(Generic[T]):
    def __init__(self, priority: Callable[[T, T], bool]):
        # priority(a, b) -> True 表示 a > b（b 更优先出队）。
        self._greater = priority
        self._data: List[List] = []  # 元素: [_Wrapper, counter]
        self._counter = itertools.count()

    def push(self, item: T) -> None:
        wrapper = _Item(item, self._greater)
        heapq.heappush(self._data, [wrapper, next(self._counter)])

    def pop(self) -> T:
        return heapq.heappop(self._data)[0].item  # type: ignore[union-attr]

    def __bool__(self) -> bool:
        return bool(self._data)

    def __len__(self) -> int:
        return len(self._data)


class _Item(Generic[T]):
    """比较器包装：``a < b`` 当且仅当 greater(b, a)（b 更大，a 更优先）。"""

    __slots__ = ("item", "_greater")

    def __init__(self, item: T, greater: Callable[[T, T], bool]):
        self.item = item
        self._greater = greater

    def __lt__(self, other: "_Item[T]") -> bool:
        # a < b ⟺ b 比 a "更大" ⟺ greater(b, a)
        return self._greater(other.item, self.item)
