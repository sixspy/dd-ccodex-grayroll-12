#!/usr/bin/env python
"""grayroll-12 并发双请求入口：duel_flag_rename_migrate。

用法（仓库根）：
    PYTHONPATH=backend .venv/bin/python tests/duel_flag_rename_migrate.py

退出码 0 = 两路一成一败、败方只记争用痕且零正式写；非 0 = 仍可无锁双成。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))

from flag_rename_race import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
