# MQL Compiler — 规范 v1.0

> 把 MQL（Metrics Query Language）编译成目标方言 SQL 的参考实现规范。
>
> **这份文档描述问题空间，不是描述代码。** 代码是它的一个实现，可以被替换；文档里定下的规则才是契约。
>
> 配套：[`mql-spec`](https://github.com/Aiops-seal/mql-spec) 定义 MQL 这门语言本身。本文不重复定义 MQL，只定义**怎么把它变成一个数是正确的 SQL**。

---

## 0. 这份文档是什么

一份编译器规范。回答四个问题：

1. **输入是什么**（MQL + 本体 + 编译上下文）
2. **输出是什么**（SQL + plan，或者一个结构化错误）
3. **怎么从前者得到后者**（六阶段，每阶段可独立失败）
4. **它做不到什么**（能力上限，写在明面上）

不回答「这个数对不对」——口径正确性取决于本体写得对不对，编译器只保证**按本体诚实地算**。

---

## 1. 三条设计原则

### 1.1 纯函数

`compile(mql, ontology, ctx) -> Result`

同样的三个入参，永远得到同样的输出。没有 IO，没有会话状态，**不读系统时钟**——当前日期由 `ctx.now` 注入。

这条不是洁癖。上一篇文章讲「自然语言只能穿过边界一次」，编译器是穿过去之后的那一半。如果它自己读时钟、自己查缓存的元数据、自己保留「上次编译用的表」，确定性就漏了，重放也失效。

**重放的含义**：把三个月前的一份 MQL 和当时的 ctx 再喂一次，必须得到一模一样的 SQL。做不到，审计就无从谈起。

### 1.2 上限写死

能力上限是**单层聚合 + JOIN**。

不含窗口函数、不含子查询、不含 UNION、不含 CTE。

这不是「v1 先这样，v2 补上」。是**这个零件的形状就是这么大**。想要更复杂的东西，属于另一个零件。

声明上限的价值在于：调用方在集成时就知道自己会不会踩坑，而不是跑起来才发现某个指标静默地算错了。

### 1.3 plan 比 SQL 重要

成功输出里 `sql` 和 `plan` 并列，但 `plan` 才是稳定接口。

原因很直接：SQL 是一段文本，人能读但程序不好读；`plan` 是结构化的「这个数怎么来的」——用了哪张表、为什么选它、注入了哪些过滤、同店口径怎么实例化的、哪些方言特性没验证过。

**没有 plan，SQL 只是一段文本；有 plan，它才是可审计产物。**

`plan` 的结构一旦发布就是公开接口，改它要升版本号。`sql` 的排版可以随时变，只要语义不变。

---

## 2. 术语

| 词 | 含义 |
|---|---|
| **fact table** | 事实表。存可加总的度量与它们的粒度 |
| **dimension table** | 维表。存实体的描述属性 |
| **grain** | 事实表的粒度。一行代表什么 |
| **binding** | 一个指标（或属性）在某张表上的一种实现方式。同一指标可以在多张表上有 binding |
| **relation** | 两张表之间的连接关系，带基数（`many_to_one` / `one_to_many`） |
| **fan-out** | 一对多 JOIN 导致的度量重复计算。数仓里 90% 的「数翻倍了」都出在这 |
| **scoped filter** | 依赖查询期间的过滤条件（如「开业满一年」）。静态字符串表达不了，必须拿到 `time_range` 后实例化 |
| **verified** | 方言标记。只有真在目标数据库上跑通过的方言才允许标 `true` |

---

## 3. 输入

### 3.1 MQL

消费 MQL v1.1，结构完全由 `mql-spec` 定义。本文不重复。

**一个关键决定：编译器要不要校验 MQL？**

要，但只做**防崩溃检查**，不做完整校验。

完整校验（类型、枚举、物理渗入、未知字段的语义）是 `mql-spec` 的职责。如果编译器重做一遍，就是两份会漂移的实现。但如果完全不查，编译器就可能被喂一个 `metrics: null` 然后抛 `TypeError`。

所以编译器做的是**最小结构断言**：

- 顶层是对象
- `metrics` 存在且是非空数组
- 每个 metric 是对象且有 `name`（字符串）
- `dimensions` / `filters` / `sort` 若存在，是数组
- `time_range` 若存在，是带 `start` / `end` 的对象
- 不认识的顶层字段 → 直接拒绝

失败报 `BAD_MQL`，附 `path`。

**代价要说清楚**：这 6 条和 `mql-spec` 的 `MQL_SHAPE` / `UNKNOWN_FIELD` 有重叠，重叠量约 40 行。这 40 行是故意留的重复——一个可以被单独使用的零件，不能假定调用方一定先调了另一个零件。

### 3.2 本体（ontology）

`mql-spec` 只需要知道「这个指标存不存在」（7 个布尔查询方法）。编译器需要知道**结构**：指标在哪张表、哪一列、怎么连、粒度是什么。

**这是两份不同的本体接口，共用同一份本体文件。**

文件格式是 JSON（零依赖，不需要 YAML 解析器）：

```json
{
  "version": "1.0",
  "domain": "ord",
  "tables": [ ... ],
  "relations": [ ... ],
  "metrics": [ ... ],
  "properties": [ ... ]
}
```

#### 3.2.1 `tables[]`

```json
{
  "name": "dwd_order_di",
  "kind": "fact",
  "layer": "detail",
  "grain": ["order_id"],
  "columns": {
    "order_id": "string",
    "store_id": "string",
    "order_date": "date",
    "pay_amount": "decimal",
    "is_valid": "int8"
  }
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 物理表名。**这是本体里唯一允许出现物理名的地方** |
| `kind` | 是 | `fact` 或 `dimension` |
| `layer` | `fact` 必填 | `detail` 或 `agg`。决定 §6.2 的优先级，是**本体作者的声明**，不由编译器猜 |
| `grain` | `fact` 必填 | 粒度列。行数等于它的基数 |
| `columns` | 是 | 列名 → 类型 |

`layer` 为什么必须由人声明：编译器没有统计信息，也没有表之间的血缘，它无法从 `grain: [order_id]` 和 `grain: [date, city]` 判断哪张是预聚合——按列数比会得出「`order_id` 一列更粗」的荒谬结论。**判不出来就让人写下来，而不是猜一个。**

#### 3.2.2 `relations[]`

```json
{"from": "dwd_order_di", "to": "dim_store", "on": "store_id", "cardinality": "many_to_one"}
{"from": "dim_store", "to": "dim_city", "on": ["city_name", "city"], "cardinality": "many_to_one"}
```

| 字段 | 说明 |
|---|---|
| `from` / `to` | 两端表名 |
| `on` | 同名列写字符串；异名写 `[左列, 右列]` |
| `cardinality` | `many_to_one` / `one_to_many` / `one_to_one` |

**关系是有方向的，而且方向就是语义。** `dwd_order_di → dim_store` 是 `many_to_one`（多张订单对一个门店），这是安全的。反过来 `dim_store → dwd_order_di` 是 `one_to_many`，JOIN 上去 SUM 会翻倍。

`one_to_many` 不是「不许 JOIN」，是「JOIN 了就要报警或报错」——见 §7.3。

#### 3.2.3 `metrics[]`

```json
{
  "name": "gmv",
  "display": "成交额",
  "bindings": [
    {"table": "dwd_order_di", "expr": "SUM({f}.pay_amount)", "requires": ["{f}.is_valid = 1"]},
    {"table": "ads_gmv_1d", "expr": "SUM({f}.gmv)", "requires": []}
  ]
}
```

**指标不绑定唯一表，而是带一组 binding。** 这是整个设计的关键。

真实的数仓里，同一个 GMV 至少在两个地方存在：明细层用 `SUM(pay_amount)` 现算，预聚合层直接 `SUM(gmv)`。选哪个不是口径问题，是**执行路径**问题，取决于这次查询还要哪些维度。

一旦把指标硬绑到一张表，「事实表自动选择」就不存在了——只能人工一个个映射。

**`expr` 里的列名必须带 `{f}.` 前缀。** 这一条是被迫的：`dwd_order_di` 和 `dim_store` 都有 `store_id` 列，JOIN 之后不加前缀，SQL 会直接报 ambiguous column。让本体自己写前缀，比让编译器去猜「哪些标识符是列名、哪些是 SQL 关键字」靠谱得多——后者要么用一张关键字表维持，要么在某个 `CASE WHEN` 上翻车。

`requires` 里的条件按 §3.2.6 的占位符规则写。

#### 3.2.4 `properties[]`

```json
{
  "name": "city",
  "display": "城市",
  "kind": "enum",
  "bindings": [
    {"table": "dim_store", "column": "city"},
    {"table": "ads_gmv_1d", "column": "city"}
  ]
}
```

维度、过滤字段、时间属性统一叫 property，靠 `kind` 区分：

| `kind` | 用途 | 额外字段 |
|---|---|---|
| `enum` / `string` / `number` | 维度 + 过滤 | — |
| `time` | 时间维度、`time_range` 的承载列 | — |

`bindings` 的写法让「免 JOIN」成为可能：`city` 在 `ads_gmv_1d` 上直接有列，也在 `dim_store` 上有列。查 `ads_gmv_1d` 时不需要 JOIN 维表。

**这是「选对表」能省一次 JOIN 的机制**，也是它值得被自动化的原因之一。

#### 3.2.5 `scoped_filters[]`（挂在 metric 上）

```json
{
  "name": "same_store_gmv",
  "display": "同店成交额",
  "bindings": [{"table": "dwd_order_di", "expr": "SUM(pay_amount)", "requires": ["{f}.is_valid = 1"]}],
  "scoped_filters": [
    {
      "table": "dim_store",
      "on": "store_id",
      "condition": "dim_store.open_date <= {period_start} - 365d"
    }
  ]
}
```

`required_filters` 是静态的，写死在 SQL 里；`scoped_filters` 依赖查询期间，必须拿到 `time_range` 才能算出具体值。

不加这个东西，零售一半的经营类指标（同店、开业满 N 个月、有效经营日占比）在 MQL 里无处安放，会被逼回 `formula` 里硬凑，确定性设计当场失效。

#### 3.2.6 占位符与表名白名单

条件字符串里允许四种占位符：

| 占位符 | 含义 | 何时替换 |
|---|---|---|
| `{f}` | 事实表的 SQL 别名 | 生成 SQL 时 |
| `{period_start}` | 查询主期的起始日 | 时间求解之后 |
| `{period_end}` | 查询主期的结束日 | 时间求解之后 |
| `{now}` | `ctx.now` | 生成 SQL 时 |

日期运算（`- 365d`）由方言层翻译：SQLite 是 `date(x, '-365 day')`，MySQL 是 `DATE_SUB(x, INTERVAL 365 DAY)`。

**表名白名单。** 条件字符串里允许直接写表名，但只在两个位置：

1. `bindings[].requires` — 只允许写该 binding 自己的 `table`，或直接用 `{f}`
2. `scoped_filters[].condition` — 只允许写 `{f}` 和**本条 scoped filter 声明的 `table`**

越界即 `BAD_ONTOLOGY`。这条检查存在的意义：条件里一旦能自由写表名，本体就绕过了 binding 机制直接指定物理结构，「指标不绑表」这个设计当场失效——而失效是静默的，只是某个指标开始算错。

允许 `scoped_filters` 写自己那张表，是因为它本来就要 JOIN 那张表来取 `open_date`，写成 `{j.dim_store}.open_date` 只是把同一件事说得更绕。

### 3.3 编译上下文

```python
CompileContext = {
    "dialect": "sqlite",
    "permissions": [
        {"field": "region", "operator": "in", "values": ["R01", "R02"]}
    ],
    "now": "2026-11-07",
    "default_time": "t-1",
    "max_scan_days": 3660
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `dialect` | 是 | `sqlite` / `mysql` / `doris` / `hive` / `sparksql` |
| `permissions` | 否 | **已决定好的权限**。本模块不鉴权，只机械转成 WHERE |
| `now` | 是 | 注入的「今天」。不读系统时钟 |
| `default_time` | 否 | MQL 没给 `time_range` 时的兜底。缺省 `t-1` |
| `max_scan_days` | 否 | 单次编译允许的最大时间跨度，防手滑全表扫。缺省 3660 天 |

**权限是入参，不是判断。**

`permissions` 的字段名指向一个 property，走和 MQL `filters` 完全相同的渲染路径。这意味着权限条件也会被记进 `plan`，而且和用户自己写的过滤在结构上分得开——审计时要能回答「这一行结果为什么你能看到」，答案不能藏在 SQL 文本里。

---

## 4. 输出

### 4.1 成功

```json
{
  "ok": true,
  "sql": "SELECT ...",
  "plan": { ... },
  "warnings": [ ... ]
}
```

### 4.2 失败

```json
{
  "ok": false,
  "errors": [
    {"code": "FANOUT_RISK", "path": "dimensions[0]", "message": "...", "detail": { ... }}
  ]
}
```

**错误对象形状与 `mql-spec` 完全一致**：`code` / `path` / `message` / `candidates`（可选）/ `detail`（可选）。

不返回 `list[str]`。调用方要按 `code` 决定是「换个问法」还是「直接拒绝」，字符串匹配不可靠。

### 4.3 `plan` 的字段

```json
{
  "dialect": "sqlite",
  "dialect_verified": true,

  "fact_table": "dwd_order_di",
  "fact_table_reason": "fine_grained",
  "tables_used": ["dwd_order_di", "dim_store"],

  "joins": [
    {"from": "dwd_order_di", "to": "dim_store", "on": ["store_id", "store_id"],
     "cardinality": "many_to_one", "purpose": "dimension:city"}
  ],

  "metrics": [{"name": "gmv", "binding": "dwd_order_di", "expr": "SUM(pay_amount)"}],
  "properties": [{"name": "city", "binding": "dim_store", "column": "city"}],

  "injected_filters": [
    {"source": "mql", "sql": "dim_store.city = '杭州'"},
    {"source": "permission", "sql": "dim_store.region IN ('R01','R02')"},
    {"source": "required", "sql": "dwd_order_di.is_valid = 1"}
  ],
  "scoped_filters_instantiated": [
    {"metric": "same_store_gmv", "sql": "dim_store.open_date <= date('2026-10-01','-365 day')"}
  ],

  "time": {
    "dimension": "order_date",
    "granularity": null,
    "primary":   {"start": "2026-10-01", "end": "2026-10-31"},
    "compare":   {"type": "yoy", "start": "2025-10-01", "end": "2025-10-31"},
    "scan_range": {"start": "2025-10-01", "end": "2026-10-31"}
  },

  "warnings": ["预聚合表 ads_gmv_1d 被排除：缺少维度 store"]
}
```

几个字段值得单说：

- `fact_table_reason` — 为什么是这张表。枚举值见 §6.2。
- `injected_filters[].source` — `mql` / `permission` / `required` / `scoped`。**审计的抓手就是这个字段。**
- `time.scan_range` — 实际 WHERE 里覆盖的范围。带 `compare` 时它比 `primary` 宽，读者一看就知道多扫了哪一段。

---

## 5. 求解流程

六个阶段，顺序执行。每个阶段失败就返回错误，不进入下一阶段。

```
A 接收      MQL 防崩溃检查 + 上下文规范化
B 定位      每个指标 → 候选表集合（取交集）
C 选表      在候选里找一张能覆盖所有维度的
D 连表      维度/过滤落到表上，求解 JOIN 路径
E 定时       相对时间 → 绝对日期，展开 compare
F 出码      生成 SQL + plan
```

### 5.1 阶段 A：接收

- MQL 最小结构断言（§3.1）
- `ctx.now` 解析成 `date`，失败即 `BAD_CONTEXT`
- `dialect` 在枚举内，否则 `UNKNOWN_DIALECT`
- `permissions[].field` 在本体里有 property，否则 `BAD_CONTEXT`

**不在这里校验 MQL 语义。** 那不是它的活。

### 5.2 阶段 B：定位

对每个 `metrics[i].name`：

1. 在本体 `metrics` 里找同名项，没有 → `UNKNOWN_METRIC`（附 `candidates`，用编辑距离 Top3）
2. 取它的 `bindings`，得到这张指标能在哪些表上算
3. 与前面指标的候选集**取交集**

交集为空 → `NO_CANDIDATE_TABLE`，`detail` 里给出每个指标的候选表，让人一眼看出是哪两个指标永远碰不到一起。

**这里有一条硬规则：v1 只允许单事实表。**

两张事实表 JOIN 起来算不同指标，是数仓里最容易出 90% 错数的地方（乘数效应、去重口径、粒度不对齐）。v1 明确不管：报错，并给出建议「请在 ads 层使用宽表」。

假装支持比不支持更危险。

### 5.3 阶段 C：选表

候选集里可能有多个。用 §6 的规则排序，选第一个。

### 5.4 阶段 D：连表

把 `dimensions` / `filters` / `sort` 里引用的 property，逐个落到已选事实表上：

1. property 有 binding 直接落在事实表上 → 免 JOIN，直接用
2. 否则在关系图里找从事实表到该 property 所属表的路径（BFS，`max_hops=2`）
3. 路径唯一 → 按路径 JOIN；路径不唯一 → `AMBIGUOUS_JOIN`
4. 找不到路径 → `UNCOVERED_DIMENSION`

JOIN 的每一跳都要过 §7.3 的扇出检查。

**同一个表被多个 property 需要时只 JOIN 一次。** `scoped_filters` 需要的表也走同一套合并逻辑——否则 SQL 里会出现两次 `JOIN dim_store`，结果直接翻倍。

### 5.5 阶段 E：定时

1. `time_range` 存在 → 解析 `start` / `end`（相对表达式基于 `ctx.now`）
2. 不存在 → 用 `ctx.default_time`
3. 跨度超过 `max_scan_days` → `TIME_RANGE_TOO_WIDE`
4. `compare` 存在 → 按 §8.3 展开出对比期
5. `scoped_filters` 用**主期**实例化（§9）

### 5.6 阶段 F：出码

1. SELECT 列表：维度列 + 度量表达式（带 `compare` 时度量变成两列）
2. FROM：事实表，别名固定为 `f`
3. JOIN：按阶段 D 的结果
4. WHERE：`injected_filters` 全量拼接
5. GROUP BY：全部维度列
6. ORDER BY / LIMIT
7. 回填 `plan`

**别名规则**：事实表固定别名 `f`，维表别名用 `d_<表名去前缀>`。生成的 SQL 只用于阅读和排查，不作为稳定接口。

---

## 6. 事实表选择

### 6.1 候选的三个条件

一张表能承载这次查询，当且仅当三条同时成立：

1. **有指标的 binding** — 阶段 B 已经保证
2. **能覆盖全部维度** — 每个维度要么直接有列在表上，要么能 JOIN 到（一跳或两跳，且不扇出）
3. **能承载时间过滤** — `time_range` 用到的 time property 有列在表上，或可 JOIN 到

第 2 条是核心。它让「预聚合表能不能用」变成一个可判定的问题，而不是拍的。

### 6.2 排序规则

多个候选时按优先级：

| 优先级 | 原因码 | 条件 |
|---|---|---|
| 1 | `no_join` | 所有维度都直接落在表上，一次 JOIN 都不用 |
| 2 | `pre_aggregated` | `layer = agg`（预聚合层） |
| 3 | `fine_grained` | `layer = detail`。兜底 |

贪心的理由：JOIN 越少，出错面越小；预聚合表行数少，扫得少。

**但这只是启发式，不是最优解。** 一次 JOIN 明细表可能比两次 JOIN 预聚合表更省，编译器不做代价估算——它没有统计信息。

这件事要承认：`fact_table_reason` 记录的是「按哪条规则选的」，不是「这样最快」。

### 6.3 被排除的表要留痕

没选上的候选，理由要进 `warnings`：

```
预聚合表 ads_gmv_1d 被排除：缺少维度 store
```

不写这句，用户看到 SQL 里扫的是明细表，会以为编译器不会用预聚合。写出来，他就知道是那张表真的撑不住这个维度。

---

## 7. JOIN 路径

### 7.1 图模型

关系图是无向的（`many_to_one` 反过来就是 `one_to_many`），但**遍历时带方向**——扇出检查依赖方向。

### 7.2 最短路径 + 唯一性

BFS 找最短路径，`max_hops=2`。

多条等长路径 → `AMBIGUOUS_JOIN`，不选其中一条。

理由：不同 JOIN 路径可能产生不同结果。雪花模型里 `dim_store → dim_city` 和 `dim_store → dim_area → dim_city` 两条路上，如果维表数据不同步，结果就不一样。编译器没有能力判断哪条是对的，所以把决定权交回给写本体的人——**这是本体该修的问题，不是编译器该猜的问题**。

### 7.3 扇出检查

每一跳都检查 `cardinality`：

| 基数 | 方向 | 处理 |
|---|---|---|
| `many_to_one` | 从事实表出发 | 安全 |
| `one_to_one` | 任意 | 安全 |
| `one_to_many` | 从事实表出发 | **`FANOUT_RISK`** |

`one_to_many` 直接报错而不是警告，因为它的后果是**静默的数翻倍**，而这类错误在业务侧看不出来——数字看着合理，只是大了几倍。

例外：如果 MQL 里这张表**只被用于过滤、不出现在维度里**，且指标是 `COUNT(DISTINCT ...)`，理论上可以放行。v1 不实现这个例外——**能力上限优先于聪明**。

---

## 8. 时间

### 8.1 支持的表达式

| 写法 | 含义 |
|---|---|
| `today` / `yesterday` | 相对 `ctx.now` |
| `-Nd` / `-Nw` / `-Nm` / `-Ny` | N 天/周/月/年前 |
| `2026-10-01` | 绝对日期 |
| `month_start` / `month_end` | 当月首/末日 |
| `last_month_start` / `last_month_end` | 上月首/末日 |

`-1m` 的语义是**日历月平移后 clamp 到月末**：`ctx.now = 2026-03-31`，`-1m` = `2026-02-28`。不做「30 天前」的近似——那会让月度报表在 2 月和 3 月对不上。

### 8.2 默认时间

MQL 没给 `time_range` 时用 `ctx.default_time`，缺省 `t-1`（昨天）。

**这是有意的不对称**：`mql-spec` 里 `time_range` 是可选字段，`mql-author` 里规定「不知道时间就不写」。两边合起来的意思是——模型不猜时间，编译器给一个保守的默认值，并且这个默认值出现在 `plan.time` 里，能被审计。

### 8.3 compare 的展开

对比期 = 主期按 `type` 平移：

| type | 平移量 |
|---|---|
| `yoy` | -1 年 |
| `mom` | -1 月（clamp 到月末） |
| `wow` | -7 天 |

`offset: n` 就是平移 n 次。`{"type": "yoy", "offset": 2}` = 前年同期。

**实现方式：条件聚合，不是 UNION。**

```sql
SUM(CASE WHEN f.order_date BETWEEN '2026-10-01' AND '2026-10-31'
         THEN f.pay_amount END) AS gmv,
SUM(CASE WHEN f.order_date BETWEEN '2025-10-01' AND '2025-10-31'
         THEN f.pay_amount END) AS gmv_yoy
```

WHERE 里扩到两段的并集（`scan_range`）。

这个选择的代价要说清楚：

- **好处**：不出 UNION、不出子查询、一段 SQL 出两列，方言兼容面最大，plan 也简单
- **代价**：扫描范围扩大到两期之和。两期是相邻的（去年 10 月到今年 10 月）时，中间那 11 个月的数据会被扫到又被 CASE 丢掉
- **为什么不优化**：做「两段分别扫再 JOIN」需要子查询或 UNION，直接撞上 §1.2 的能力上限

`scan_range` 出现在 `plan` 里，就是为了让人看见这个代价，而不是把它藏起来。

---

## 9. `scoped_filters` 的实例化

### 9.1 变量代入

`condition` 里的 `{period_start}` / `{period_end}` 用**主期**代入，不用对比期。

**这是本规范里最容易做错、后果最重的一条。**

「同店」的定义是：主期和对比期都能正常经营的门店集合。如果拿对比期去实例化，`open_date <= 2025-10-01 - 365d` 会把主期新开的店算进来，同店口径变成「老店 + 老店里的新店」，同比就虚高了。

标准做法是**用主期定集合，两期共用**。所以：

- 主期 `2026-10-01 ~ 2026-10-31` → `dim_store.open_date <= date('2026-10-01','-365 day')`
- 对比期沿用同一个条件，不重新实例化

`plan.scoped_filters_instantiated` 里记下实例化后的字面条件，审计时能一眼看出用的是哪一期。

### 9.2 需要的 JOIN 要合并

`scoped_filters` 常常需要 JOIN 一张维表（`dim_store`）。如果这张表因为维度已经被 JOIN 了，**必须复用，不能再 JOIN 一次**。

忘了合并的表现：SQL 里出现 `JOIN dim_store ... JOIN dim_store ...`，SQLite 会直接报错，MySQL 会算出翻倍的数。前者还算幸运。

### 9.3 顺序

静态条件（`requires`）→ 权限 → MQL filters → `scoped_filters`。

四类在 WHERE 里用 `AND` 连，顺序不影响结果，但 `plan.injected_filters` 里按 `source` 分开记。

### 9.4 这个机制治不了什么

样例库里有一家 2026 年 5 月关掉的门店 `S08`。`dim_store` 里**没有 `close_date` 这一列**——关店这件事只能从「订单在某天之后消失」推出来。

于是 `open_date <= {period_start} - 365d` 会把 `S08` 判成同店。实测：

| 口径 | 主期（2026-10） | 对比期（2025-10） | 同比 |
|---|---|---|---|
| 只查 `open_date` | 28,511 | 54,632 | **−47.8%** |
| 剔除已关店 | 28,511 | 25,051 | **+13.8%** |

一个说腰斩，一个说涨了一成。差的不是编译器，是本体里少了一列。

**结论：`scoped_filters` 的表达力上限，等于本体能提供的列。** 中间格式治得了「说错」，治不了「缺数据」。

v1 不补 `close_date`。补了就等于替客户定义了同店口径，而这个口径各家不同：有的要求开业满 12 个月，有的要求两期都有销售，有的还要排除装修停业天数。**编译器不该内置业务口径**——它一旦内置，就变成了「我们的同店和你们的同店不是一个同店」，而这正是这套东西想消灭的问题。

这个缺陷留给本体作者，不留给编译器。代价是：本体写得不够时，编译出来的数会错，而且错得看不出来。

---

## 10. 方言

### 10.1 需要抽象的三处

| 差异 | sqlite | mysql | hive / sparksql |
|---|---|---|---|
| 日期字面量 | `'2026-10-01'` | 同 | 同 |
| 日期加减 | `date(x,'-365 day')` | `DATE_SUB(x, INTERVAL 365 DAY)` | `date_sub(x, 365)` |
| 日期截断到月 | `strftime('%Y-%m-01', x)` | `DATE_FORMAT(x,'%Y-%m-01')` | `trunc(x,'MM')` |
| 标识符引号 | `"x"` | `` `x` `` | `` `x` `` |

只抽象这几处。**不做通用 SQL 方言适配层**——那是个无底洞，而且和「单层聚合 + JOIN」的上限不匹配。

### 10.2 `verified` 的语义

```
sqlite    → verified: true    （端到端测试真跑过）
mysql     → verified: false
doris     → verified: false
hive      → verified: false
sparksql  → verified: false
```

`false` 的意思是「生成的 SQL 看起来对，但我没有在真实数据库上跑过」。

**宁可承认没验证，也不能假装支持。** 这条直接继承上游的做法——它做对了，不该改。

一个 `verified: false` 的方言如果被生产使用，调用方必须自己先跑一遍。把这句话写进 README，而不是写在心里。

---

## 11. 错误码

十四个。与 `mql-spec` 的 `MQL_*` 不重叠（除了两个本来就属于编译器的）。

| code | 阶段 | 含义 |
|---|---|---|
| `BAD_MQL` | A | MQL 结构不成立 |
| `BAD_CONTEXT` | A | ctx 缺字段或取值非法 |
| `UNKNOWN_DIALECT` | A | 方言不在枚举内 |
| `BAD_ONTOLOGY` | A | 本体文件结构错误（含条件字符串里出现非法表名） |
| `UNKNOWN_METRIC` | B | 指标不在本体（附 candidates） |
| `UNKNOWN_PROPERTY` | D | 维度/过滤字段不在本体（附 candidates） |
| `NO_CANDIDATE_TABLE` | B | 无任何表能同时承载这组指标 |
| `UNCOVERED_DIMENSION` | D | 维度无法落到任何可达表 |
| `AMBIGUOUS_JOIN` | D | 到维表存在多条等长路径 |
| `FANOUT_RISK` | D | JOIN 会放大度量 |
| `BAD_TIME_EXPR` | E | 时间表达式无法解析 |
| `TIME_RANGE_TOO_WIDE` | E | 跨度超过 `max_scan_days` |
| `UNSOLVABLE` | E | 语义冲突（如 compare 平移后早于数据起始日） |
| `INTERNAL_INVARIANT` | F | 内部断言失败（正常不该出现，出现即 bug） |

`INTERNAL_INVARIANT` 的存在是自觉：编译器有 40 多处「这里必然成立」的假设，每一个都要能被触发成明确错误，而不是 `KeyError`。

---

## 12. 明确不支持

写在这里的，是**决定不做**的，不是「还没做」的：

1. **窗口函数** — 环比排名、移动平均、累计值
2. **子查询 / CTE / UNION** — 含「两段分别扫再合并」的优化
3. **多事实表** — 跨事实表算指标（§5.2）
4. **指标之间四则运算** — `gmv / order_count` 这类派生指标，要么在本体里定义成一个指标，要么不做
5. **占比 / 分位数 / 去重计数之外的高级聚合** — 中位数、P95
6. **同环比归因** — 「为什么掉了 8 个点」这种问题，本模块不回答
7. **代价估算与执行计划优化** — 不读统计信息，不重排 JOIN
8. **SQL 注入防护** — 输入是结构化 MQL，不是字符串拼接。但如果本体文件本身是恶意的，本模块不负责

---

## 13. 验收标准

### 13.1 端到端数值

每一类 SQL 特性都要在 **SQLite 上真跑**，断言数值，而不是断言 SQL 字符串。

要覆盖：

1. 单指标 + 单维度 + 时间过滤 → 数值对
2. 多指标 + 多维度 + GROUP BY → 数值对
3. `compare.yoy` → 两列都有值，且与手工算的一致
4. `scoped_filters` → 同店集合被正确收窄，且**换个 `period_start` 结果会变**
5. 两份 `permissions` → 两段不同 SQL，执行后行数不同

第 4 条是关键：`scoped_filters` 如果没生效，SQL 也能跑出数，只是数不对。只断言「能跑」等于没测。

### 13.2 选表的可证伪性

样例数仓里必须存在一组查询，**选错表会真的产生错数**：

- 预聚合表 `ads_gmv_1d` 的 grain 是 `[date, city]`
- 一个带 `store` 维度的查询，如果强行用它（JOIN `dim_store` 反查）会扇出
- 引擎必须报 `FANOUT_RISK`，而不是给出一个翻倍的数

如果选错表也能碰巧算对，这个 demo 就没有说服力。

### 13.3 plan 快照

每个用例的 `plan` 做快照比对。`sql` 不比对——排版会变，语义不该变。

### 13.4 错误码全覆盖

14 个错误码，每个至少一个触发用例。

---

## 14. 这个设计放弃了什么

| 放弃的东西 | 换来了什么 | 代价 |
|---|---|---|
| 多事实表 | 不碰数仓里最容易出错的 JOIN | 跨域指标只能靠 ads 宽表 |
| 窗口函数 | SQL 生成逻辑保持线性 | 「累计 GMV」「排名」做不了 |
| 代价估算 | 不需要统计信息，纯函数 | 选表是启发式，可能非最优 |
| 多跳路径自动择优 | 结果确定 | 本体没写好就报错，逼人修本体 |
| 通用方言适配 | 5 个方言，代码量可控 | 换一个数据库就要加适配 |
| 自己读时钟 | 可重放、可审计 | 调用方必须传 `now` |

最后一行值得展开：**`ctx.now` 是必填的，不是默认取 `datetime.now()`**。这个约束会让第一次用的人觉得麻烦，但省掉它，同一份 MQL 隔天编译就会得到不同的 SQL，「三个月前那个数是怎么算出来的」就永远说不清。

---

## 15. 与同类实现的关系

- **dbt** 编译 metric 到 SQL，走的是 Jinja 模板 + 宏。本项目的差异是**不做模板**，用本体声明驱动，plan 是结构化的中间产物。
- **Cube** 有 query planner，能力比这里厚得多（含预聚合匹配、滚动窗口）。这里刻意薄。
- **Holistics AQL** 的指标可以组合成指标（metric on metric）。本项目不做（§12.4）。

统一口径：**不是做得更好，是刻意做薄，为了让每个零件都能被单独验证。**

薄是设计选择，不是能力不足——但必须主动承认它薄。
