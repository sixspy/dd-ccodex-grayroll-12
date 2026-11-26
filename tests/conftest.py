"""测试隔离：每个用例前重置为种子库，杜绝共享 data/app.db 串测。

登录与库照旧：仍是同一套 SQLite + init_db 种子，仅每例重建库文件。
"""
from pathlib import Path

import pytest

from db import DB_PATH, init_db


@pytest.fixture(autouse=True)
def _reset_db():
    Path(DB_PATH).unlink(missing_ok=True)
    init_db()
    yield
