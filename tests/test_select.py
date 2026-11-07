from __future__ import annotations

from datetime import date

import pytest

from _helpers import compile_err, compile_ok, mql, rows
from mqlc import Ontology


def test_city_dimension_uses_pre_aggregated_table(onto: Ontology) -> None:
    r = compile_ok(mql(["gmv"], ["city"]), onto)
    assert r["plan"]["fact_table"] == "ads_gmv_1d"
    assert r["plan"]["fact_table_reason"] == "no_join"
    assert r["plan"]["joins"] == []


def test_store_dimension_rejects_pre_aggregated_table(onto: Ontology) -> None:
    """预聚合表的粒度是 [date, city]，撑不住 store 维度。

    它可以通过 `dim_store.city` 反查门店，但那是 city → store 的一对多，
    一个门店会拿到整个城市的量。编译器必须拒绝，而不是给出一个放大的数。
    """
    r = compile_ok(mql(["gmv"], ["store"]), onto)
    assert r["plan"]["fact_table"] == "dwd_order_di"
    assert any("ads_gmv_1d 被排除" in w for w in r["warnings"])
    assert any("一对多" in w for w in r["warnings"])


def test_two_layers_agree_on_city(onto: Ontology, db) -> None:
    """选 ads 还是选 dwd，在同一组维度上必须得到同一个数。

    如果两个层的口径不一致，那「选表」就变成了口径问题——
    而口径不该由编译器决定。这条测试守的是这个边界。
    """
    a = compile_ok(mql(["gmv"], ["city"]), onto)
    assert a["plan"]["fact_table"] == "ads_gmv_1d"
    ads_map = dict(rows(db, a["sql"]))

    # 手工强制走明细层
    manual = """
        SELECT s.city, SUM(o.pay_amount)
        FROM dwd_order_di o JOIN dim_store s ON s.store_id = o.store_id
        WHERE o.is_valid = 1 AND o.order_date BETWEEN '2026-10-01' AND '2026-10-31'
        GROUP BY s.city
    """
    for city, v in rows(db, manual):
        assert abs(ads_map[city] - v) < 1e-9


def test_metric_intersection_forces_detail_layer(onto: Ontology) -> None:
    """gmv 两处都有，refund_amount 只有明细层有 → 交集只剩明细层。"""
    r = compile_ok(mql(["gmv", "refund_amount"], ["city"]), onto)
    assert r["plan"]["fact_table"] == "dwd_order_di"
    assert r["plan"]["joins"], "city 在 dwd 上需要 JOIN dim_store"


def test_no_common_table_is_an_error(onto: Ontology) -> None:
    e = compile_err(mql(["gmv", "nonexistent_metric"]), onto)
    assert e["code"] == "UNKNOWN_METRIC"


def test_time_property_is_the_only_one(onto: Ontology) -> None:
    from mqlc.select import pick_time_property

    assert pick_time_property(onto, []) == "order_date"
    assert pick_time_property(onto, ["order_date"]) == "order_date"


def test_unknown_metric_suggests_candidates(onto: Ontology) -> None:
    e = compile_err(mql(["gmvv"], ["city"]), onto)
    assert e["code"] == "UNKNOWN_METRIC"
    assert "gmv" in e["candidates"]


def test_unknown_dimension(onto: Ontology) -> None:
    e = compile_err(mql(["gmv"], ["district"]), onto)
    assert e["code"] == "UNKNOWN_PROPERTY"


def test_join_is_merged_not_repeated(onto: Ontology, db) -> None:
    """city 维度和 scoped_filter 都要 dim_store，只能 JOIN 一次。

    重复 JOIN 的表现：SQLite 直接报错，MySQL 算出翻倍的数。后者更难受。
    """
    r = compile_ok(mql(["same_store_gmv"], ["city"]), onto)
    assert r["sql"].count("JOIN dim_store") == 1
    rows(db, r["sql"])  # 能跑就说明没重复 JOIN


def test_limit_and_sort(onto: Ontology, db) -> None:
    q = mql(["gmv"], ["store"], sort=[{"field": "gmv", "order": "desc"}], limit=3)
    r = compile_ok(q, onto)
    got = rows(db, r["sql"])
    assert len(got) == 3
    assert [g[1] for g in got] == sorted([g[1] for g in got], reverse=True)


def test_granularity_month(onto: Ontology, db) -> None:
    q = {
        "mql_version": "1.1",
        "metrics": [{"name": "gmv"}],
        "dimensions": [{"name": "order_date", "granularity": "month"}],
        "time_range": {"start": "-3m", "end": "yesterday"},
    }
    r = compile_ok(q, onto)
    got = rows(db, r["sql"])
    assert got, "应该有数据"
    assert all(len(g[0]) == 10 and g[0].endswith("-01") for g in got)


def test_compile_is_deterministic(onto: Ontology) -> None:
    q = mql(["gmv", "order_count"], ["city"], compare={"type": "yoy"})
    a = compile_ok(q, onto)
    b = compile_ok(q, onto)
    assert a["sql"] == b["sql"]
    assert a["plan"] == b["plan"]


def test_now_is_required(onto: Ontology) -> None:
    from mqlc import compile_mql

    r = compile_mql(mql(["gmv"], ["city"]), onto, {"dialect": "sqlite"})
    assert not r["ok"]
    assert r["errors"][0]["code"] == "BAD_CONTEXT"


def test_default_time_when_missing(onto: Ontology) -> None:
    q = {"mql_version": "1.1", "metrics": [{"name": "gmv"}], "dimensions": [{"name": "city"}]}
    r = compile_ok(q, onto)
    assert r["plan"]["time"]["primary"] == {"start": "2026-11-06", "end": "2026-11-06"}
