"""MQL Compiler —— 把 MQL 编译成 SQL。

    >>> from mqlc import compile_mql, Ontology
    >>> onto = Ontology.load("fixtures/ontology.json")
    >>> r = compile_mql({"mql_version": "1.1",
    ...                  "metrics": [{"name": "gmv"}],
    ...                  "dimensions": [{"name": "city"}],
    ...                  "time_range": {"start": "-1m", "end": "yesterday"}},
    ...                 onto, {"dialect": "sqlite", "now": "2026-11-07"})
    >>> r["ok"]
    True

纯函数：不读时钟、不连数据库、不保留状态。同样的入参永远同样的输出。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from . import dialect as _dialect
from . import timeexpr
from .errors import CompileError
from .ontology import Ontology
from .render import TimeInfo, build
from .select import choose, pick_time_property

__all__ = ["compile_mql", "Ontology", "CompileError", "dialects"]
__version__ = "1.0.0"

_ALLOWED_TOP = {
    "mql_version",
    "metrics",
    "dimensions",
    "filters",
    "time_range",
    "compare",
    "sort",
    "limit",
}


def dialects() -> list[str]:
    return _dialect.known()


# ---------------------------------------------------------------- 阶段 A


def _bad_mql(msg: str, path: str = "$") -> CompileError:
    return CompileError("BAD_MQL", msg, path=path)


def _check_mql(mql: Any) -> dict:
    if not isinstance(mql, dict):
        raise _bad_mql(f"MQL 顶层必须是对象，实际是 {type(mql).__name__}")
    unknown = set(mql) - _ALLOWED_TOP
    if unknown:
        raise _bad_mql(
            f"出现未知字段 {sorted(unknown)}；严格模式下未知字段直接拒绝",
            path=",".join(sorted(unknown)),
        )
    metrics = mql.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        raise _bad_mql("metrics 必须是非空数组", path="metrics")
    for i, m in enumerate(metrics):
        if isinstance(m, str):
            continue
        if not isinstance(m, dict) or not isinstance(m.get("name"), str):
            raise _bad_mql("每个 metric 必须是字符串或带 name 的对象", path=f"metrics[{i}]")
    for key in ("dimensions", "filters", "sort"):
        if key in mql and not isinstance(mql[key], list):
            raise _bad_mql(f"{key} 必须是数组", path=key)
    tr = mql.get("time_range")
    if tr is not None:
        if not isinstance(tr, dict) or "start" not in tr or "end" not in tr:
            raise _bad_mql("time_range 必须是带 start/end 的对象", path="time_range")
    return mql


def _normalize_ctx(ctx: dict | None) -> dict:
    if ctx is None:
        raise CompileError("BAD_CONTEXT", "ctx 必填；now 不接受默认值", path="ctx")
    if "now" not in ctx:
        raise CompileError(
            "BAD_CONTEXT",
            "ctx.now 必填。编译器不读系统时钟——省掉这个参数，同一份 MQL "
            "隔天编译就会得到不同的 SQL",
            path="ctx.now",
        )
    now = ctx["now"]
    if isinstance(now, str):
        try:
            now = date.fromisoformat(now)
        except ValueError:
            raise CompileError("BAD_CONTEXT", f"ctx.now 不是合法日期：{now!r}", path="ctx.now")
    elif not isinstance(now, date):
        raise CompileError("BAD_CONTEXT", "ctx.now 必须是 date 或 YYYY-MM-DD", path="ctx.now")

    out = {
        "dialect": _dialect.get(str(ctx.get("dialect", "sqlite"))),
        "now": now,
        "permissions": list(ctx.get("permissions") or []),
        "default_time": str(ctx.get("default_time", "t-1")),
        "max_scan_days": int(ctx.get("max_scan_days", 3660)),
    }
    for i, p in enumerate(out["permissions"]):
        if not isinstance(p, dict) or "field" not in p:
            raise CompileError("BAD_CONTEXT", "permissions 每项必须是带 field 的对象", path=f"ctx.permissions[{i}]")
    return out


def _names(items: list, key: str = "name") -> list[str]:
    out = []
    for it in items or []:
        out.append(it if isinstance(it, str) else str(it.get(key, "")))
    return out


# ---------------------------------------------------------------- 阶段 E


def _resolve_default_time(spec: str, now: date) -> date:
    """`t-1` 这类默认时间。只有这一种语法，不引入第二套表达式。"""
    s = spec.strip()
    if s.startswith("t-"):
        try:
            return now - timedelta(days=int(s[2:]))
        except ValueError:
            pass
    if s == "t":
        return now
    return timeexpr.parse(s, now)


def _resolve_time(mql: dict, ctx: dict, dims: list[dict]) -> TimeInfo:
    tr = mql.get("time_range")
    if tr:
        start = timeexpr.parse(tr["start"], ctx["now"])
        end = timeexpr.parse(tr["end"], ctx["now"])
    else:
        d = _resolve_default_time(ctx["default_time"], ctx["now"])
        start = end = d
    if start > end:
        raise CompileError(
            "UNSOLVABLE",
            f"time_range 起点 {start} 晚于终点 {end}",
            path="time_range",
        )

    compare_period = None
    compare_type = None
    cmp_ = mql.get("compare")
    if cmp_:
        ctype = str(cmp_.get("type", ""))
        offset = cmp_.get("offset", 1)
        if not isinstance(offset, int) or offset < 1:
            raise CompileError(
                "BAD_MQL", f"compare.offset 必须是正整数，实际 {offset!r}", path="compare.offset"
            )
        compare_period = timeexpr.shift_period(start, end, ctype, offset)
        compare_type = ctype

    scan_start = min(start, compare_period[0]) if compare_period else start
    scan_end = max(end, compare_period[1]) if compare_period else end
    span = (scan_end - scan_start).days
    if span > ctx["max_scan_days"]:
        raise CompileError(
            "TIME_RANGE_TOO_WIDE",
            f"扫描跨度 {span} 天超过上限 {ctx['max_scan_days']} 天；"
            f"带 compare 时跨度为两期之和，放宽上限请显式传 ctx.max_scan_days",
            path="time_range",
            detail={"span_days": span, "limit": ctx["max_scan_days"]},
        )

    gran = None
    for d in dims:
        if isinstance(d, dict) and d.get("granularity"):
            gran = str(d["granularity"])
            break

    return TimeInfo(
        prop="",  # 由调用处填
        primary=(start, end),
        compare=compare_period,
        compare_type=compare_type,
        scan=(scan_start, scan_end),
        granularity=gran,
    )


# ---------------------------------------------------------------- 主流程


def compile_mql(mql: dict, ontology: Ontology, ctx: dict | None = None) -> dict:
    """编译一份 MQL。

    返回 `{"ok": True, "sql": ..., "plan": ..., "warnings": [...]}`
    或 `{"ok": False, "errors": [{"code", "path", "message", ...}]}`。

    不抛异常——错误是正常返回值的一部分。调用方要按 code 分流，
    而不是靠 try/except 猜。
    """
    try:
        return _compile(mql, ontology, ctx)
    except CompileError as exc:
        return {"ok": False, "errors": [exc.to_dict()]}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "errors": [
                {
                    "code": "INTERNAL_INVARIANT",
                    "path": "$",
                    "message": f"编译器内部断言失败：{type(exc).__name__}: {exc}",
                }
            ],
        }


def _compile(mql: dict, ontology: Ontology, ctx: dict | None) -> dict:
    # A 接收
    mql = _check_mql(mql)
    c = _normalize_ctx(ctx)

    metric_names = _names(mql["metrics"])
    dims = mql.get("dimensions") or []
    dim_names = _names(dims)
    filter_props = _names(mql.get("filters") or [], "field")

    # B 定位（在 choose 内部做交集与失败归因）
    missing = [n for n in metric_names if n not in ontology.metrics]
    if missing:
        raise CompileError(
            "UNKNOWN_METRIC",
            f"指标不在本体里：{missing}",
            path="metrics",
            candidates=tuple(_top3(missing[0], list(ontology.metrics))),
        )
    for i, n in enumerate(dim_names):
        if n not in ontology.properties:
            raise CompileError(
                "UNKNOWN_PROPERTY",
                f"维度 {n!r} 不在本体里",
                path=f"dimensions[{i}]",
                candidates=tuple(_top3(n, list(ontology.properties))),
            )
    for i, n in enumerate(filter_props):
        if n not in ontology.properties:
            raise CompileError(
                "UNKNOWN_PROPERTY",
                f"过滤字段 {n!r} 不在本体里",
                path=f"filters[{i}].field",
                candidates=tuple(_top3(n, list(ontology.properties))),
            )

    # 权限字段也必须参与「落到哪张表」的求解，否则它没有地方安放。
    # 这一点会改变选表结果：`region` 只挂在 dim_store 上，一旦要注入权限，
    # 那张没有 region 列的预聚合表就用不了了 —— 同一个问题，权限不同，
    # 编译出来的不只是 WHERE 不同，连事实表都不同。
    perm_fields: list[str] = []
    for i, p in enumerate(c["permissions"]):
        f = str(p.get("field", ""))
        if f not in ontology.properties:
            raise CompileError(
                "BAD_CONTEXT",
                f"权限字段 {f!r} 不是本体里的属性",
                path=f"ctx.permissions[{i}].field",
                candidates=tuple(_top3(f, list(ontology.properties))),
            )
        if f not in perm_fields:
            perm_fields.append(f)
    needed = filter_props + [f for f in perm_fields if f not in filter_props]

    # E 定时（先算时间，选表时要用 time property）
    time_prop = pick_time_property(ontology, dim_names)
    time_info = _resolve_time(mql, c, dims)
    time_info.prop = time_prop

    # C+D 选表与连表
    resolution = choose(ontology, metric_names, dim_names, needed, time_prop)

    # F 出码
    compiled = build(mql, ontology, c, c["dialect"], resolution, time_info)
    return {
        "ok": True,
        "sql": compiled.sql,
        "plan": compiled.plan,
        "warnings": compiled.warnings,
    }


def _top3(word: str, pool: list[str]) -> list[str]:
    """粗排候选。不求准，只求别让调用方对着空列表发呆。"""
    def score(w: str) -> tuple[int, int]:
        common = len(set(word.lower()) & set(w.lower()))
        return (-common, abs(len(w) - len(word)))

    return sorted(pool, key=score)[:3]
