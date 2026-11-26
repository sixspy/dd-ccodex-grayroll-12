"""grayroll-12 红测争（flag_rename_race）：两路写者抢「开关键更名互斥」。

并发双请求脚本/函数名固定为 duel_flag_rename_migrate：两路会话（admin/writer）
Barrier 齐发同一开关的更名迁移，只许一成一败 [200,409]；败方只写
contention_marks 争用痕，不得写任何正式账（迁移票/键/外键保持唯一胜者口径）。

另含一个确定性持锁测试，排除调度侥幸，证明互斥不是“无锁双成”。

可独立运行（仓库根）：
    PYTHONPATH=backend .venv/bin/python tests/flag_rename_race.py
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from app import app
from db import DB_PATH, init_db

KEY_OLD = "ads.bid_curve"
KEY_NEW = "ads.bid_curve_v2"
ENV_PROD = 3

FLAG_ID_TABLES = (
    "flag_rules",
    "rule_shadows",
    "releases",
    "release_shadows",
    "kill_events",
    "kill_shadows",
    "overrides",
    "rollout_schedules",
    "change_history",
)


def _login(client, username, password):
    t = client.post(
        "/api/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def _seed_duel_fixture(client):
    """夹具打头：已有正式规则时执行开关键更名（争用同一票）。"""
    h = _login(client, "admin", "admin123")
    fid = client.post(
        "/api/flags",
        headers=h,
        json={"key": KEY_OLD, "title": "竞价曲线", "owner": "admin"},
    ).json()["id"]
    pub = client.post(
        "/api/releases/publish",
        headers=h,
        json={
            "flag_id": fid,
            "env_id": ENV_PROD,
            "kind": "percent",
            "percent": 40,
            "summary": "争用夹具正式规则",
            "use_shadow": False,
        },
    )
    assert pub.status_code == 200, pub.text
    return fid, h


def _marks_count():
    db = sqlite3.connect(DB_PATH)
    try:
        return db.execute("SELECT COUNT(*) FROM contention_marks").fetchone()[0]
    finally:
        db.close()


def _latest_mark():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        row = db.execute(
            "SELECT * FROM contention_marks ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def duel_flag_rename_migrate() -> dict:
    """并发双请求：两路会话齐发 flag rename-migrate。

    无参、自重置种子库，便于评分方直接调用/脚本运行。返回结构化验收报告。
    """
    Path(DB_PATH).unlink(missing_ok=True)
    init_db()
    report: dict = {"failures": []}

    with TestClient(app) as client:
        fid, h_admin = _seed_duel_fixture(client)
        h_writer = _login(client, "writer", "writer123")
        lock_key = f"flag-rename:{fid}"
        report["flag_id"] = fid
        report["lock_key"] = lock_key

        marks0 = _marks_count()
        db = sqlite3.connect(DB_PATH)
        try:
            orders0 = db.execute("SELECT COUNT(*) FROM flag_rename_orders").fetchone()[0]
        finally:
            db.close()

        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, object]] = []

        def worker(headers, who):
            barrier.wait()
            try:
                r = client.post(
                    "/api/flags/rename-migrate",
                    headers=headers,
                    json={"flag_id": fid, "new_key": KEY_NEW},
                )
                outcomes.append((who, r.status_code))
            except Exception as exc:  # 并发异常不得被吞成双成
                outcomes.append((who, f"EXC:{type(exc).__name__}:{exc}"))

        t1 = threading.Thread(target=worker, args=(h_admin, "admin"))
        t2 = threading.Thread(target=worker, args=(h_writer, "writer"))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        statuses = sorted(s for _, s in outcomes if isinstance(s, int))
        report["outcomes"] = outcomes
        report["statuses"] = statuses
        report["marks_delta"] = _marks_count() - marks0
        report["latest_mark"] = _latest_mark()

        db = sqlite3.connect(DB_PATH)
        try:
            key_now = db.execute("SELECT key FROM flags WHERE id=?", (fid,)).fetchone()[0]
            orders_all = db.execute(
                "SELECT status, path FROM flag_rename_orders"
            ).fetchall()
            rule_n = db.execute(
                "SELECT COUNT(*) FROM flag_rules WHERE flag_id=? AND official=1", (fid,)
            ).fetchone()[0]
            orphan = []
            for t in FLAG_ID_TABLES:
                n = db.execute(
                    f"SELECT COUNT(*) FROM {t} x LEFT JOIN flags f ON f.id=x.flag_id "
                    "WHERE x.flag_id IS NOT NULL AND f.id IS NULL"
                ).fetchone()[0]
                if n:
                    orphan.append((t, n))
        finally:
            db.close()

        report["key_now"] = key_now
        report["orders"] = [tuple(x) for x in orders_all]
        report["formal_orders_delta"] = sum(
            1 for st, path in orders_all if st in ("committed", "promoted")
        ) - orders0
        report["official_rule_rows"] = rule_n
        report["orphans"] = orphan

        # 败方不得写正式：状态必须一成一败
        if statuses != [200, 409]:
            report["failures"].append(
                f"两路更名未互斥：期望 [200,409]，实得 {statuses}（疑似无锁双成）"
            )
        if report["marks_delta"] != 1:
            report["failures"].append(
                f"争用痕应恰增 1，实增 {report['marks_delta']}"
            )
        mark = report["latest_mark"]
        if not mark or mark.get("lock_key") != lock_key:
            report["failures"].append(f"争用痕锁键应为 {lock_key}，实得 {mark}")
        if report["formal_orders_delta"] != 1:
            report["failures"].append(
                f"正式迁移票应恰增 1，实增 {report['formal_orders_delta']}"
            )
        if rule_n != 1:
            report["failures"].append(f"正式规则应保持单行，实得 {rule_n}")
        if orphan:
            report["failures"].append(f"外键存在孤儿：{orphan}")
        if key_now != KEY_NEW:
            report["failures"].append(f"胜者口径应为新键，实际键={key_now}")

    report["ok"] = not report["failures"]
    return report


def test_race_duel_flag_rename_migrate_one_winner_one_loser():
    report = duel_flag_rename_migrate()
    assert report["statuses"] == [200, 409], report
    assert report["marks_delta"] == 1, report
    assert report["latest_mark"]["lock_key"] == report["lock_key"]
    assert report["formal_orders_delta"] == 1, report
    assert report["official_rule_rows"] == 1
    assert report["orphans"] == []
    assert report["key_now"] == KEY_NEW
    assert report["failures"] == []


def test_race_jia_and_yi_paths_share_rename_mutex():
    """两路一甲一乙抢同一把更名锁：仍只许一成一败，败方零正式写入。"""
    with TestClient(app) as client:
        fid, h = _seed_duel_fixture(client)
        marks0 = _marks_count()
        barrier = threading.Barrier(2)
        outcomes = []

        def worker(use_shadow, who):
            barrier.wait()
            r = client.post(
                "/api/flags/rename-migrate",
                headers=h,
                json={"flag_id": fid, "new_key": KEY_NEW, "use_shadow": use_shadow},
            )
            outcomes.append((who, r.status_code))

        t1 = threading.Thread(target=worker, args=(False, "jia"))
        t2 = threading.Thread(target=worker, args=(True, "yi"))
        t1.start(); t2.start(); t1.join(30); t2.join(30)

        statuses = sorted(code for _, code in outcomes)
        assert statuses == [200, 409], outcomes
        assert _marks_count() - marks0 == 1

        db = sqlite3.connect(DB_PATH)
        try:
            key_now = db.execute("SELECT key FROM flags WHERE id=?", (fid,)).fetchone()[0]
            open_yi = db.execute(
                "SELECT COUNT(*) FROM flag_rename_orders WHERE status='open'"
            ).fetchone()[0]
            committed = db.execute(
                "SELECT COUNT(*) FROM flag_rename_orders WHERE status IN ('committed','promoted')"
            ).fetchone()[0]
        finally:
            db.close()

        # 只能是两种一致结局之一：甲胜→键已新、无 open 草案；
        # 乙胜→正式键仍旧、恰一张 open 草案、无 committed 票。
        if key_now == KEY_NEW:
            assert committed == 1 and open_yi == 0
        else:
            assert key_now == KEY_OLD
            assert open_yi == 1 and committed == 0


def test_race_mutex_is_deterministic_while_winner_holds_lock(monkeypatch):
    """不依赖调度的持锁测试：胜者已提交但卡在 release（仍持互斥门闩/行锁）时，
    另一路更名同一 flag（不同目标键，绕过“新旧键相同”预检）必 409 且只记争用痕。"""
    import flags_svc

    real_release = flags_svc.release
    reached_release = threading.Event()
    allow_release = threading.Event()

    def blocking_release(conn, key, holder):
        if key == f"flag-rename:{fid}":
            reached_release.set()
            allow_release.wait(timeout=5)  # 即使主线程断言失败，超时后也照常释放
        return real_release(conn, key, holder)

    monkeypatch.setattr(flags_svc, "release", blocking_release)

    with TestClient(app) as client:
        fid, h = _seed_duel_fixture(client)
        marks0 = _marks_count()
        winner_status = {}

        def winner():
            r = client.post(
                "/api/flags/rename-migrate",
                headers=h,
                json={"flag_id": fid, "new_key": KEY_NEW},
            )
            winner_status["code"] = r.status_code

        tw = threading.Thread(target=winner)
        tw.start()
        assert reached_release.wait(timeout=5), "胜者未进入持锁收尾段"

        loser = client.post(
            "/api/flags/rename-migrate",
            headers=h,
            json={"flag_id": fid, "new_key": "ads.bid_curve_v3_contender"},
        )
        assert loser.status_code == 409, loser.text
        assert _marks_count() - marks0 == 1

        allow_release.set()
        tw.join(timeout=10)
        assert winner_status.get("code") == 200


def main() -> int:
    report = duel_flag_rename_migrate()
    print(f"[duel] outcomes={report['outcomes']} statuses={report['statuses']}")
    print(
        f"[duel] marks_delta={report['marks_delta']} "
        f"formal_orders_delta={report['formal_orders_delta']} "
        f"rule_rows={report['official_rule_rows']} key={report['key_now']}"
    )
    if report["ok"]:
        print("GREEN: duel_flag_rename_migrate 一成一败，败方只记争用痕、零正式写。")
        return 0
    print("RED: " + "；".join(report["failures"]))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
