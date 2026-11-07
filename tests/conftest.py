from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mqlc import Ontology  # noqa: E402


@pytest.fixture(scope="session")
def onto() -> Ontology:
    return Ontology.load(ROOT / "fixtures" / "ontology.json")


@pytest.fixture(scope="session")
def db_path() -> Path:
    """每次测试会话重建样例库。

    不缓存到临时目录：测试要断言具体数值，库必须是确定的那一份。
    数据本身由 sha256 散列生成，同一份脚本在哪台机器上都一样。
    """
    spec = importlib.util.spec_from_file_location("build_db", ROOT / "fixtures" / "build_db.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    mod.build()
    return ROOT / "fixtures" / "fixture.db"


@pytest.fixture()
def db(db_path: Path):
    conn = sqlite3.connect(db_path)
    yield conn
    conn.close()
