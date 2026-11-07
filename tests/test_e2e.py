"""端到端：编译出的 SQL 真在 SQLite 上跑，断言数值。

这里的每个数字都能用 CLI 复现：

    python -m mqlc run examples/q01-gmv-by-city.json --now 2026-11-07

为什么不断言 SQL 字符串：排版会变，语义不该变。断言字符串的测试会在
每一次无害的重构里变红，然后被人加 `--ignore`。断言数值不会。
"""

from __future__ import annotations

from _helpers import NOW, compile_ok, mql, rows, scalar
from mqlc import Ontology

# 2026-10 全量，明细层与预聚合层一致
GMV_BY_CITY = {"HZ": 99324.0, "NB": 59840.0, "SZ": 59192.0}
GMV_TOTAL = 218356.0

GMV_BY_STORE = {
    "S01": 34466.0,
    "S02": 30789.0,
    "S03": 34069.0,
    "S04": 33904.0,
    "S05": 25936.0,
    "S06": 28511.0,
    "S07": 30681.0,
}

# 2025-10 同期
YOY_BY_CITY = {"HZ": 54353.0, "NB": 61808.0, "SZ": 54632.0}


def test_gmv_by_city(onto: Ontology, db) -> None:
    r = compile_ok(mql(["gmv"], ["city"]), onto)
    got = dict(rows(db, r["sql"]))
    assert got == GMV_BY_CITY


def test_gmv_total_no_dimension(onto: Ontology, db) -> None:
    r = compile_ok(mql(["gmv"]), onto)
    assert scalar(db, r["sql"]) == GMV_TOTAL


def test_gmv_by_store(onto: Ontology, db) -> None:
    r = compile_ok(mql(["gmv"], ["store"]), onto)
    got = dict(rows(db, r["sql"]))
    assert got == GMV_BY_STORE


def test_yoy_primary_period_is_not_the_union(onto: Ontology, db) -> None:
    """带 compare 时 WHERE 扫两期并集，主列必须用 CASE 收回来。

    写成「主列不加 CASE」的版本，HZ 会算出 84 万多（两期一起加总），
    看着像是暴涨，实际上是 bug。
    """
    r = compile_ok(mql(["gmv"], ["city"], compare={"type": "yoy"}), onto)
    assert "CASE WHEN" in r["sql"]
    got = {c: (p, q) for c, p, q in rows(db, r["sql"])}
    assert {c: v[0] for c, v in got.items()} == GMV_BY_CITY
    assert {c: v[1] for c, v in got.items()} == YOY_BY_CITY
    assert r["plan"]["time"]["primary"] == {"start": "2026-10-01", "end": "2026-10-31"}
    assert r["plan"]["time"]["compare"]["start"] == "2025-10-01"
    assert r["plan"]["time"]["scan_range"] == {"start": "2025-10-01", "end": "2026-10-31"}


def test_same_store_excludes_new_stores(onto: Ontology, db) -> None:
    """同店口径把 2026 年新开的 S03、S07 排除。

    HZ: 99324 - 34069(S03) = 65255
    SZ: 59192 - 30681(S07) = 28511
    NB: 两家都是老店，不变
    """
    r = compile_ok(mql(["same_store_gmv"], ["city"]), onto)
    got = dict(rows(db, r["sql"]))
    assert got == {"HZ": 65255.0, "NB": 59840.0, "SZ": 28511.0}


def test_same_store_uses_primary_period_for_instantiation(onto: Ontology) -> None:
    """scoped_filter 必须用主期起点实例化，不是对比期。

    用对比期会得到 `open_date <= 2025-10-01 - 365d`，把主期新开的店也当成老店。
    """
    r = compile_ok(
        mql(["same_store_gmv"], ["city"], compare={"type": "yoy"}), onto
    )
    inst = r["plan"]["scoped_filters_instantiated"]
    assert len(inst) == 1
    assert "2026-10-01" in inst[0]["sql"]
    assert "2025-10-01" not in inst[0]["sql"]


def test_permission_changes_the_sql_and_the_row_count(onto: Ontology, db) -> None:
    """同一份 MQL，两种权限：行数不同，但重叠部分的数必须一致。

    这条是整套设计的落点——权限不在模型手里，也不在 MQL 里，
    它是编译入参。模型看到的永远是同一份问题。
    """
    q = mql(["gmv", "order_count"], ["city"])
    hq = compile_ok(q, onto)
    rm = compile_ok(q, onto, permissions=[{"field": "region", "operator": "in", "values": ["R01"]}])

    assert hq["plan"]["fact_table"] == "ads_gmv_1d"
    assert rm["plan"]["fact_table"] == "dwd_order_di", "权限字段只挂在 dim_store 上，预聚合表撑不住"

    hq_rows = dict((r[0], r[1:]) for r in rows(db, hq["sql"]))
    rm_rows = dict((r[0], r[1:]) for r in rows(db, rm["sql"]))
    assert len(hq_rows) == 3
    assert len(rm_rows) == 2
    for city, vals in rm_rows.items():
        assert vals == hq_rows[city], f"{city} 在两种权限下数不一致"


def test_permission_filter_is_recorded_in_plan(onto: Ontology) -> None:
    r = compile_ok(
        mql(["gmv"], ["city"]),
        onto,
        permissions=[{"field": "region", "operator": "in", "values": ["R01", "R02"]}],
    )
    sources = [f["source"] for f in r["plan"]["injected_filters"]]
    assert "permission" in sources
    perm = [f for f in r["plan"]["injected_filters"] if f["source"] == "permission"][0]
    assert "R01" in perm["sql"] and "R02" in perm["sql"]


def test_mql_filter(onto: Ontology, db) -> None:
    q = mql(["gmv"], ["store"], filters=[{"field": "channel", "operator": "eq", "value": "app"}])
    r = compile_ok(q, onto)
    assert "channel" in r["sql"]
    got = sum(row[1] for row in rows(db, r["sql"]))
    manual = scalar(
        db,
        "SELECT SUM(pay_amount) FROM dwd_order_di WHERE is_valid=1 AND channel='app' "
        "AND order_date BETWEEN '2026-10-01' AND '2026-10-31'",
    )
    assert abs(got - manual) < 1e-9


def test_mixed_metrics_across_layers(onto: Ontology, db) -> None:
    """avg_order_amount 只有明细层有，gmv 两层都有 → 只能走明细层。"""
    r = compile_ok(mql(["gmv", "avg_order_amount"], ["city"]), onto)
    assert r["plan"]["fact_table"] == "dwd_order_di"
    got = {row[0]: row[1:] for row in rows(db, r["sql"])}
    assert got["HZ"][0] == 99324.0
    assert got["HZ"][1] != got["NB"][1]


def test_unknown_dialect_marks_unverified(onto: Ontology) -> None:
    r = compile_ok(mql(["gmv"], ["city"]), onto, dialect="hive")
    assert r["plan"]["dialect_verified"] is False
    assert any("未验证" in w for w in r["warnings"])


def test_pre_aggregated_table_would_have_been_wrong(db) -> None:
    """手工证明「选错表」真的会算错，而不是碰巧对。

    预聚合表按 city 存，硬要拿它回答 store 维度的问题，只能靠 city 反查门店，
    结果是每个门店都拿到整个城市的量。放大倍数在 1.76x ~ 3.23x 之间——
    数字看着合理，所以这类错在业务侧对不出来。

    这也是编译器必须直接拒绝、而不是给个 warning 的原因。
    """
    bad = """
        SELECT s.store_id, SUM(a.gmv)
        FROM ads_gmv_1d a JOIN dim_store s ON s.city = a.city
        WHERE a.date BETWEEN '2026-10-01' AND '2026-10-31'
        GROUP BY s.store_id
    """
    inflated = dict(rows(db, bad))
    assert inflated["S01"] > GMV_BY_STORE["S01"] * 2

    correct = """
        SELECT store_id, SUM(pay_amount) FROM dwd_order_di
        WHERE is_valid = 1 AND order_date BETWEEN '2026-10-01' AND '2026-10-31'
        GROUP BY store_id
    """
    assert dict(rows(db, correct)) == GMV_BY_STORE


def test_plan_snapshot(onto: Ontology) -> None:
    """plan 是稳定接口，做快照；sql 不做。"""
    r = compile_ok(
        mql(["gmv", "order_count"], ["city"], compare={"type": "yoy", "offset": 1}), onto
    )
    plan = r["plan"]
    assert plan["fact_table"] == "ads_gmv_1d"
    assert plan["fact_table_reason"] == "no_join"
    assert plan["tables_used"] == ["ads_gmv_1d"]
    assert plan["dialect"] == "sqlite" and plan["dialect_verified"] is True
    assert [m["name"] for m in plan["metrics"]] == ["gmv", "order_count"]
    assert [p["name"] for p in plan["properties"]] == ["city"]
    assert plan["time"]["compare"]["type"] == "yoy"
    assert plan["scoped_filters_instantiated"] == []
