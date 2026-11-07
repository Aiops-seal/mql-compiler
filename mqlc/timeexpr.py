"""时间表达式解析与期间平移。

所有解析都基于调用方注入的 `now`，**不读系统时钟**。
这看起来只是把 `date.today()` 换个地方写，实际是「可重放」的前提：
同一份 MQL 三个月后重编译，必须得到同一段 SQL，否则审计时说不清
「当时那个数是怎么算的」。
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from .errors import CompileError

_ABS = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_REL = re.compile(r"^-(\d+)([dwmy])$")

_UNIT_DAYS = {"d": 1, "w": 7}


def shift_months(d: date, n: int) -> date:
    """按日历月平移，日超出目标月长度时 clamp 到月末。

    这是 `-1m` 的语义：3 月 31 日往前一个月是 2 月 28 日（或 29 日），
    不是「30 天前」。用后者会让月度报表在 2 月和 3 月对不上，
    而那种错只有在跨月对账时才会暴露。
    """
    total = d.year * 12 + (d.month - 1) + n
    year, month = divmod(total, 12)
    month += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def parse(expr: str, now: date) -> date:
    """把一个时间表达式解析成绝对日期。

    支持：`today` / `yesterday` / `YYYY-MM-DD` / `-Nd|-Nw|-Nm|-Ny` /
    `month_start` / `month_end` / `last_month_start` / `last_month_end`
    """
    text = str(expr).strip()
    if not text:
        raise CompileError("BAD_TIME_EXPR", "时间表达式为空", path="time_range")

    if text == "today":
        return now
    if text == "yesterday":
        return now - timedelta(days=1)
    if text == "month_start":
        return month_start(now)
    if text == "month_end":
        return month_end(now)
    if text == "last_month_start":
        return month_start(shift_months(now, -1))
    if text == "last_month_end":
        prev = shift_months(now, -1)
        return month_end(prev)

    m = _ABS.match(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError as exc:
            raise CompileError(
                "BAD_TIME_EXPR", f"不是合法日期：{text}（{exc}）", path="time_range"
            ) from None

    m = _REL.match(text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit == "m":
            return shift_months(now, -n)
        if unit == "y":
            return shift_months(now, -12 * n)
        return now - timedelta(days=_UNIT_DAYS[unit] * n)

    raise CompileError(
        "BAD_TIME_EXPR",
        f"无法解析时间表达式 {text!r}；支持 today/yesterday/YYYY-MM-DD/-Nd/-Nw/-Nm/-Ny",
        path="time_range",
    )


def shift_period(start: date, end: date, kind: str, offset: int) -> tuple[date, date]:
    """把主期整体平移，得到对比期。

    `offset` 是平移次数：`{"type":"yoy","offset":2}` = 前年同期。
    """
    if kind == "yoy":
        return shift_months(start, -12 * offset), shift_months(end, -12 * offset)
    if kind == "mom":
        return shift_months(start, -offset), shift_months(end, -offset)
    if kind == "wow":
        return start - timedelta(days=7 * offset), end - timedelta(days=7 * offset)
    raise CompileError(
        "BAD_MQL", f"compare.type 只允许 yoy/mom/wow，实际是 {kind!r}", path="compare.type"
    )


def render_relative(expr: str) -> tuple[str, int]:
    """给方言层用：把 `365d` 这类后缀拆成 (单位, 数值)。"""
    m = re.match(r"^(\d+)([dwmy])$", expr.strip())
    if not m:
        raise CompileError(
            "BAD_ONTOLOGY", f"日期偏移 {expr!r} 必须是 <数字><d|w|m|y>", path="ontology"
        )
    unit = m.group(2)
    n = int(m.group(1))
    if unit in ("m", "y"):
        raise CompileError(
            "BAD_ONTOLOGY",
            f"日期偏移 {expr!r} 用了月/年单位；月年在各月末不等长，"
            f"条件里只允许用 d/w，避免同一条口径在不同月份算出不同的边界",
            path="ontology",
        )
    return unit, n
