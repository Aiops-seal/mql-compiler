"""14 个错误码，每个至少一个触发用例。

这不是覆盖率游戏。它的用途是：以后改代码时，如果某个错误退化成
`KeyError` 或被静默吞掉，会有一条测试变红，而不是等到线上算错数。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import mqlc
from _helpers import compile_err, mql
from mqlc.errors import CODES
from mqlc.ontology import Ontology
from mqlc.select import try_resolve

ROOT = Path(__file__).resolve().parent.parent
RAW = json.loads((ROOT / "fixtures" / "ontology.json").read_text(encoding="utf-8"))


def mutated(fn) -> Ontology:
    d = copy.deepcopy(RAW)
    fn(d)
    return Ontology.from_dict(d)


def test_code_table_is_frozen() -> None:
    assert len(CODES) == 14


# ---- A 阶段 ------------------------------------------------------------


def test_bad_mql_not_an_object(onto: Ontology) -> None:
    assert compile_err("not a dict", onto)["code"] == "BAD_MQL"  # type: ignore[arg-type]


def test_bad_mql_empty_metrics(onto: Ontology) -> None:
    e = compile_err({"mql_version": "1.1", "metrics": []}, onto)
    assert e["code"] == "BAD_MQL"
    assert "metrics" in e["path"]


def test_bad_mql_unknown_top_level_field(onto: Ontology) -> None:
    """严格模式：拼错一个字段名，不该被静默忽略。"""
    q = mql(["gmv"], ["city"])
    q["dimension"] = [{"name": "city"}]
    e = compile_err(q, onto)
    assert e["code"] == "BAD_MQL"
    assert "dimension" in e["path"]


def test_bad_context_without_now(onto: Ontology) -> None:
    r = mqlc.compile_mql(mql(["gmv"]), onto, {"dialect": "sqlite"})
    assert r["errors"][0]["code"] == "BAD_CONTEXT"


def test_bad_context_permission_field_unknown(onto: Ontology) -> None:
    e = compile_err(
        mql(["gmv"]), onto, permissions=[{"field": "district", "operator": "in", "values": ["x"]}]
    )
    assert e["code"] == "BAD_CONTEXT"


def test_unknown_dialect(onto: Ontology) -> None:
    e = compile_err(mql(["gmv"]), onto, dialect="postgres")
    assert e["code"] == "UNKNOWN_DIALECT"
    assert "sqlite" in e["candidates"]


def test_bad_ontology() -> None:
    d = copy.deepcopy(RAW)
    d["metrics"][0]["bindings"][0]["expr"] = "SUM(pay_amount)"
    with pytest.raises(Exception) as ei:
        Ontology.from_dict(d)
    assert getattr(ei.value, "code", "") == "BAD_ONTOLOGY"


# ---- B 阶段 ------------------------------------------------------------


def test_unknown_metric(onto: Ontology) -> None:
    assert compile_err(mql(["gmv_ttm"], ["city"]), onto)["code"] == "UNKNOWN_METRIC"


def test_no_candidate_table(onto_with_ads_only: Ontology) -> None:
    """两个指标分属两张事实表，交集为空。

    本编译器不支持跨事实表计算——这是刻意的（数仓里最容易出 90% 错数的地方）。
    """
    e = compile_err(mql(["ads_only", "refund_amount"]), onto_with_ads_only)
    assert e["code"] == "NO_CANDIDATE_TABLE"
    assert e["detail"]["ads_only"] == ["ads_gmv_1d"]
    assert e["detail"]["refund_amount"] == ["dwd_order_di"]


# ---- D 阶段 ------------------------------------------------------------


def test_unknown_property(onto: Ontology) -> None:
    assert compile_err(mql(["gmv"], ["district"]), onto)["code"] == "UNKNOWN_PROPERTY"


def test_uncovered_dimension(onto_with_ads_only: Ontology) -> None:
    """category 只在 dim_product 上，而预聚合表到 dim_product 没有路径。"""
    e = compile_err(mql(["ads_only"], ["category"]), onto_with_ads_only)
    assert e["code"] == "UNCOVERED_DIMENSION"


def test_fanout_risk(onto: Ontology) -> None:
    e = compile_err(
        mql(["gmv"], ["store"]),
        mutated(lambda d: d["metrics"][0]["bindings"].pop(0)),  # 删掉明细层，只剩 ads
    )
    assert e["code"] == "FANOUT_RISK"
    assert "一对多" in e["message"]


def test_ambiguous_join() -> None:
    """同一个属性能从两张等距的表取到。不同路径可能算出不同的数，编译器不替你选。"""

    def add(d):
        d["properties"].append(
            {
                "name": "tag",
                "display": "标签",
                "kind": "enum",
                "bindings": [
                    {"table": "dim_store", "column": "store_type"},
                    {"table": "dim_product", "column": "category"},
                ],
            }
        )

    onto = mutated(add)
    with pytest.raises(mqlc.CompileError) as ei:
        try_resolve(onto, "dwd_order_di", ["gmv"], ["tag"], [], "order_date")
    assert ei.value.code == "AMBIGUOUS_JOIN"


# ---- E 阶段 ------------------------------------------------------------


def test_bad_time_expr(onto: Ontology) -> None:
    q = mql(["gmv"], ["city"], time_range={"start": "next week", "end": "yesterday"})
    assert compile_err(q, onto)["code"] == "BAD_TIME_EXPR"


def test_time_range_too_wide(onto: Ontology) -> None:
    q = mql(["gmv"], ["city"], time_range={"start": "2020-01-01", "end": "2026-10-31"})
    e = compile_err(q, onto, max_scan_days=365)
    assert e["code"] == "TIME_RANGE_TOO_WIDE"
    assert e["detail"]["span_days"] > 365


def test_time_range_too_wide_by_compare_span(onto: Ontology) -> None:
    """带 compare 时跨度是两期之和，所以更容易撞上限。"""
    q = mql(
        ["gmv"],
        ["city"],
        time_range={"start": "-3y", "end": "yesterday"},
        compare={"type": "yoy"},
    )
    e = compile_err(q, onto, max_scan_days=1000)
    assert e["code"] == "TIME_RANGE_TOO_WIDE"


def test_unsolvable_start_after_end(onto: Ontology) -> None:
    q = mql(["gmv"], ["city"], time_range={"start": "2026-10-31", "end": "2026-10-01"})
    assert compile_err(q, onto)["code"] == "UNSOLVABLE"


def test_unsolvable_compare_on_non_aggregate(onto: Ontology) -> None:
    """compare 的分期靠给聚合套 CASE，非聚合表达式套不上。"""

    def replace(d):
        d["metrics"][0]["bindings"][1]["expr"] = "{f}.gmv"

    e = compile_err(
        mql(["gmv"], ["city"], compare={"type": "yoy"}), mutated(replace)
    )
    assert e["code"] == "UNSOLVABLE"


# ---- F 阶段 ------------------------------------------------------------


def test_internal_invariant_is_caught_not_raised(onto: Ontology, monkeypatch) -> None:
    """内部断言失败要变成明确错误，而不是把 traceback 甩给调用方。"""

    def boom(*a, **kw):
        raise RuntimeError("假设不成立")

    monkeypatch.setattr(mqlc, "choose", boom)
    r = mqlc.compile_mql(mql(["gmv"], ["city"]), onto, {"now": "2026-11-07"})
    assert not r["ok"]
    assert r["errors"][0]["code"] == "INTERNAL_INVARIANT"


# ---- fixture -----------------------------------------------------------

@pytest.fixture(scope="module")
def onto_with_ads_only() -> Ontology:
    def add(d):
        d["metrics"].append(
            {
                "name": "ads_only",
                "display": "只在预聚合层的指标",
                "bindings": [{"table": "ads_gmv_1d", "expr": "SUM({f}.gmv)", "requires": []}],
            }
        )

    return mutated(add)
