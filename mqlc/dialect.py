"""方言差异。只抽象真正不一样的那几处。

不做通用方言适配层。那是个无底洞，而且和「单层聚合 + JOIN」的能力上限不匹配
——为一个用不到窗口函数的编译器去支持 12 种方言的窗口语义，是纯浪费。
"""

from __future__ import annotations

from datetime import date

from .errors import CompileError


class Dialect:
    name: str = ""
    verified: bool = False
    quote_char: str = '"'

    # ---- 基础 -------------------------------------------------------

    def quote(self, ident: str) -> str:
        q = self.quote_char
        return f"{q}{ident}{q}"

    def date_literal(self, d: date) -> str:
        return f"'{d.isoformat()}'"

    def qualified(self, alias: str, column: str) -> str:
        return f"{alias}.{column}"

    # ---- 日期运算（子类实现） ---------------------------------------

    def add_days(self, expr: str, days: int) -> str:
        raise NotImplementedError

    def trunc(self, expr: str, unit: str) -> str:
        raise NotImplementedError

    # ---- 期间表达式 -------------------------------------------------

    def period_start_expr(self, expr: str) -> str:
        """`{period_start} - 30d` 里的日期运算，翻译成本方言写法。"""
        return self.add_days(expr, -30)  # 由 render 层替换成实际天数


class SQLite(Dialect):
    name = "sqlite"
    verified = True
    quote_char = '"'

    def add_days(self, expr: str, days: int) -> str:
        return f"date({expr}, '{days} day')"

    def trunc(self, expr: str, unit: str) -> str:
        if unit == "day":
            return expr
        if unit == "week":
            # 回到本周一：先取星期几（0=周日），再回退到周一
            return (
                f"date({expr}, '-' || ((cast(strftime('%w', {expr}) as integer) + 6) % 7)"
                f" || ' day')"
            )
        if unit == "month":
            return f"strftime('%Y-%m-01', {expr})"
        if unit == "quarter":
            return (
                f"date(strftime('%Y-', {expr}) || "
                f"printf('%02d', ((cast(strftime('%m', {expr}) as integer) - 1) / 3) * 3 + 1)"
                f" || '-01')"
            )
        if unit == "year":
            return f"strftime('%Y-01-01', {expr})"
        raise CompileError("BAD_MQL", f"不支持的粒度 {unit}", path="dimensions")


class MySQL(Dialect):
    name = "mysql"
    verified = False
    quote_char = "`"

    def add_days(self, expr: str, days: int) -> str:
        return f"DATE_ADD({expr}, INTERVAL {days} DAY)"

    def trunc(self, expr: str, unit: str) -> str:
        if unit == "day":
            return expr
        if unit == "week":
            return f"DATE_SUB(DATE({expr}), INTERVAL WEEKDAY({expr}) DAY)"
        if unit == "month":
            return f"DATE_FORMAT({expr}, '%Y-%m-01')"
        if unit == "quarter":
            return f"MAKEDATE(YEAR({expr}), 1) + INTERVAL (QUARTER({expr}) - 1) QUARTER"
        if unit == "year":
            return f"DATE_FORMAT({expr}, '%Y-01-01')"
        raise CompileError("BAD_MQL", f"不支持的粒度 {unit}", path="dimensions")


class Doris(MySQL):
    name = "doris"
    verified = False


class Hive(Dialect):
    name = "hive"
    verified = False
    quote_char = "`"

    def add_days(self, expr: str, days: int) -> str:
        return f"date_add({expr}, {days})"

    def trunc(self, expr: str, unit: str) -> str:
        if unit == "day":
            return expr
        if unit == "week":
            return f"date_sub({expr}, weekday({expr}))"
        if unit == "month":
            return f"trunc({expr}, 'MM')"
        if unit == "quarter":
            return f"trunc({expr}, 'Q')"
        if unit == "year":
            return f"trunc({expr}, 'YYYY')"
        raise CompileError("BAD_MQL", f"不支持的粒度 {unit}", path="dimensions")


class SparkSQL(Hive):
    name = "sparksql"
    verified = False


_REGISTRY: dict[str, Dialect] = {
    d.name: d for d in (SQLite(), MySQL(), Doris(), Hive(), SparkSQL())
}


def get(name: str) -> Dialect:
    try:
        return _REGISTRY[str(name)]
    except KeyError:
        raise CompileError(
            "UNKNOWN_DIALECT",
            f"不支持的方言 {name!r}",
            path="ctx.dialect",
            candidates=tuple(sorted(_REGISTRY)),
        ) from None


def known() -> list[str]:
    return sorted(_REGISTRY)
