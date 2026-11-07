"""编译器的错误码与错误对象。

为什么不用异常消息字符串
------------------------
调用方要按错误类型决定下一步动作：`FANOUT_RISK` 该去改本体，
`TIME_RANGE_TOO_WIDE` 该去缩时间范围，`UNKNOWN_METRIC` 该去追问用户。
用字符串匹配区分这三件事，等于把「判断」塞回给调用方。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 冻结的错误码。新增要同时改 SPEC.md §11 与测试。
CODES = frozenset({
    "BAD_MQL",
    "BAD_CONTEXT",
    "UNKNOWN_DIALECT",
    "BAD_ONTOLOGY",
    "UNKNOWN_METRIC",
    "UNKNOWN_PROPERTY",
    "NO_CANDIDATE_TABLE",
    "UNCOVERED_DIMENSION",
    "AMBIGUOUS_JOIN",
    "FANOUT_RISK",
    "BAD_TIME_EXPR",
    "TIME_RANGE_TOO_WIDE",
    "UNSOLVABLE",
    "INTERNAL_INVARIANT",
})


@dataclass
class CompileError(Exception):
    """一个编译错误。

    `path` 用 JSONPath 风格指向出错位置（`metrics[1].name`），
    和 mql-spec 的错误对象保持同一形状——同一条链路上游下游，
    错误对象长得不一样会逼调用方写两套解析。
    """

    code: str
    message: str
    path: str = "$"
    candidates: tuple[str, ...] = ()
    detail: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in CODES:
            raise ValueError(f"未登记的错误码: {self.code}")
        super().__init__(self.message)

    def to_dict(self) -> dict:
        out: dict = {"code": self.code, "path": self.path, "message": self.message}
        if self.candidates:
            out["candidates"] = list(self.candidates)
        if self.detail:
            out["detail"] = self.detail
        return out
