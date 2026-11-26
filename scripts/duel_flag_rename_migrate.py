#!/usr/bin/env python
"""命令行薄封装：并发双请求 duel_flag_rename_migrate。

用法（仓库根）：
    PYTHONPATH=backend .venv/bin/python scripts/duel_flag_rename_migrate.py
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
