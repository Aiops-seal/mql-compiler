from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from mqlc.errors import CompileError
from mqlc.ontology import Ontology

ROOT = Path(__file__).resolve().parent.parent
RAW = json.loads((ROOT / "fixtures" / "ontology.json").read_text(encoding="utf-8"))


def mutated(fn) -> dict:
    d = copy.deepcopy(RAW)
    fn(d)
    return d


def load_bad(d: dict) -> CompileError:
    with pytest.raises(CompileError) as ei:
        Ontology.from_dict(d)
    assert ei.value.code == "BAD_ONTOLOGY"
    return ei.value


def test_load_fixture(onto: Ontology) -> None:
    assert set(onto.tables) == {
        "dwd_order_di",
        "ads_gmv_1d",
        "dim_store",
        "dim_product",
        "dim_date",
    }
    assert onto.metric_tables("gmv") == {"dwd_order_di", "ads_gmv_1d"}
    assert onto.metric_tables("refund_amount") == {"dwd_order_di"}


def test_relations_are_bidirectional_with_inverted_cardinality(onto: Ontology) -> None:
    out = {h.dst: h for h in onto.neighbors("dwd_order_di")}
    assert out["dim_store"].cardinality == "many_to_one"

    back = {h.dst: h for h in onto.neighbors("ads_gmv_1d")}
    assert back["dim_store"].cardinality == "one_to_many", "反过来必须是一对多"


def test_column_lookup(onto: Ontology) -> None:
    assert onto.column_for_property("city", "ads_gmv_1d") == "city"
    assert onto.column_for_property("city", "dwd_order_di") is None  # 要走 JOIN
    assert onto.column_for_property("store", "dwd_order_di") == "store_id"


def test_missing_columns() -> None:
    d = mutated(lambda x: x["tables"][0].pop("columns"))
    e = load_bad(d)
    assert "columns" in e.message


def test_fact_table_needs_layer() -> None:
    def drop(d):
        d["tables"][0].pop("layer")

    e = load_bad(mutated(drop))
    assert "layer" in e.message


def test_metric_binding_must_have_f_prefix() -> None:
    """表达式里不带 {f}. 前缀，JOIN 之后列名会撞，必须提前拒绝。"""

    def strip(d):
        d["metrics"][0]["bindings"][0]["expr"] = "SUM(pay_amount)"

    e = load_bad(mutated(strip))
    assert "{f}" in e.message


def test_metric_binding_cannot_point_to_dimension() -> None:
    def bad(d):
        d["metrics"][0]["bindings"][0]["table"] = "dim_store"

    e = load_bad(mutated(bad))
    assert "事实表" in e.message


def test_property_binding_may_point_to_dimension(onto: Ontology) -> None:
    """属性挂在维表上是正常的——上面那条限制只对指标成立。"""
    assert onto.prop("city").bindings[0].table == "dim_store"


def test_condition_cannot_reference_foreign_table() -> None:
    """条件里出现白名单外的表名，等于绕过 binding 直接指定物理结构。"""

    def leak(d):
        d["metrics"][0]["bindings"][0]["requires"] = ["dim_store.region = 'R01'"]

    e = load_bad(mutated(leak))
    assert "dim_store" in e.message


def test_scoped_filter_may_reference_its_own_table(onto: Ontology) -> None:
    m = onto.metric("same_store_gmv")
    assert m is not None
    assert m.scoped_filters[0].table == "dim_store"
    assert "dim_store" in m.scoped_filters[0].condition


def test_relation_column_must_exist() -> None:
    def bad(d):
        d["relations"][0]["on"] = "no_such_column"

    e = load_bad(mutated(bad))
    assert "no_such_column" in e.message


def test_invalid_cardinality() -> None:
    def bad(d):
        d["relations"][0]["cardinality"] = "lots_to_one"

    e = load_bad(mutated(bad))
    assert "cardinality" in e.message


def test_missing_top_level_key() -> None:
    d = mutated(lambda x: x.pop("properties"))
    e = load_bad(d)
    assert "properties" in e.message
