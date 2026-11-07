"""造一个最小但自洽的样例数仓（SQLite）。

为什么要有这个文件
------------------
编译器需要能被证伪。「它选对了表」这句话只有在「选错表会出错」的前提下才有意义。
所以这份数仓刻意造了三件事：

1. 同一个 GMV 在两处存在：明细层 `dwd_order_di`（一行一单）和预聚合层
   `ads_gmv_1d`（一行一城一天）。两者口径一致，选哪张都得到同一个数。
2. `ads_gmv_1d` 的粒度是 [date, city]，撑不住 `store` 维度。强行用它会通过
   `dim_store` 的 city 反查门店，一个门店拿到整个城市的量——**放大 3 倍**。
3. 同店口径依赖开业日期：有两家店是 2026 年开的，任何「开业满一年」的
   口径都该把它们排除，而这个排除只能靠 `scoped_filters` 做到。

数据是确定性生成的（sha256 散列），同一份脚本在任何机器上跑出同一批数。

用法：
    python fixtures/build_db.py            # 产出 fixtures/fixture.db
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "fixture.db"

START = date(2025, 9, 1)
END = date(2026, 11, 7)

STORES = [
    # store_id, name, city, region, type, open_date
    ("S01", "杭州西湖店", "HZ", "R01", "直营", "2019-01-01"),
    ("S02", "杭州滨江店", "HZ", "R01", "加盟", "2020-06-01"),
    ("S03", "杭州临平店", "HZ", "R01", "直营", "2026-08-01"),  # 新店，同店口径应排除
    ("S04", "宁波海曙店", "NB", "R01", "直营", "2018-03-01"),
    ("S05", "宁波鄞州店", "NB", "R01", "加盟", "2021-11-01"),
    ("S06", "苏州工业园店", "SZ", "R02", "直营", "2022-05-01"),
    ("S07", "苏州吴中店", "SZ", "R02", "加盟", "2026-09-01"),  # 新店，同店口径应排除
    ("S08", "苏州姑苏店", "SZ", "R02", "直营", "2024-01-01"),  # 已关店，见下
]

#: 关店门店 -> 最后一天有单。**注意这里没有 close_date 这一列。**
#: 关店这件事只能从「订单在某个日期之后消失」推出来，数据层没有任何字段说它关了。
#: 这是刻意的：它让「同店口径只检查开业日期」这个缺陷可被验证——
#: 对比期有 S08、主期没有，同比就会虚低，而 scoped_filters 拦不住。
CLOSED = {"S08": date(2026, 5, 31)}

PRODUCTS = [
    ("P01", "手机", "数码"),
    ("P02", "耳机", "数码"),
    ("P03", "衬衫", "服饰"),
    ("P04", "外套", "服饰"),
    ("P05", "纸巾", "日用"),
    ("P06", "洗衣液", "日用"),
]

CHANNELS = ["app", "miniapp", "store"]

SCHEMA = """
DROP TABLE IF EXISTS dwd_order_di;
DROP TABLE IF EXISTS ads_gmv_1d;
DROP TABLE IF EXISTS dim_store;
DROP TABLE IF EXISTS dim_product;
DROP TABLE IF EXISTS dim_date;

CREATE TABLE dim_store (
    store_id   TEXT PRIMARY KEY,
    store_name TEXT NOT NULL,
    city       TEXT NOT NULL,
    region     TEXT NOT NULL,
    store_type TEXT NOT NULL,
    open_date  TEXT NOT NULL
);

CREATE TABLE dim_product (
    product_id   TEXT PRIMARY KEY,
    category     TEXT NOT NULL,
    product_type TEXT NOT NULL
);

CREATE TABLE dim_date (
    date       TEXT PRIMARY KEY,
    year       INTEGER NOT NULL,
    month      INTEGER NOT NULL,
    is_holiday INTEGER NOT NULL
);

CREATE TABLE dwd_order_di (
    order_id      TEXT PRIMARY KEY,
    store_id      TEXT NOT NULL,
    product_id    TEXT NOT NULL,
    order_date    TEXT NOT NULL,
    channel       TEXT NOT NULL,
    pay_amount    REAL NOT NULL,
    refund_amount REAL NOT NULL,
    is_valid      INTEGER NOT NULL
);

CREATE TABLE ads_gmv_1d (
    date        TEXT NOT NULL,
    city        TEXT NOT NULL,
    gmv         REAL NOT NULL,
    order_count INTEGER NOT NULL,
    PRIMARY KEY (date, city)
);
"""


def h(*parts: object) -> int:
    """确定性散列。同一组参数在任何机器上都返回同一个数。"""
    raw = "|".join(str(p) for p in parts)
    return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12], 16)


def build() -> None:
    if DB.exists():
        DB.unlink()
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.executescript(SCHEMA)

    cur.executemany("INSERT INTO dim_store VALUES (?,?,?,?,?,?)", STORES)
    cur.executemany("INSERT INTO dim_product VALUES (?,?,?)", PRODUCTS)

    days = []
    d = START
    while d <= END:
        iso = d.isoformat()
        # 周末与两个固定节日标记为节假日，够测试过滤用了
        holiday = 1 if d.weekday() >= 5 or iso in (
            "2025-10-01", "2026-01-01", "2026-05-01", "2026-10-01"
        ) else 0
        days.append((iso, d.year, d.month, holiday))
        d += timedelta(days=1)
    cur.executemany("INSERT INTO dim_date VALUES (?,?,?,?)", days)

    orders = []
    for store_id, _name, city, _region, _type, open_date in STORES:
        closed_on = CLOSED.get(store_id)
        d = START
        while d <= END:
            iso = d.isoformat()
            if iso < open_date or (closed_on and d > closed_on):
                d += timedelta(days=1)
                continue
            for i in range(1 + h(iso, store_id, "n") % 3):
                pid = PRODUCTS[h(iso, store_id, i, "p") % len(PRODUCTS)][0]
                amount = 39 + h(iso, store_id, i, "a") % 900
                invalid = h(iso, store_id, i, "v") % 23 == 0
                if invalid:
                    refund = amount
                elif h(iso, store_id, i, "r") % 13 == 0:
                    refund = amount // 2
                else:
                    refund = 0
                orders.append((
                    f"O{iso.replace('-', '')}{store_id}{i}",
                    store_id,
                    pid,
                    iso,
                    CHANNELS[h(iso, store_id, i, "c") % len(CHANNELS)],
                    float(amount),
                    float(refund),
                    0 if invalid else 1,
                ))
            d += timedelta(days=1)

    cur.executemany("INSERT INTO dwd_order_di VALUES (?,?,?,?,?,?,?,?)", orders)

    # 预聚合层从明细层聚合而来 —— 口径必须一致，否则「选哪张表」会变成口径问题，
    # 那就不再是编译器该负责的事了。
    cur.execute("""
        INSERT INTO ads_gmv_1d (date, city, gmv, order_count)
        SELECT o.order_date,
               s.city,
               SUM(o.pay_amount),
               COUNT(DISTINCT o.order_id)
        FROM dwd_order_di o
        JOIN dim_store s ON s.store_id = o.store_id
        WHERE o.is_valid = 1
        GROUP BY o.order_date, s.city
    """)

    conn.commit()

    n_orders = cur.execute("SELECT COUNT(*) FROM dwd_order_di").fetchone()[0]
    n_ads = cur.execute("SELECT COUNT(*) FROM ads_gmv_1d").fetchone()[0]
    conn.close()

    print(f"built {DB}")
    print(f"  dwd_order_di  {n_orders:>6} rows")
    print(f"  ads_gmv_1d    {n_ads:>6} rows")
    print(f"  dim_store     {len(STORES):>6} rows")
    print(f"  dim_product   {len(PRODUCTS):>6} rows")
    print(f"  dim_date      {len(days):>6} rows")


if __name__ == "__main__":
    build()
