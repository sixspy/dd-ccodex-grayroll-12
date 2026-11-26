"""grayroll-12 红测乙（flag_rename_eval_fake）：未升正新键不得读到空规则假绿。

乙路径正确性口径：更名先落影子迁移草案（flag_rename_orders, path=yi,
status=open），正式 flags.key 与全部正式外键零改；求值读口仍钉在旧键正式区。

盯的假绿：若提前把求值读口切到未升正新键，新键名下没有正式规则，接口会回
200 + enabled=false + no_official_rule——表面“求值链路是绿的”，实际读到空
规则（题面“新键下规则为空”）。正道是草案期新键拒识并结构化挂出待升正影子，
旧键求值口径不变；原子升正后读口才切到新键，新键真命中、旧键拒识。
"""
from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app import app
from db import DB_PATH

KEY_OLD = "ads.bid_curve"
KEY_NEW = "ads.bid_curve_v2"
ENV_PROD = 3
SUBJECT = "u-grayroll12-yi"


def _h(client):
    t = client.post(
        "/api/login", json={"username": "admin", "password": "admin123"}
    ).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def _seed(client, h, percent=100):
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
            "percent": percent,
            "summary": "乙路径夹具正式规则",
            "use_shadow": False,
        },
    )
    assert pub.status_code == 200, pub.text
    return fid


def _eval(client, h, key):
    return client.post(
        "/api/evaluate",
        headers=h,
        json={"flag_key": key, "env_code": "prod", "subject_key": SUBJECT},
    )


def test_yi_shadow_draft_leaves_formal_port_on_old_key():
    with TestClient(app) as client:
        h = _h(client)
        fid = _seed(client, h)

        r = client.post(
            "/api/flags/rename-shadow",
            headers=h,
            json={"flag_id": fid, "new_key": KEY_NEW},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mode"] == "shadow", body
        oid = body.get("ticket_id") or body.get("order_id") or body["id"]

        # 正式区零改：flags.key 仍旧，新键没有正式行，正式规则原样
        db = sqlite3.connect(DB_PATH)
        try:
            assert db.execute("SELECT key FROM flags WHERE id=?", (fid,)).fetchone()[0] == KEY_OLD
            assert db.execute(
                "SELECT COUNT(*) FROM flags WHERE key=?", (KEY_NEW,)
            ).fetchone()[0] == 0
            assert db.execute(
                "SELECT COUNT(*) FROM flag_rules WHERE flag_id=? AND official=1", (fid,)
            ).fetchone()[0] == 1
            order = db.execute(
                "SELECT path, status, old_key, new_key FROM flag_rename_orders WHERE id=?",
                (oid,),
            ).fetchone()
            assert order[0] in ("yi", "shadow")
            assert order[1] == "open"
            assert order[2] == KEY_OLD and order[3] == KEY_NEW
        finally:
            db.close()

        # 旧键求值读口不变：percent=100 仍真命中，并显式挂出待升正更名影子
        old = _eval(client, h, KEY_OLD)
        assert old.status_code == 200, old.text
        ob = old.json()
        assert ob["enabled"] is True and ob["reason"] == "rule"
        pr = ob.get("pending_rename")
        assert pr and pr["new_key"] == KEY_NEW and pr["status"] == "open"
        assert pr.get("promoted") is False

        # 假绿关：未升正新键必须拒识，绝不允许 200 空规则 no_official_rule
        new = _eval(client, h, KEY_NEW)
        assert new.status_code == 404, (
            "未升正新键不得作为正式读口返回空规则假绿",
            new.status_code,
            new.text,
        )
        detail = new.json()["detail"]
        code = detail.get("code") if isinstance(detail, dict) else ""
        assert code == "rename_shadow_pending", detail

        # 求值总览板同样不得提前冒出新键行
        board = client.get("/api/evaluate/board", headers=h).json()
        assert all(row["flag_key"] != KEY_NEW for row in board["rules"])
        assert any(row["flag_key"] == KEY_OLD for row in board["rules"])


def test_yi_promote_atomically_switches_eval_port():
    with TestClient(app) as client:
        h = _h(client)
        fid = _seed(client, h)
        draft = client.post(
            "/api/flags/rename-shadow",
            headers=h,
            json={"flag_id": fid, "new_key": KEY_NEW},
        ).json()
        oid = draft.get("ticket_id") or draft.get("order_id") or draft["id"]

        p = client.post(
            f"/api/flags/rename-shadow/{oid}/promote",
            headers=h,
            json={},
        )
        assert p.status_code == 200, p.text
        pb = p.json()
        assert pb["mode"] == "official", pb
        assert int((pb.get("migrated") or {}).get("flag_rules", 0)) >= 1

        # 升正后读口才切换：新键真命中（规则版本/比例不丢）
        new = _eval(client, h, KEY_NEW)
        assert new.status_code == 200, new.text
        nb = new.json()
        assert nb["enabled"] is True and nb["reason"] == "rule"
        assert nb["percent"] == 100 and nb["flag_key"] == KEY_NEW
        assert nb.get("pending_rename") is None

        # 旧键拒识
        assert _eval(client, h, KEY_OLD).status_code == 404

        db = sqlite3.connect(DB_PATH)
        try:
            assert db.execute("SELECT key FROM flags WHERE id=?", (fid,)).fetchone()[0] == KEY_NEW
            st = db.execute(
                "SELECT status FROM flag_rename_orders WHERE id=?", (oid,)
            ).fetchone()[0]
            assert st == "promoted"
            # 外键全部随同票迁完，无孤儿
            orphan = db.execute(
                "SELECT COUNT(*) FROM flag_rules r LEFT JOIN flags f ON f.id=r.flag_id "
                "WHERE f.id IS NULL"
            ).fetchone()[0]
            assert orphan == 0
        finally:
            db.close()


def test_yi_shadow_draft_does_not_evaluate_new_key_as_empty_rule_even_percent_zero():
    """换一个角度堵假绿：即便旧规则是 0%，草案期新键也不得回 no_official_rule 200。"""
    with TestClient(app) as client:
        h = _h(client)
        fid = _seed(client, h, percent=0)
        draft = client.post(
            "/api/flags/rename-shadow",
            headers=h,
            json={"flag_id": fid, "new_key": KEY_NEW},
        )
        assert draft.status_code == 200, draft.text  # 草案必须真实落库，禁止空绿
        new = _eval(client, h, KEY_NEW)
        assert new.status_code == 404
        # 旧键读到的是“正式规则 0%”而不是“无正式规则”，口径不得被草案污染
        old = _eval(client, h, KEY_OLD).json()
        assert old["reason"] == "rule" and old["percent"] == 0
