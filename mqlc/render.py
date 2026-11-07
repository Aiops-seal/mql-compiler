"""SQL 渲染与 plan 构建。

渲染的输入全部来自前面阶段的确定性结果，这一层不做任何决策——
它只负责把已经定好的东西写成文本。任何「这里顺手判断一下」都是 bug 的温床。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .errors import CompileError
from .ontology import Binding, Ontology
from .select import Resolution

#: `SUM(...)` / `COUNT(DISTINCT ...)` 这类聚合，compare 时要拆开加 CASE
_AGG = re.compile(r"^(SUM|AVG|COUNT|MIN|MAX)\((DISTINCT\s+)?(.+)\)$", re.S)
#: `{period_start} - 365d` 这种带日期运算的占位符，必须先于裸占位符替换
_DATE_OP_FULL = re.compile(r"(\{period_start\}|\{period_end\})\s*([+-])\s*(\d+)([dwmy])")

_COMPARE_SUFFIX = {"yoy": "yoy", "mom": "mom", "wow": "wow"}


@dataclass
class TimeInfo:
    prop: str
    primary: tuple[date, date]
    compare: tuple[date, date] | None
    compare_type: str | None
    scan: tuple[date, date]
    granularity: str | None = None


@dataclass
class Compiled:
    sql: str
    plan: dict
    warnings: list[str] = field(default_factory=list)


class Aliaser:
    """给表分配 SQL 别名。事实表固定 `f`，维表 `d_<去掉 dim_ 前缀的表名>`。"""

    def __init__(self, fact_table: str) -> None:
        self.fact_table = fact_table
        self._map: dict[str, str] = {fact_table: "f"}
        self._used: set[str] = {"f"}

    def get(self, table: str) -> str:
        if table in self._map:
            return self._map[table]
        base = table[4:] if table.startswith("dim_") else table
        alias = f"d_{base}"
        n = 2
        while alias in self._used:
            alias = f"d_{base}{n}"
            n += 1
        self._map[table] = alias
        self._used.add(alias)
        return alias

    def as_dict(self) -> dict[str, str]:
        return dict(self._map)


def _render_condition(
    cond: str,
    dialect,
    period_start: date,
    period_end: date,
    now: date,
    aliases: Aliaser,
) -> str:
    """把本体里的条件字符串变成可执行的 SQL 片段。

    顺序很重要：先算日期运算（结果里含日期字面量），再替换剩下的占位符，
    最后把物理表名换成别名。反过来的话，`{period_start}` 会先被替换成
    字面量，日期运算的正则就匹配不上了。
    """

    def _op(m: re.Match) -> str:
        which, sign, num, unit = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        days = num * (7 if unit == "w" else 1) * (-1 if sign == "-" else 1)
        base = period_start if which == "{period_start}" else period_end
        return dialect.add_days(dialect.date_literal(base), days)

    out = _DATE_OP_FULL.sub(_op, cond)
    out = out.replace("{period_start}", dialect.date_literal(period_start))
    out = out.replace("{period_end}", dialect.date_literal(period_end))
    out = out.replace("{now}", dialect.date_literal(now))
    out = out.replace("{f}", aliases.get(aliases.fact_table))
    # 表名换成别名。长的先换，避免 dim_store 被 dim_st 之类的短名前缀截断
    for table, alias in sorted(aliases.as_dict().items(), key=lambda kv: -len(kv[0])):
        out = re.sub(rf"\b{re.escape(table)}\.", f"{alias}.", out)
    return out


def _value_sql(operator: str, value: object, dialect) -> str:
    if operator in ("in", "not_in"):
        vals = value if isinstance(value, (list, tuple)) else [value]
        if not vals:
            raise CompileError("BAD_MQL", f"{operator} 的值不能为空", path="filters")
        return "(" + ", ".join(_scalar(v) for v in vals) + ")"
    if operator == "between":
        vals = value if isinstance(value, (list, tuple)) else []
        if len(vals) != 2:
            raise CompileError("BAD_MQL", "between 需要两个值", path="filters")
        return f"{_scalar(vals[0])} AND {_scalar(vals[1])}"
    if isinstance(value, (list, tuple)):
        if len(value) != 1:
            raise CompileError(
                "BAD_MQL", f"{operator} 只接受单值，收到 {len(value)} 个", path="filters"
            )
        value = value[0]
    return _scalar(value)


def _scalar(v: object) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


_OP_SQL = {
    "eq": lambda a, b: f"{a} = {b}",
    "neq": lambda a, b: f"{a} <> {b}",
    "gt": lambda a, b: f"{a} > {b}",
    "gte": lambda a, b: f"{a} >= {b}",
    "lt": lambda a, b: f"{a} < {b}",
    "lte": lambda a, b: f"{a} <= {b}",
    "in": lambda a, b: f"{a} IN {b}",
    "not_in": lambda a, b: f"{a} NOT IN {b}",
    "like": lambda a, b: f"{a} LIKE {b}",
    "between": lambda a, b: f"{a} BETWEEN {b}",
}


def _filter_sql(field_sql: str, operator: str, value: object, dialect) -> str:
    fn = _OP_SQL.get(operator)
    if fn is None:
        raise CompileError(
            "BAD_MQL",
            f"operator {operator!r} 不在枚举内",
            path="filters",
            candidates=tuple(_OP_SQL),
        )
    return fn(field_sql, _value_sql(operator, value, dialect))


def _wrap_case(binding: Binding, cond: str) -> str:
    """把 `SUM({f}.x)` 变成 `SUM(CASE WHEN <cond> THEN {f}.x END)`。

    这是 compare 的实现方式。代价是 WHERE 要扩到两期的并集，
    好处是不用 UNION——而 UNION 直接撞上能力上限。
    """
    expr = binding.expr.replace("{f}", "f")
    m = _AGG.match(expr)
    if not m:
        raise CompileError(
            "UNSOLVABLE",
            f"指标表达式 {binding.expr!r} 不是单个聚合函数，无法套用 compare 的分期条件；"
            f"compare 只支持 SUM/AVG/COUNT/MIN/MAX 这类单层聚合",
            path="compare",
        )
    fn, distinct, inner = m.group(1), m.group(2) or "", m.group(3)
    return f"{fn}({distinct}CASE WHEN {cond} THEN {inner} END)"


def build(
    mql: dict,
    onto: Ontology,
    ctx: dict,
    dialect,
    resolution: Resolution,
    time: TimeInfo,
) -> Compiled:
    aliases = Aliaser(resolution.fact_table)
    warnings = list(resolution.dropped)

    # ---- 维度 -------------------------------------------------------
    select_items: list[str] = []
    group_items: list[str] = []
    dim_outputs: dict[str, str] = {}
    for dim in mql.get("dimensions") or []:
        name = str(dim["name"]) if isinstance(dim, dict) else str(dim)
        tname, col = resolution.prop_bindings[name]
        alias = aliases.get(tname)
        expr = dialect.qualified(alias, col)
        gran = dim.get("granularity") if isinstance(dim, dict) else None
        if gran:
            if onto.prop(name) and onto.prop(name).kind != "time":
                raise CompileError(
                    "BAD_MQL", f"属性 {name} 不是时间属性，不能指定 granularity", path="dimensions"
                )
            expr = dialect.trunc(expr, gran)
        select_items.append(f"{expr} AS {name}")
        group_items.append(expr)
        dim_outputs[name] = name

    # ---- 度量 -------------------------------------------------------
    mql_metrics = [str(m["name"]) if isinstance(m, dict) else str(m) for m in mql["metrics"]]
    metric_outputs: dict[str, str] = {}
    compare_cond = ""
    primary_cond = ""
    suffix = ""
    tfa = aliases.get(resolution.time_binding[0])
    tcol = resolution.time_binding[1]
    if time.compare and time.compare_type:
        suffix = _COMPARE_SUFFIX[time.compare_type]
        # WHERE 扫的是两期的并集，所以两列都必须带 CASE 把期间收回来；
        # 只给对比列加 CASE 是个很隐蔽的错——主列会把两期一起加总，
        # 数字大得「看起来像是增长了」。
        primary_cond = (
            f"{tfa}.{tcol} BETWEEN {dialect.date_literal(time.primary[0])}"
            f" AND {dialect.date_literal(time.primary[1])}"
        )
        compare_cond = (
            f"{tfa}.{tcol} BETWEEN {dialect.date_literal(time.compare[0])}"
            f" AND {dialect.date_literal(time.compare[1])}"
        )

    for name in mql_metrics:
        b: Binding = resolution.metric_bindings[name]  # type: ignore[assignment]
        if compare_cond:
            select_items.append(f"{_wrap_case(b, primary_cond)} AS {name}")
            select_items.append(f"{_wrap_case(b, compare_cond)} AS {name}_{suffix}")
        else:
            select_items.append(f"{b.expr.replace('{f}', 'f')} AS {name}")
        metric_outputs[name] = name
        if compare_cond:
            metric_outputs[f"{name}_{suffix}"] = f"{name}_{suffix}"

    # ---- JOIN -------------------------------------------------------
    join_lines: list[str] = []
    plan_joins: list[dict] = []
    dim_tables = {t for t, _c in resolution.prop_bindings.values()}
    for hop in resolution.joins:
        sa, da = aliases.get(hop.src), aliases.get(hop.dst)
        join_lines.append(
            f"JOIN {hop.dst} {da} ON {sa}.{hop.src_col} = {da}.{hop.dst_col}"
        )
        purpose = "dimension" if hop.dst in dim_tables else "scoped_filter"
        plan_joins.append(
            {
                "from": hop.src,
                "to": hop.dst,
                "on": [hop.src_col, hop.dst_col],
                "cardinality": hop.cardinality,
                "purpose": purpose,
            }
        )

    # ---- WHERE ------------------------------------------------------
    where: list[str] = []
    injected: list[dict] = []

    scan_cond = (
        f"{aliases.get(resolution.time_binding[0])}.{resolution.time_binding[1]} BETWEEN "
        f"{dialect.date_literal(time.scan[0])} AND {dialect.date_literal(time.scan[1])}"
    )
    where.append(scan_cond)
    injected.append({"source": "time", "sql": scan_cond})

    for name in mql_metrics:
        b: Binding = resolution.metric_bindings[name]  # type: ignore[assignment]
        for req in b.requires:
            sql = _render_condition(
                req, dialect, time.primary[0], time.primary[1], ctx["now"], aliases
            )
            if sql not in where:
                where.append(sql)
                injected.append({"source": "required", "sql": sql, "metric": name})

    for flt in mql.get("filters") or []:
        if not isinstance(flt, dict) or "field" not in flt:
            raise CompileError("BAD_MQL", "filters 每项必须是带 field 的对象", path="filters")
        field = str(flt["field"])
        tname, col = resolution.prop_bindings.get(field, ("", ""))
        if not tname:
            raise CompileError(
                "UNKNOWN_PROPERTY", f"过滤字段 {field} 未落到任何表", path="filters", candidates=()
            )
        sql = _filter_sql(
            dialect.qualified(aliases.get(tname), col),
            str(flt.get("operator", "eq")),
            flt.get("value"),
            dialect,
        )
        where.append(sql)
        injected.append({"source": "mql", "sql": sql})

    for perm in ctx.get("permissions") or []:
        field = str(perm.get("field", ""))
        tname, col = resolution.prop_bindings.get(field, ("", ""))
        if not tname:
            raise CompileError(
                "BAD_CONTEXT",
                f"权限字段 {field} 不是本次查询可用的属性；"
                f"权限必须落在已 JOIN 的表上，否则只能靠子查询——那超出能力上限",
                path="ctx.permissions",
                candidates=tuple(resolution.prop_bindings),
            )
        sql = _filter_sql(
            dialect.qualified(aliases.get(tname), col),
            str(perm.get("operator", "in")),
            perm.get("values", perm.get("value")),
            dialect,
        )
        where.append(sql)
        injected.append({"source": "permission", "sql": sql, "field": field})

    scoped_instantiated: list[dict] = []
    for name in mql_metrics:
        m = onto.metrics[name]
        for sf in m.scoped_filters:
            sql = _render_condition(
                sf.condition, dialect, time.primary[0], time.primary[1], ctx["now"], aliases
            )
            where.append(sql)
            scoped_instantiated.append({"metric": name, "sql": sql})
            injected.append({"source": "scoped", "sql": sql, "metric": name})

    # ---- 拼 SQL -----------------------------------------------------
    lines = ["SELECT", "  " + ",\n  ".join(select_items), f"FROM {resolution.fact_table} f"]
    for j in join_lines:
        lines.append("  " + j)
    lines.append("WHERE " + "\n  AND ".join(where))
    if group_items:
        lines.append("GROUP BY " + ", ".join(group_items))

    order_items = []
    for s in mql.get("sort") or []:
        field = str(s.get("field", "")) if isinstance(s, dict) else str(s)
        direction = str(s.get("order", "asc")).upper() if isinstance(s, dict) else "ASC"
        if field not in dim_outputs and field not in metric_outputs:
            raise CompileError(
                "BAD_MQL",
                f"sort 字段 {field} 不在本次查询的输出列里",
                path="sort",
                candidates=tuple(list(dim_outputs) + list(metric_outputs)),
            )
        order_items.append(f"{field} {direction}")
    if order_items:
        lines.append("ORDER BY " + ", ".join(order_items))
    if mql.get("limit") is not None:
        lines.append(f"LIMIT {int(mql['limit'])}")

    sql = "\n".join(lines)

    # ---- plan -------------------------------------------------------
    plan = {
        "dialect": dialect.name,
        "dialect_verified": dialect.verified,
        "fact_table": resolution.fact_table,
        "fact_table_reason": resolution.reason,
        "tables_used": [resolution.fact_table] + [h.dst for h in resolution.joins],
        "joins": plan_joins,
        "metrics": [
            {
                "name": n,
                "binding": resolution.metric_bindings[n].table,  # type: ignore[union-attr]
                "expr": resolution.metric_bindings[n].expr,  # type: ignore[union-attr]
            }
            for n in mql_metrics
        ],
        "properties": [
            {"name": n, "binding": t, "column": c}
            for n, (t, c) in resolution.prop_bindings.items()
        ],
        "injected_filters": injected,
        "scoped_filters_instantiated": scoped_instantiated,
        "time": {
            "dimension": time.prop,
            "granularity": time.granularity,
            "primary": {"start": time.primary[0].isoformat(), "end": time.primary[1].isoformat()},
            "compare": (
                {
                    "type": time.compare_type,
                    "start": time.compare[0].isoformat(),
                    "end": time.compare[1].isoformat(),
                }
                if time.compare
                else None
            ),
            "scan_range": {"start": time.scan[0].isoformat(), "end": time.scan[1].isoformat()},
        },
    }
    if not dialect.verified:
        warnings.append(
            f"方言 {dialect.name} 标记为未验证：SQL 是按其语法生成的，但没有在真实数据库上跑过"
        )
    return Compiled(sql, plan, warnings)
