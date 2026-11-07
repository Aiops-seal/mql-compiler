"""事实表选择。

问题：同一个指标往往有多张表能算，选哪张取决于这次查询还要哪些维度。
`ads_gmv_1d` 有 city 没有 store，查城市维度用它最快，查门店维度用它就会扇出。

这一步的价值不在于「聪明」，而在于**把选表的依据变成可判定的**：
能不能覆盖维度、要不要 JOIN、JOIN 会不会放大。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import CompileError
from .joins import merge_join_paths, resolve, shortest_paths
from .ontology import Hop, Ontology


@dataclass
class Resolution:
    """一个候选表被接受时的完整落地方案。"""

    fact_table: str
    reason: str
    joins: list[Hop] = field(default_factory=list)
    #: property -> (表名, 列名)
    prop_bindings: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: 时间属性 -> (表名, 列名)
    time_binding: tuple[str, str] = ("", "")
    #: 每个指标在所选表上的 binding
    metric_bindings: dict[str, object] = field(default_factory=dict)
    #: 没选上的候选各自的理由，进 warnings
    dropped: list[str] = field(default_factory=list)


def pick_time_property(onto: Ontology, dimension_names: list[str]) -> str:
    """决定 `time_range` 落在哪个时间列上。

    优先用 MQL 里显式出现的时间维度；否则要求本体里恰好只有一个时间属性。
    **不做「猜一个最像的」**——猜错了会静默地按错误的列过滤时间。
    """
    for name in dimension_names:
        p = onto.prop(name)
        if p and p.kind == "time":
            return name
    times = [p.name for p in onto.properties.values() if p.kind == "time"]
    if len(times) == 1:
        return times[0]
    if not times:
        raise CompileError(
            "BAD_ONTOLOGY", "本体里没有任何 kind=time 的属性，无法处理 time_range", path="properties"
        )
    raise CompileError(
        "UNSOLVABLE",
        f"本体里有 {len(times)} 个时间属性 {times}，MQL 未指定用哪个，"
        f"请在 dimensions 里显式写一个",
        path="dimensions",
        candidates=tuple(times),
    )


def _bind_property(
    onto: Ontology, fact_table: str, prop_name: str, purpose: str
) -> tuple[tuple[str, str], list[Hop]]:
    """把一个属性落到表上：直接有列就免 JOIN，否则求一条唯一安全路径。"""
    direct = onto.column_for_property(prop_name, fact_table)
    if direct:
        return (fact_table, direct), []

    options: list[tuple[int, str]] = []
    for tname in onto.tables_for_property(prop_name):
        if tname == fact_table:
            continue
        paths = shortest_paths(onto, fact_table, tname)
        if paths:
            options.append((len(paths[0]), tname))

    if not options:
        raise CompileError(
            "UNCOVERED_DIMENSION",
            f"属性 {prop_name} 在 {fact_table} 上既没有列，也没有可达的承载表",
            path=purpose,
            detail={"fact_table": fact_table, "property": prop_name},
        )

    best = min(o[0] for o in options)
    winners = [t for d, t in options if d == best]
    if len(winners) > 1:
        raise CompileError(
            "AMBIGUOUS_JOIN",
            f"属性 {prop_name} 可以从 {winners} 任一张表取到，距离都是 {best} 跳，"
            f"这些表的数据若不同步就会算出不同的数",
            path=purpose,
            candidates=tuple(winners),
        )

    target = winners[0]
    hops = resolve(onto, fact_table, target, purpose)
    col = onto.column_for_property(prop_name, target)
    return (target, col or ""), hops


def try_resolve(
    onto: Ontology,
    fact_table: str,
    metric_names: list[str],
    dimension_names: list[str],
    filter_props: list[str],
    time_prop: str,
) -> Resolution:
    """尝试在这张事实表上完成本次查询。失败抛 CompileError。"""
    paths: list[list[Hop]] = []
    res = Resolution(fact_table=fact_table, reason="")

    for name in metric_names:
        b = onto.binding_for_metric(name, fact_table)
        if b is None:
            raise CompileError(
                "NO_CANDIDATE_TABLE",
                f"指标 {name} 在 {fact_table} 上没有 binding",
                path="metrics",
                detail={"fact_table": fact_table, "metric": name},
            )
        res.metric_bindings[name] = b

    for i, name in enumerate(dimension_names):
        (tname, col), hops = _bind_property(onto, fact_table, name, f"dimensions[{i}]")
        res.prop_bindings[name] = (tname, col)
        paths.append(hops)

    for name in filter_props:
        if name in res.prop_bindings:
            continue
        (tname, col), hops = _bind_property(onto, fact_table, name, f"filters:{name}")
        res.prop_bindings[name] = (tname, col)
        paths.append(hops)

    (tname, col), hops = _bind_property(onto, fact_table, time_prop, "time_range")
    res.time_binding = (tname, col)
    paths.append(hops)

    # scoped_filters 需要的表也要 JOIN，且必须和上面的合并
    for name in metric_names:
        m = onto.metrics[name]
        for sf in m.scoped_filters:
            hops = resolve(onto, fact_table, sf.table, f"scoped_filters:{name}")
            paths.append(hops)

    res.joins = merge_join_paths(paths)

    layer = onto.table(fact_table).layer
    if not res.joins:
        res.reason = "no_join"
    elif layer == "agg":
        res.reason = "pre_aggregated"
    else:
        res.reason = "fine_grained"
    return res


def choose(
    onto: Ontology,
    metric_names: list[str],
    dimension_names: list[str],
    filter_props: list[str],
    time_prop: str,
) -> Resolution:
    """在候选表里选一个，返回落地方案。"""
    candidates: set[str] | None = None
    for name in metric_names:
        tables = onto.metric_tables(name)
        candidates = tables if candidates is None else (candidates & tables)

    if not candidates:
        raise CompileError(
            "NO_CANDIDATE_TABLE",
            f"指标 {metric_names} 没有任何一张表能同时承载；"
            f"本编译器不支持跨事实表计算，请在 ads 层准备宽表",
            path="metrics",
            detail={n: sorted(onto.metric_tables(n)) for n in metric_names},
        )

    failures: dict[str, str] = {}
    ok: list[Resolution] = []
    for tname in sorted(candidates):
        try:
            ok.append(
                try_resolve(onto, tname, metric_names, dimension_names, filter_props, time_prop)
            )
        except CompileError as exc:
            failures[tname] = f"{exc.code}: {exc.message}"

    if not ok:
        # 报哪个错：扇出比「不可达」更有信息量，优先报它
        first_code = "UNCOVERED_DIMENSION"
        first = None
        for tname in sorted(failures):
            if failures[tname].startswith("FANOUT_RISK"):
                first_code, first = "FANOUT_RISK", failures[tname]
                break
        if first is None:
            tname0 = sorted(failures)[0]
            code = failures[tname0].split(":", 1)[0]
            raise CompileError(
                code,
                failures[tname0].split(":", 1)[1].strip(),
                path="dimensions",
                detail={"candidates_tried": failures},
            )
        raise CompileError(
            first_code,
            first.split(":", 1)[1].strip(),
            path="dimensions",
            detail={"candidates_tried": failures},
        )

    def rank(r: Resolution) -> tuple[int, int, str]:
        no_join = 1 if not r.joins else 0
        agg = 1 if onto.table(r.fact_table).layer == "agg" else 0
        return (-no_join, -agg, r.fact_table)

    ok.sort(key=rank)
    best = ok[0]

    dropped = []
    for tname in sorted(failures):
        dropped.append(f"{tname} 被排除：{failures[tname].split(':', 1)[1].strip()}")
    for r in ok[1:]:
        dropped.append(
            f"{r.fact_table} 被排除：需要 {len(r.joins)} 次 JOIN，"
            f"而 {best.fact_table} 需要 {len(best.joins)} 次"
        )
    best.dropped = dropped
    return best
