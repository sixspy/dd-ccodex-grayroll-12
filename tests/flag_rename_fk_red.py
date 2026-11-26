"""grayroll-12 红测甲（flag_rename_fk_red）：已有正式规则时执行开关键更名。

夹具打头：ads.bid_curve 名下已有 prod 正式规则（percent=100）与正式发布、覆盖。

盯的红：只改/新建键而不搬迁规则与发布外键时，更名后旧键仍命中正式规则、
新键下规则为空（题面事故）。甲路径正确性口径：
  更名（flags.key）与规则、发布等全部 flag_id 外键的搬迁/联查必须同一事务、
  同一张迁移票（flag_rename_orders），任何一步失败整笔回滚。

收束断言：新键可命中；旧键拒识；外键全部迁完（跨表账实无孤儿、行数不丢）。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import app
from db import DB_PATH

KEY_OLD = "ads.bid_curve"
KEY_NEW = "ads.bid_curve_v2"
ENV_PROD = 3
SUBJECT = "u-grayroll12-jia"

# 所有挂 flag_id 的表：grayroll-12 收束要求“外键全部迁完”，逐表联查账实。
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


def _h(client, username="admin", password="admin123"):
    t = client.post(
        "/api/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def seed_bid_curve(client, h, *, percent=100, with_override=True):
    """夹具：已有正式规则时执行开关键更名——先把正式账实造齐。"""
    f = client.post(
        "/api/flags",
        headers=h,
        json={"key": KEY_OLD, "title": "竞价曲线", "description": "广告出价曲线", "owner": "admin"},
    )
    assert f.status_code == 200, f.text
    fid = f.json()["id"]
    pub = client.post(
        "/api/releases/publish",
        headers=h,
        json={
            "flag_id": fid,
            "env_id": ENV_PROD,
            "kind": "percent",
            "percent": percent,
            "summary": "竞价曲线 prod 正式灰度规则",
            "use_shadow": False,
        },
    )
    assert pub.status_code == 200, pub.text
    if with_override:
        ov = client.post(
            "/api/overrides",
            headers=h,
            json={
                "flag_id": fid,
                "env_id": ENV_PROD,
                "subject_key": "u-override-marker",
                "effect": "force_on",
                "note": "外键随迁标记",
            },
        )
        assert ov.status_code == 200, ov.text
    return fid


def _table_counts(fid):
    db = sqlite3.connect(DB_PATH)
    try:
        out = {
            t: db.execute(
                f"SELECT COUNT(*) FROM {t} WHERE flag_id=?", (fid,)
            ).fetchone()[0]
            for t in FLAG_ID_TABLES
        }
    finally:
        db.close()
    return out


def _orphan_tables():
    db = sqlite3.connect(DB_PATH)
    bad = []
    try:
        for t in FLAG_ID_TABLES:
            n = db.execute(
                f"SELECT COUNT(*) FROM {t} x LEFT JOIN flags f ON f.id=x.flag_id "
                "WHERE x.flag_id IS NOT NULL AND f.id IS NULL"
            ).fetchone()[0]
            if n:
                bad.append((t, n))
    finally:
        db.close()
    return bad


def test_jia_rename_same_tx_moves_rule_and_release_fks():
    with TestClient(app) as client:
        h = _h(client)
        fid = seed_bid_curve(client, h)
        before = _table_counts(fid)
        assert before["flag_rules"] == 1
        assert before["releases"] >= 1
        assert before["overrides"] == 1

        r = client.post(
            "/api/flags/rename-migrate",
            headers=h,
            json={"flag_id": fid, "new_key": KEY_NEW},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("mode") == "official", body
        tid = body.get("ticket_id") or body.get("order_id") or body.get("id")
        assert tid, body
        migrated = body.get("migrated") or body.get("counts") or {}
        assert int(migrated.get("flag_rules", 0)) >= 1, body
        assert int(migrated.get("releases", 0)) >= 1, body

        # 收束 1：新键可命中（正式规则随外键一起迁到新键名下）
        ev = client.post(
            "/api/evaluate",
            headers=h,
            json={"flag_key": KEY_NEW, "env_code": "prod", "subject_key": SUBJECT},
        )
        assert ev.status_code == 200, ev.text
        assert ev.json()["enabled"] is True, ev.text
        assert ev.json()["reason"] == "rule"

        # 收束 2：旧键拒识（不得再命中旧正式规则）
        old = client.post(
            "/api/evaluate",
            headers=h,
            json={"flag_key": KEY_OLD, "env_code": "prod", "subject_key": SUBJECT},
        )
        assert old.status_code == 404, old.text

        # 跨表账实：键已换、身份稳定；各表行数一笔不丢
        db = sqlite3.connect(DB_PATH)
        try:
            assert db.execute(
                "SELECT COUNT(*) FROM flags WHERE key=?", (KEY_OLD,)
            ).fetchone()[0] == 0
            new_id, new_key = db.execute(
                "SELECT id, key FROM flags WHERE key=?", (KEY_NEW,)
            ).fetchone()
            assert new_key == KEY_NEW
            assert new_id == fid  # 代理主键稳定，外键同票联查而非另起孤岛
            assert db.execute(
                "SELECT official FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1",
                (fid, ENV_PROD),
            ).fetchone()[0] == 1
        finally:
            db.close()

        after = _table_counts(fid)
        for t in FLAG_ID_TABLES:
            if t == "change_history":
                # 历史 append-only：更名本身再记一笔，旧账一笔不丢
                assert after[t] == before[t] + 1, f"{t} 历史账异常 before={before} after={after}"
            else:
                assert after[t] == before[t], f"{t} 行数丢失 before={before} after={after}"
        assert _orphan_tables() == []

        # 规则页 / 发布页读口必须挂新键，不能仍旧键或裸 flag_id
        rules = client.get("/api/rules", headers=h).json()["official"]
        mine = [x for x in rules if x["flag_id"] == fid and x["env_id"] == ENV_PROD]
        assert mine and mine[0]["flag_key"] == KEY_NEW
        rels = client.get("/api/releases", headers=h).json()["official"]
        mine_rel = [x for x in rels if x["flag_id"] == fid and x["env_id"] == ENV_PROD]
        assert mine_rel and all(x["flag_key"] == KEY_NEW for x in mine_rel)
        assert all(x.get("flag_key") != KEY_OLD for x in rels)

        # 历史服务：更名入史，旧发布历史经同一 flag_id  JOIN 出新键（历史同源）
        hist = client.get("/api/history", headers=h).json()["items"]
        assert any(x["action"] == "flag_rename" and KEY_NEW in x["detail"] for x in hist)
        pub_hist = [x for x in hist if x["action"] == "publish" and x["flag_id"] == fid]
        assert pub_hist and all(x["flag_key"] == KEY_NEW for x in pub_hist)

        # 同一张迁移票：甲路径直接 committed
        orders = client.get("/api/flags/rename-migrations", headers=h).json()["items"]
        ticket = next(x for x in orders if x["id"] == tid)
        assert ticket["status"] in ("committed", "promoted")
        assert ticket.get("path") in ("jia", "official") or ticket.get("mode") == "official"


def test_jia_rename_by_old_key_also_works():
    """兼容按 old_key 定位的更名请求。"""
    with TestClient(app) as client:
        h = _h(client)
        seed_bid_curve(client, h, with_override=False)
        r = client.post(
            "/api/flags/rename-migrate",
            headers=h,
            json={"old_key": KEY_OLD, "new_key": KEY_NEW},
        )
        assert r.status_code == 200, r.text
        ev = client.post(
            "/api/evaluate",
            headers=h,
            json={"flag_key": KEY_NEW, "env_code": "prod", "subject_key": SUBJECT},
        )
        assert ev.status_code == 200 and ev.json()["enabled"] is True


def test_jia_duplicate_new_key_rejected_and_state_untouched():
    with TestClient(app) as client:
        h = _h(client)
        fid = seed_bid_curve(client, h, with_override=False)
        before = _table_counts(fid)
        r = client.post(
            "/api/flags/rename-migrate",
            headers=h,
            json={"flag_id": fid, "new_key": "checkout.new_pay"},  # 已被种子占用
        )
        assert r.status_code == 400, r.text
        # 旧键与正式规则原样还在
        ev = client.post(
            "/api/evaluate",
            headers=h,
            json={"flag_key": KEY_OLD, "env_code": "prod", "subject_key": SUBJECT},
        )
        assert ev.status_code == 200 and ev.json()["enabled"] is True
        assert _table_counts(fid) == before


def test_jia_failure_mid_migration_rolls_back_whole_tx(monkeypatch):
    """甲路径失败整笔回滚：迁移中段抛错，键改动/脏写/迁移票全部不得落库。"""
    import flags_svc

    def boom(conn, old_id, new_id):
        conn.execute("UPDATE flags SET title='DIRTY-SHOULD-ROLLBACK' WHERE id=?", (old_id,))
        raise RuntimeError("grayroll12 injected migration failure")

    monkeypatch.setattr(flags_svc, "migrate_all_references", boom, raising=False)

    with TestClient(app, raise_server_exceptions=False) as client:
        h = _h(client)
        fid = seed_bid_curve(client, h, with_override=False)
        r = client.post(
            "/api/flags/rename-migrate",
            headers=h,
            json={"flag_id": fid, "new_key": KEY_NEW},
        )
        assert r.status_code == 500, r.text

        db = sqlite3.connect(DB_PATH)
        try:
            row = db.execute("SELECT key, title FROM flags WHERE id=?", (fid,)).fetchone()
            assert row[0] == KEY_OLD
            assert row[1] != "DIRTY-SHOULD-ROLLBACK"
            assert db.execute(
                "SELECT COUNT(*) FROM flags WHERE key=?", (KEY_NEW,)
            ).fetchone()[0] == 0
            assert db.execute(
                "SELECT COUNT(*) FROM flag_rename_orders WHERE new_key=? AND status='committed'",
                (KEY_NEW,),
            ).fetchone()[0] == 0
        finally:
            db.close()

        ev = client.post(
            "/api/evaluate",
            headers=h,
            json={"flag_key": KEY_OLD, "env_code": "prod", "subject_key": SUBJECT},
        )
        assert ev.status_code == 200 and ev.json()["enabled"] is True

        # 锁不得被失败请求卡死：撤掉故障后同键更名可成功
        monkeypatch.undo()
        again = client.post(
            "/api/flags/rename-migrate",
            headers=h,
            json={"flag_id": fid, "new_key": KEY_NEW},
        )
        assert again.status_code == 200, again.text


def test_jia_rename_requires_writer():
    with TestClient(app) as client:
        h = _h(client)
        fid = seed_bid_curve(client, h, with_override=False)
        v = _h(client, "viewer", "viewer123")
        r = client.post(
            "/api/flags/rename-migrate",
            headers=v,
            json={"flag_id": fid, "new_key": KEY_NEW},
        )
        assert r.status_code == 403, r.text
