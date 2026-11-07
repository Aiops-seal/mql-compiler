# mql-compiler

把 [MQL](https://github.com/Aiops-seal/mql-spec)（Metrics Query Language，指标查询语言）编译成 SQL。

纯函数：不读系统时钟、不连数据库、不保留状态。同样的三个入参，永远得到同样的 SQL 和同一份 plan。

零运行时依赖，Python 3.10+。

> **MQL 与 MongoDB Query Language 无关**，也跟营销里的 MQL（Marketing Qualified Lead）无关。这个词不是本项目发明的，国内 NL2SQL 圈已有既成用法（如 Aloudata 的 NL2MQL2SQL）。

---

## 30 秒跑起来

```bash
git clone https://github.com/Aiops-seal/mql-compiler
cd mql-compiler
python fixtures/build_db.py          # 造一个 SQLite 样例数仓
python -m mqlc run examples/q01-gmv-by-city.json --now 2026-11-07
```

输出（真实运行结果）：

```
SELECT
  f.city AS city,
  SUM(f.gmv) AS gmv
FROM ads_gmv_1d f
WHERE f.date BETWEEN '2026-10-01' AND '2026-10-31'
GROUP BY f.city

--- rows ---
  city | gmv
  HZ | 99324.0
  NB | 59840.0
  SZ | 59192.0
```

`--now` 是必填的，不是遗漏了默认值。省掉它，同一份 MQL 隔天编译就会得到不同的 SQL，三个月前那个数就永远说不清了。

---

## 它到底做了什么：同一份 MQL，两种权限

这是整个设计的落点。同一个问题、同一份 MQL，因为**谁在问**不同，编译出来的不只是 `WHERE` 不同，连事实表都不同。

```bash
# 总部：没有区域权限
python -m mqlc run examples/q05-gmv-permission.json --now 2026-11-07

# 区域经理：只能看 R01
python -m mqlc run examples/q05-gmv-permission.json --now 2026-11-07 --permission region=R01
```

两次的真实产出：

| | 总部 | 区域经理 |
|---|---|---|
| 事实表 | `ads_gmv_1d`（预聚合） | `dwd_order_di`（明细） |
| JOIN | 无 | `JOIN dim_store` |
| 度量表达式 | `SUM(f.gmv)` | `SUM(f.pay_amount)` |
| 过滤 | 只有时间 | 时间 + `region IN ('R01')` |
| 返回行数 | 3 | 2 |

```sql
-- 总部
SELECT f.city AS city, SUM(f.gmv) AS gmv
FROM ads_gmv_1d f
WHERE f.date BETWEEN '2026-10-01' AND '2026-10-31'
GROUP BY f.city

-- 区域经理
SELECT d_store.city AS city, SUM(f.pay_amount) AS gmv
FROM dwd_order_di f
  JOIN dim_store d_store ON f.store_id = d_store.store_id
WHERE f.order_date BETWEEN '2026-10-01' AND '2026-10-31'
  AND d_store.region IN ('R01')
GROUP BY d_store.city
```

**为什么换表了**：权限字段 `region` 只挂在 `dim_store` 上。预聚合表按 `[date, city]` 存，没有 `region`，要注入权限就得用 `city` 反查门店——那是 city → store 的一对多，会把每行重复计入。所以它用不了，只能退回明细层。

**为什么两个数还是一致的**：两层口径相同（样例库里预聚合表就是从明细表聚合出来的）。重叠的城市上数值完全一致。编译器的选择只影响执行路径，不影响语义——这是一条硬约束，有一条测试专门守它。

**权限不在模型手里。** 它是编译入参，模型看到的永远是同一份问题。

---

## 它会拒绝什么

### 拿错表算出的数，长得很像对的

样例库里 `ads_gmv_1d` 按 `[date, city]` 存。硬要用它回答「各门店 GMV」，只能靠 `city` 反查门店：

```
S01  ads误用=   99,324.00   正确=   34,466.00   放大 2.88x
S02  ads误用=   99,324.00   正确=   30,789.00   放大 3.23x
S04  ads误用=   59,840.00   正确=   33,904.00   放大 1.76x
```

放大倍数在 1.76x ~ 3.23x 之间。没有 10 倍、没有负数、没有 NULL，**数字看着就是业务增长的样子**。这类错在业务侧对不出来，会被当成好消息发出去。

所以编译器对它报错，不是警告：

```bash
python -m mqlc run examples/q02-gmv-by-store.json --now 2026-11-07
```

```
--- warnings ---
  · ads_gmv_1d 被排除：ads_gmv_1d → dim_store 是一对多，JOIN 后度量的每一行会被重复计入；
    这次的 dimensions[0] 需要它，结果会放大而不是报错
```

### 十四个错误码

每个都有触发用例（`tests/test_errors.py`）。

| code | 什么时候 |
|---|---|
| `BAD_MQL` | 顶层不是对象、`metrics` 为空、出现未知字段 |
| `BAD_CONTEXT` | 缺 `now`、权限字段不是本体里的属性 |
| `UNKNOWN_DIALECT` | 方言不在枚举内 |
| `BAD_ONTOLOGY` | 本体结构错、表达式缺 `{f}.` 前缀、条件引用了白名单外的表 |
| `UNKNOWN_METRIC` / `UNKNOWN_PROPERTY` | 指标或字段不在本体，附候选 |
| `NO_CANDIDATE_TABLE` | 没有任何一张表能同时承载这组指标 |
| `UNCOVERED_DIMENSION` | 维度落不到任何可达的表 |
| `AMBIGUOUS_JOIN` | 到维表有多条等长路径 |
| `FANOUT_RISK` | JOIN 会放大度量 |
| `BAD_TIME_EXPR` / `TIME_RANGE_TOO_WIDE` | 时间表达式解析不了 / 跨度超上限 |
| `UNSOLVABLE` | 语义冲突（起止倒置、`compare` 套不上非聚合表达式） |
| `INTERNAL_INVARIANT` | 内部断言失败 —— 出现即 bug |

错误对象和 `mql-spec` 同形状（`code` / `path` / `message` / `candidates`），调用方不用写两套解析。

---

## plan 比 SQL 重要

成功返回 `{"ok": true, "sql": ..., "plan": ..., "warnings": [...]}`。

SQL 是一段文本，人能读、程序不好读。`plan` 回答「这个数怎么来的」。下面是
`python -m mqlc compile examples/q04-same-store.json --now 2026-11-07 --permission region=R01`
的真实输出（节选）：

```json
{
  "fact_table": "dwd_order_di",
  "fact_table_reason": "fine_grained",
  "tables_used": ["dwd_order_di", "dim_store"],
  "joins": [{"from": "dwd_order_di", "to": "dim_store",
             "on": ["store_id", "store_id"],
             "cardinality": "many_to_one", "purpose": "dimension"}],
  "injected_filters": [
    {"source": "time", "sql": "f.order_date BETWEEN '2025-10-01' AND '2026-10-31'"},
    {"source": "required", "sql": "f.is_valid = 1", "metric": "same_store_gmv"},
    {"source": "permission", "sql": "d_store.region IN ('R01')", "field": "region"},
    {"source": "scoped", "sql": "d_store.open_date <= date('2026-10-01', '-365 day')",
     "metric": "same_store_gmv"}
  ],
  "time": {
    "primary":    {"start": "2026-10-01", "end": "2026-10-31"},
    "compare":    {"type": "yoy", "start": "2025-10-01", "end": "2025-10-31"},
    "scan_range": {"start": "2025-10-01", "end": "2026-10-31"}
  }
}
```

三个字段值得单说：

- `fact_table_reason` —— 为什么是这张表（`no_join` / `pre_aggregated` / `fine_grained`）
- `injected_filters[].source` —— 这条条件是 MQL 带来的、权限带来的、指标自带的，还是作用域过滤实例化的。审计的抓手就是它
- `time.scan_range` —— 实际 WHERE 覆盖的范围。带 `compare` 时它比 `primary` 宽，多扫了哪一段一眼能看见

`plan` 的结构是公开接口，改要升版本。`sql` 的排版随时可以变。

---

## 能力上限（写死在明面上）

**单层聚合 + JOIN。** 不含窗口函数、子查询、CTE、UNION。

明确不做，不是「还没做」：

1. 窗口函数（排名、移动平均、累计值）
2. 子查询 / CTE / UNION —— 包括「两期分别扫再合并」这种优化
3. **多事实表** —— 跨事实表算指标。数仓里最容易出 90% 错数的地方，明确不管
4. 指标之间的四则运算（`gmv / order_count` 这类派生指标要在本体里定义好）
5. 占比、分位数、中位数
6. 同环比归因
7. 代价估算与执行计划优化 —— 不读统计信息，选表是启发式，可能非最优
8. SQL 注入防护 —— 输入是结构化 MQL，不是字符串拼接

---

## 本体怎么写

一个 JSON 文件，四段：`tables` / `relations` / `metrics` / `properties`。完整定义见 [`SPEC.md` §3](SPEC.md)。

关键设计是**指标不绑定唯一表**：

```json
{
  "name": "gmv",
  "bindings": [
    {"table": "dwd_order_di", "expr": "SUM({f}.pay_amount)", "requires": ["{f}.is_valid = 1"]},
    {"table": "ads_gmv_1d",  "expr": "SUM({f}.gmv)",        "requires": []}
  ]
}
```

同一个指标在两层的两种实现。选哪个不是口径问题，是执行路径问题，取决于这次还要哪些维度。

`expr` 里的列名必须带 `{f}.` 前缀。`dwd_order_di` 和 `dim_store` 都有 `store_id`，JOIN 之后不加前缀 SQL 会直接报 ambiguous column。让本体自己写前缀，比让编译器去猜「哪些标识符是列名、哪些是关键字」靠谱得多。

`relations` 的 `cardinality` 不是注释，是安全依据。`many_to_one` 安全，`one_to_many` 会让 `FANOUT_RISK` 报错。

---

## 方言

```bash
python -m mqlc dialects
```

```
doris      verified=False
hive       verified=False
mysql      verified=False
sparksql   verified=False
sqlite     verified=True
```

`verified` 的意思是「真在目标数据库上跑过端到端测试」。只有 SQLite 是 `true`，其他四个的 SQL 是按其语法生成的，**没跑过**。

用未验证的方言会得到一条 warning，`plan.dialect_verified` 也会是 `false`。宁可承认没验证，也不假装支持。

---

## 测试

```bash
pip install pytest
python -m pytest
```

82 项。重点不在覆盖率，在两类：

- **端到端数值断言**（`tests/test_e2e.py`）：编译出的 SQL 真在 SQLite 上跑，断言具体数字，不是断言 SQL 字符串。字符串会在每次无害的重构里变红，然后被人 `--ignore` 掉
- **错误码全覆盖**（`tests/test_errors.py`）：14 个码，每个至少一个触发用例。目的是防止某个错误以后退化成 `KeyError` 或被静默吞掉

样例数仓由 `fixtures/build_db.py` 用 sha256 散列确定性生成，同一份脚本在任何机器上跑出同一批数。

其中有一处是刻意的缺陷：门店 `S08` 在 2026 年 5 月关店，但 `dim_store` 里**没有 `close_date` 这一列**。所以「开业满一年」的同店口径拦不住它：

| 口径 | 主期 | 对比期 | 同比 |
|---|---|---|---|
| 只查 `open_date` | 28,511 | 54,632 | **−47.8%** |
| 剔除已关店 | 28,511 | 25,051 | **+13.8%** |

一个说腰斩，一个说涨了一成。**中间格式治得了「说错」，治不了「缺数据」。** 这个缺口留给本体作者，不留给编译器——补上 `close_date` 就等于替客户定义了同店口径，而这个口径各家不同。

---

## 和 mql-spec 的关系

```
mql-spec     定义 MQL 这门语言（schema + 校验器），不出 SQL
mql-compiler 消费 MQL，产出 SQL + plan
```

**两者共享数据契约，不共享代码。** 任何一个方向出现 `import` 都属于设计事故。

`mql-spec` 的本体接口只有 7 个布尔方法（「这个指标存不存在」），这里的接口要问「它在哪张表、哪一列、怎么连」。两套接口读同一份本体文件——文件格式一变，两边同时炸，而不是一边悄悄错。

---

## 也看什么

- [`SPEC.md`](SPEC.md) —— 完整规范：六阶段求解、选表规则、扇出判定、时间语义、plan 字段
- [`test_e2e.py`](tests/test_e2e.py) —— 这个编译器到底算出什么数，看这个文件最快

## License

MIT
