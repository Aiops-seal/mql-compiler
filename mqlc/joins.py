"""JOIN 路径求解。

两件事：找最短路径，以及判断这条路走上去会不会放大度量。

第二条是重点。数仓里「数翻倍了」的来源几乎只有一类——一对多 JOIN 之后
SUM 把同一行算了多次。而它最坏的地方是**静默**：数字看着合理，只是大了几倍，
业务侧对不出来就发出去了。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .errors import CompileError
from .ontology import Hop, Ontology


@dataclass
class Path:
    hops: list[Hop]

    @property
    def tables(self) -> list[str]:
        return [self.hops[0].src] + [h.dst for h in self.hops] if self.hops else []


def shortest_paths(onto: Ontology, start: str, goal: str, max_hops: int = 2) -> list[list[Hop]]:
    """返回 start → goal 的所有最短路径（可能 0 条、1 条、多条）。

    完整 BFS 之后回溯，不用「第一次遇到就返回」的写法——那个写法遇到
    两条等长路径时，返回哪条取决于邻接表的顺序，编译器就变成了不确定的。
    """
    if start == goal:
        return [[]]

    dist: dict[str, int] = {start: 0}
    prev: dict[str, list[Hop]] = {}
    queue: deque[str] = deque([start])

    while queue:
        cur = queue.popleft()
        if dist[cur] >= max_hops:
            continue
        for hop in onto.neighbors(cur):
            nd = dist[cur] + 1
            if hop.dst not in dist:
                dist[hop.dst] = nd
                prev[hop.dst] = [hop]
                queue.append(hop.dst)
            elif dist[hop.dst] == nd:
                prev[hop.dst].append(hop)

    if goal not in dist:
        return []

    paths: list[list[Hop]] = []

    def walk(node: str, acc: list[Hop]) -> None:
        if node == start:
            paths.append(list(reversed(acc)))
            return
        for hop in prev.get(node, ()):
            walk(hop.src, acc + [hop])

    walk(goal, [])
    # 短的排前面（正常情况都等长），再按表名排序，保证输出稳定
    paths.sort(key=lambda p: (len(p), [h.dst for h in p]))
    return paths


def resolve(
    onto: Ontology, fact_table: str, target_table: str, purpose: str, max_hops: int = 2
) -> list[Hop]:
    """求一条唯一、安全（不扇出）的路径。失败即抛错。"""
    paths = shortest_paths(onto, fact_table, target_table, max_hops)
    if not paths:
        raise CompileError(
            "UNCOVERED_DIMENSION",
            f"从 {fact_table} 出发在 {max_hops} 跳内找不到 {target_table}，"
            f"无法承载 {purpose}",
            path=purpose,
            detail={"fact_table": fact_table, "target": target_table},
        )
    if len(paths) > 1:
        raise CompileError(
            "AMBIGUOUS_JOIN",
            f"{fact_table} → {target_table} 有 {len(paths)} 条等长路径，"
            f"不同路径可能算出不同的数，编译器不替你选",
            path=purpose,
            detail={"paths": [[h.dst for h in p] for p in paths]},
        )
    hops = paths[0]
    for hop in hops:
        if hop.cardinality == "one_to_many":
            raise CompileError(
                "FANOUT_RISK",
                f"{hop.src} → {hop.dst} 是一对多，JOIN 后度量的每一行会被重复计入；"
                f"这次的 {purpose} 需要它，结果会放大而不是报错",
                path=purpose,
                detail={
                    "hop": [hop.src, hop.dst],
                    "cardinality": hop.cardinality,
                    "suggestion": "换粒度更粗的事实表，或在维表上补一列冗余字段",
                },
            )
    return hops


def merge_join_paths(paths: list[list[Hop]]) -> list[Hop]:
    """合并多条路径，同一跳只保留一次。

    不合并的后果很直接：`JOIN dim_store` 出现两次，SQLite 报错，
    MySQL 算出翻倍的数。后者更难查。
    """
    seen: dict[tuple[str, str, str, str], Hop] = {}
    for path in paths:
        for hop in path:
            seen.setdefault((hop.src, hop.dst, hop.src_col, hop.dst_col), hop)
    return list(seen.values())
