from __future__ import annotations

from datetime import date

import pytest

from mqlc.errors import CompileError
from mqlc.timeexpr import month_end, month_start, parse, shift_months, shift_period

NOW = date(2026, 11, 7)


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("today", date(2026, 11, 7)),
        ("yesterday", date(2026, 11, 6)),
        ("2026-10-01", date(2026, 10, 1)),
        ("month_start", date(2026, 11, 1)),
        ("month_end", date(2026, 11, 30)),
        ("last_month_start", date(2026, 10, 1)),
        ("last_month_end", date(2026, 10, 31)),
        ("-7d", date(2026, 10, 31)),
        ("-2w", date(2026, 10, 24)),
        ("-1m", date(2026, 10, 7)),
        ("-1y", date(2025, 11, 7)),
    ],
)
def test_parse(expr: str, expected: date) -> None:
    assert parse(expr, NOW) == expected


def test_month_shift_clamps_to_month_end() -> None:
    """3 月 31 日往前一个月是 2 月 28 日，不是「30 天前」。

    用后者会让月度报表在 2、3 月对不上，而且只在跨月对账时暴露。
    """
    assert shift_months(date(2026, 3, 31), -1) == date(2026, 2, 28)
    assert shift_months(date(2024, 3, 31), -1) == date(2024, 2, 29)  # 闰年
    assert shift_months(date(2026, 1, 15), -1) == date(2025, 12, 15)


def test_month_helpers() -> None:
    assert month_start(date(2026, 2, 17)) == date(2026, 2, 1)
    assert month_end(date(2026, 2, 17)) == date(2026, 2, 28)


@pytest.mark.parametrize("bad", ["", "next week", "2026-13-01", "yesterday-ish", "-1x"])
def test_bad_expression(bad: str) -> None:
    with pytest.raises(CompileError) as ei:
        parse(bad, NOW)
    assert ei.value.code == "BAD_TIME_EXPR"


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("yoy", (date(2025, 10, 1), date(2025, 10, 31))),
        ("mom", (date(2026, 9, 1), date(2026, 9, 30))),
        ("wow", (date(2026, 9, 24), date(2026, 10, 24))),
    ],
)
def test_shift_period(kind: str, expected: tuple[date, date]) -> None:
    assert shift_period(date(2026, 10, 1), date(2026, 10, 31), kind, 1) == expected


def test_shift_period_offset_two() -> None:
    assert shift_period(date(2026, 10, 1), date(2026, 10, 31), "yoy", 2) == (
        date(2024, 10, 1),
        date(2024, 10, 31),
    )
