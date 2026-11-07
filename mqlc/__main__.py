"""命令行入口。

    python -m mqlc compile q.json --now 2026-11-07
    python -m mqlc run     q.json --now 2026-11-07 --db fixtures/fixture.db

`run` 是给验证用的：编译完立刻在 SQLite 上执行，把结果也打出来。
文章里的每个数字都该能用这条命令复现。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import Ontology, compile_mql, dialects


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mqlc", description="MQL -> SQL 编译器")
    p.add_argument("command", choices=["compile", "run", "dialects"])
    p.add_argument("mql", nargs="?", help="MQL 的 JSON 文件")
    p.add_argument("--ontology", default="fixtures/ontology.json", help="本体 JSON")
    p.add_argument("--now", required=False, help="注入的今天，YYYY-MM-DD")
    p.add_argument("--dialect", default="sqlite")
    p.add_argument(
        "--permission",
        action="append",
        default=[],
        help='权限，形如 region=R01,R02（可重复）',
    )
    p.add_argument("--max-scan-days", type=int, default=3660)
    p.add_argument("--db", default="fixtures/fixture.db", help="run 用的 SQLite 库")
    p.add_argument("--json", action="store_true", help="只输出 JSON")
    args = p.parse_args(argv)

    if args.command == "dialects":
        for name in dialects():
            from . import dialect as d

            info = d.get(name)
            print(f"{name:<10} verified={info.verified}")
        return 0

    if not args.mql:
        p.error("compile/run 需要指定 MQL 文件")
    if not args.now:
        p.error("--now 是必填的：编译器不读系统时钟")

    permissions = []
    for spec in args.permission:
        field, _, rest = spec.partition("=")
        permissions.append(
            {"field": field, "operator": "in", "values": [x for x in rest.split(",") if x]}
        )

    ctx = {
        "dialect": args.dialect,
        "now": args.now,
        "permissions": permissions,
        "max_scan_days": args.max_scan_days,
    }
    result = compile_mql(_load(args.mql), Ontology.load(args.ontology), ctx)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    if not result["ok"]:
        print("编译失败", file=sys.stderr)
        for e in result["errors"]:
            print(f"  [{e['code']}] {e['path']}: {e['message']}", file=sys.stderr)
        return 1

    print(result["sql"])
    print()
    print("--- plan ---")
    print(json.dumps(result["plan"], ensure_ascii=False, indent=2))
    if result["warnings"]:
        print("\n--- warnings ---")
        for w in result["warnings"]:
            print(f"  · {w}")

    if args.command == "run":
        conn = sqlite3.connect(args.db)
        cur = conn.execute(result["sql"])
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        print("\n--- rows ---")
        print("  " + " | ".join(cols))
        for r in rows[:20]:
            print("  " + " | ".join(str(x) for x in r))
        if len(rows) > 20:
            print(f"  ... 共 {len(rows)} 行")
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
