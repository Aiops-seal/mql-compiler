"""本体加载与查询。

和 mql-spec 的关系
------------------
两边的本体接口**不一样厚**：校验器只问「这个指标存不存在」（7 个布尔方法），
编译器要问「它在哪张表、哪一列、怎么连」。所以这里是另一份接口。

**但共用同一份本体文件。** 代码不共享、数据共享——这是「数据契约」这个词的
具体含义。两边各读各的，谁也不 import 谁，也就没有「改了一边忘了另一边」
的隐患：文件格式一变，两边会同时炸，而不是一边悄悄错。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import CompileError

_F_PREFIX = re.compile(r"\{f\}\.([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class Table:
    name: str
    kind: str          # fact | dimension
    layer: str         # detail | agg（仅 fact）
    grain: tuple[str, ...]
    columns: dict[str, str]


@dataclass(frozen=True)
class Relation:
    src: str
    dst: str
    src_col: str
    dst_col: str
    cardinality: str   # 从 src 看 dst 的基数


@dataclass(frozen=True)
class Binding:
    table: str
    expr: str = ""                       # 指标用
    column: str = ""                     # 属性用
    requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScopedFilter:
    table: str
    src_col: str
    dst_col: str
    condition: str


@dataclass(frozen=True)
class Metric:
    name: str
    display: str
    bindings: tuple[Binding, ...]
    scoped_filters: tuple[ScopedFilter, ...] = ()


@dataclass(frozen=True)
class Property:
    name: str
    display: str
    kind: str          # enum | string | number | time
    bindings: tuple[Binding, ...] = ()


@dataclass(frozen=True)
class Hop:
    """一条 JOIN 边，方向已经确定（从事实表往外走）。"""

    src: str
    dst: str
    src_col: str
    dst_col: str
    cardinality: str   # 从 src 看 dst


def _bad(msg: str, path: str = "ontology") -> CompileError:
    return CompileError("BAD_ONTOLOGY", msg, path=path)


def _as_pair(on: object, src: str) -> tuple[str, str]:
    if isinstance(on, str):
        return on, on
    if isinstance(on, (list, tuple)) and len(on) == 2:
        return str(on[0]), str(on[1])
    raise _bad(f"relation.on 必须是字符串或两元素数组，实际是 {on!r}")


def _check_sql_refs(cond: str, allowed_tables: set[str], path: str) -> None:
    """条件字符串里出现的表名必须在白名单内（SPEC §3.2.6）。"""
    for token in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\.", cond):
        if token in allowed_tables:
            continue
        raise _bad(
            f"条件 {cond!r} 里引用了表 {token!r}，不在允许范围内 {sorted(allowed_tables)}",
            path,
        )


class Ontology:
    """编译期的本体视图。只读。"""

    def __init__(
        self,
        tables: dict[str, Table],
        relations: tuple[Relation, ...],
        metrics: dict[str, Metric],
        properties: dict[str, Property],
        domain: str = "",
    ) -> None:
        self.tables = tables
        self.relations = relations
        self.metrics = metrics
        self.properties = properties
        self.domain = domain
        self._adj: dict[str, list[Hop]] = {name: [] for name in tables}
        for rel in relations:
            self._adj[rel.src].append(
                Hop(rel.src, rel.dst, rel.src_col, rel.dst_col, rel.cardinality)
            )
            self._adj[rel.dst].append(
                Hop(rel.dst, rel.src, rel.dst_col, rel.src_col, _invert(rel.cardinality))
            )

    # ---- 加载 -------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "Ontology":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "Ontology":
        if not isinstance(raw, dict):
            raise _bad("本体顶层必须是对象")
        for key in ("tables", "metrics", "properties"):
            if key not in raw:
                raise _bad(f"本体缺少 {key!r}")

        tables: dict[str, Table] = {}
        for i, t in enumerate(raw["tables"]):
            path = f"tables[{i}]"
            if not isinstance(t, dict) or "name" not in t:
                raise _bad("表定义必须是带 name 的对象", path)
            name = str(t["name"])
            kind = str(t.get("kind", ""))
            if kind not in ("fact", "dimension"):
                raise _bad(f"表 {name} 的 kind 必须是 fact 或 dimension", path)
            columns = t.get("columns")
            if not isinstance(columns, dict) or not columns:
                raise _bad(f"表 {name} 缺少 columns", path)
            layer = str(t.get("layer", ""))
            grain = tuple(t.get("grain") or ())
            if kind == "fact":
                if layer not in ("detail", "agg"):
                    raise _bad(f"事实表 {name} 必须声明 layer=detail|agg", path)
                if not grain:
                    raise _bad(f"事实表 {name} 必须声明 grain", path)
                for col in grain:
                    if col not in columns:
                        raise _bad(f"表 {name} 的 grain 列 {col} 不在 columns 里", path)
            tables[name] = Table(name, kind, layer, grain, dict(columns))

        relations: list[Relation] = []
        for i, r in enumerate(raw.get("relations") or []):
            path = f"relations[{i}]"
            if not isinstance(r, dict):
                raise _bad("关系必须是对象", path)
            src, dst = str(r.get("from", "")), str(r.get("to", ""))
            if src not in tables or dst not in tables:
                raise _bad(f"关系两端必须都是已声明的表：{src} -> {dst}", path)
            card = str(r.get("cardinality", ""))
            if card not in ("many_to_one", "one_to_many", "one_to_one"):
                raise _bad(f"cardinality 非法：{card!r}", path)
            sc, dc = _as_pair(r.get("on"), src)
            for tname, col in ((src, sc), (dst, dc)):
                if col not in tables[tname].columns:
                    raise _bad(f"关系列 {tname}.{col} 不在该表的 columns 里", path)
            relations.append(Relation(src, dst, sc, dc, card))

        metrics: dict[str, Metric] = {}
        for i, m in enumerate(raw["metrics"]):
            path = f"metrics[{i}]"
            if not isinstance(m, dict) or "name" not in m:
                raise _bad("指标必须是带 name 的对象", path)
            name = str(m["name"])
            bindings = _load_bindings(m.get("bindings"), tables, path, need_expr=True)
            scoped = []
            for j, sf in enumerate(m.get("scoped_filters") or []):
                spath = f"{path}.scoped_filters[{j}]"
                if not isinstance(sf, dict):
                    raise _bad("scoped_filter 必须是对象", spath)
                tname = str(sf.get("table", ""))
                if tname not in tables:
                    raise _bad(f"scoped_filter 的表 {tname!r} 不存在", spath)
                sc, dc = _as_pair(sf.get("on"), tname)
                for tt, col in ((tname, sc), (tname, dc)):
                    if col not in tables[tt].columns:
                        raise _bad(f"scoped_filter 的列 {col} 不在 {tt}.columns 里", spath)
                cond = str(sf.get("condition", "")).strip()
                if not cond:
                    raise _bad("scoped_filter 缺少 condition", spath)
                _check_sql_refs(cond, {"f", tname}, spath)
                scoped.append(ScopedFilter(tname, sc, dc, cond))
            metrics[name] = Metric(name, str(m.get("display", name)), bindings, tuple(scoped))

        properties: dict[str, Property] = {}
        for i, p in enumerate(raw["properties"]):
            path = f"properties[{i}]"
            if not isinstance(p, dict) or "name" not in p:
                raise _bad("属性必须是带 name 的对象", path)
            name = str(p["name"])
            kind = str(p.get("kind", "string"))
            if kind not in ("enum", "string", "number", "time"):
                raise _bad(f"属性 {name} 的 kind 非法：{kind!r}", path)
            bindings = _load_bindings(
                p.get("bindings"), tables, path, need_expr=False, fact_only=False
            )
            properties[name] = Property(name, str(p.get("display", name)), kind, bindings)

        return cls(tables, tuple(relations), metrics, properties, str(raw.get("domain", "")))

    # ---- 查询 -------------------------------------------------------

    def table(self, name: str) -> Table:
        try:
            return self.tables[name]
        except KeyError:
            raise CompileError("INTERNAL_INVARIANT", f"表 {name} 不存在", path="$") from None

    def metric(self, name: str) -> Metric | None:
        return self.metrics.get(name)

    def prop(self, name: str) -> Property | None:
        return self.properties.get(name)

    def neighbors(self, name: str) -> list[Hop]:
        return self._adj.get(name, [])

    def metric_tables(self, metric_name: str) -> set[str]:
        m = self.metrics.get(metric_name)
        return {b.table for b in m.bindings} if m else set()

    def binding_for_metric(self, metric_name: str, table: str) -> Binding | None:
        m = self.metrics.get(metric_name)
        if not m:
            return None
        for b in m.bindings:
            if b.table == table:
                return b
        return None

    def column_for_property(self, prop_name: str, table: str) -> str | None:
        """属性在这张表上的列名；没有则 None（需要 JOIN）。"""
        p = self.properties.get(prop_name)
        if not p:
            return None
        for b in p.bindings:
            if b.table == table:
                return b.column
        return None

    def tables_for_property(self, prop_name: str) -> list[str]:
        p = self.properties.get(prop_name)
        return [b.table for b in p.bindings] if p else []


def _invert(card: str) -> str:
    if card == "many_to_one":
        return "one_to_many"
    if card == "one_to_many":
        return "many_to_one"
    return "one_to_one"


def _load_bindings(
    raw: object,
    tables: dict[str, Table],
    path: str,
    need_expr: bool,
    fact_only: bool = True,
) -> tuple[Binding, ...]:
    if not isinstance(raw, list) or not raw:
        raise _bad("bindings 必须是非空数组", path)
    out: list[Binding] = []
    for i, b in enumerate(raw):
        bpath = f"{path}.bindings[{i}]"
        if not isinstance(b, dict):
            raise _bad("binding 必须是对象", bpath)
        tname = str(b.get("table", ""))
        if tname not in tables:
            raise _bad(f"binding 指向的表 {tname!r} 不存在", bpath)
        # 指标只能在事实表上算；属性天然挂在维表上，不能套用同一条限制
        if fact_only and tables[tname].kind != "fact":
            raise _bad(f"指标 binding 只能指向事实表，{tname} 是维表", bpath)

        if need_expr:
            expr = str(b.get("expr", "")).strip()
            if not expr:
                raise _bad("指标 binding 缺少 expr", bpath)
            cols = _F_PREFIX.findall(expr)
            if not cols:
                # 没有 {f}. 前缀的表达式一定会在 JOIN 后撞名，直接拒绝
                raise _bad(
                    f"expr {expr!r} 里的列名必须带 {{f}}. 前缀，否则 JOIN 后无法消歧",
                    bpath,
                )
            for col in cols:
                if col not in tables[tname].columns:
                    raise _bad(f"expr 引用的列 {tname}.{col} 不存在", bpath)
            requires = tuple(str(x) for x in (b.get("requires") or ()))
            for cond in requires:
                _check_sql_refs(cond, {"f", tname}, bpath)
            out.append(Binding(tname, expr=expr, requires=requires))
        else:
            col = str(b.get("column", "")).strip()
            if col not in tables[tname].columns:
                raise _bad(f"属性 binding 的列 {tname}.{col} 不存在", bpath)
            out.append(Binding(tname, column=col))
    return tuple(out)
