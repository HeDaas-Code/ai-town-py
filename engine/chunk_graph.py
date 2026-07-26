"""ChunkGraph：区块级别的连通图，用于跨区块长距离寻路。

把每个已加载/已摘要的 chunk 当作图节点，相邻 chunk 之间的 portal 连接当作边。
先在图上做 A* 得到 chunk 序列，再在每个 chunk 内部做网格 A*。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from .minheap import MinHeap

ChunkKey = Tuple[int, int]


class ChunkGraph:
    """区块连通图。"""

    def __init__(self):
        self.edges: Dict[ChunkKey, List[Tuple[ChunkKey, float]]] = {}

    def add_chunk(self, cx: int, cy: int) -> None:
        key = (cx, cy)
        if key not in self.edges:
            self.edges[key] = []

    def add_edge(self, a: ChunkKey, b: ChunkKey, cost: float = 1.0) -> None:
        """添加无向边。"""
        self.add_chunk(a[0], a[1])
        self.add_chunk(b[0], b[1])
        self.edges[a].append((b, cost))
        self.edges[b].append((a, cost))

    def neighbors(self, key: ChunkKey) -> List[Tuple[ChunkKey, float]]:
        return list(self.edges.get(key, []))

    def has_chunk(self, key: ChunkKey) -> bool:
        return key in self.edges

    def find_path(
        self, start: ChunkKey, goal: ChunkKey
    ) -> Optional[List[ChunkKey]]:
        """A* 找 chunk 路径。返回 chunk 坐标列表（含起点和终点），无路径返回 None。"""
        if start == goal:
            return [start]
        if not self.has_chunk(start) or not self.has_chunk(goal):
            return None

        open_set = MinHeap(lambda a, b: a[0] > b[0])
        open_set.push((0.0, start))
        came_from: Dict[ChunkKey, ChunkKey] = {}
        g_score: Dict[ChunkKey, float] = {start: 0.0}
        f_score: Dict[ChunkKey, float] = {start: _heuristic(start, goal)}
        closed: Set[ChunkKey] = set()

        while open_set:
            _, current = open_set.pop()
            if current in closed:
                continue
            if current == goal:
                return _reconstruct_path(came_from, current)
            closed.add(current)

            for nxt, edge_cost in self.edges.get(current, []):
                if nxt in closed:
                    continue
                tentative_g = g_score[current] + edge_cost
                if tentative_g < g_score.get(nxt, float("inf")):
                    came_from[nxt] = current
                    g_score[nxt] = tentative_g
                    f_score[nxt] = tentative_g + _heuristic(nxt, goal)
                    open_set.push((f_score[nxt], nxt))

        return None


def _heuristic(a: ChunkKey, b: ChunkKey) -> float:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _reconstruct_path(
    came_from: Dict[ChunkKey, ChunkKey], current: ChunkKey
) -> List[ChunkKey]:
    path = [current]
    while current in came_from:
        current = came_from[current]
        path.append(current)
    path.reverse()
    return path
