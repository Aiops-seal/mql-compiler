"""测试用的共享小工具。

单独放一个文件，而不是塞进 conftest——conftest 由 pytest 当插件加载，
从测试模块里 `from conftest import x` 依赖导入顺序，容易在不同版本上翻车。
"""

from __future__ import annotations

import sqlite3

from mqlc import Ontology, compile_mql

NOW = "2026-11-07"


def compile_ok(mql: dict, ontology: Ontology, **ctx) -> dict:
    r = compile_mql(mql, ontology, {"now": NOW, **ctx})
    assert r["ok"], r.get("errors")
    return r


def compile_err(mql: dict, ontology: Ontology, **ctx) -> dict:
    r = compile_mql(mql, ontology, {"now": NOW, **ctx})
    assert not r["ok"], f"本应失败，却成功了：{r.get('sql')}"
    return r["errors"][0]


def rows(conn: sqlite3.Connection, sql: str) -> list[tuple]:
    return conn.execute(sql).fetchall()


def scalar(conn: sqlite3.Connection, sql: str) -> float:
    v = conn.execute(sql).fetchone()[0]
    return float(v) if v is not None else 0.0


def mql(metrics: list[str], dims: list[str] | None = None, **extra) -> dict:
    out: dict = {
        "mql_version": "1.1",
        "metrics": [{"name": m} for m in metrics],
        "time_range": {"start": "last_month_start", "end": "last_month_end"},
    }
    if dims:
        out["dimensions"] = [{"name": d} for d in dims]
    out.update(extra)
    return out
