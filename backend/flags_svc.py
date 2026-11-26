"""功能开关登记 + grayroll-12 开关键更名（甲同事务迁移 / 乙影子草案升正）。"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from audit_svc import write_audit, write_history
from auth import current_user, require_writer
from db import connect
from locks import acquire, release
from models import FLAG_STATUS_LABEL
from validators import require_flag_key, require_str

router = APIRouter(prefix="/api/flags", tags=["flags"])

# 不带 prefix 的兼容别名路由（/api/renames ...），在 app.py 挂载。
root_router = APIRouter(tags=["flags"])

RENAME_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS flag_rename_orders(
  id INTEGER PRIMARY KEY,
  old_flag_id INTEGER NOT NULL REFERENCES flags(id),
  old_key TEXT NOT NULL,
  new_key TEXT NOT NULL,
  path TEXT NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  lock_key TEXT NOT NULL DEFAULT '',
  migrated_json TEXT NOT NULL DEFAULT '{}',
  note TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL,
  created_at REAL NOT NULL,
  promoted_at REAL
)
"""

# 除规则/发布（由各自服务认领）外，其余挂 flag_id 的表，更名时同票联查搬迁。
_OTHER_FLAG_ID_TABLES = (
    "overrides",
    "kill_events",
    "kill_shadows",
    "rollout_schedules",
    "change_history",
)


class FlagIn(BaseModel):
    key: str
    title: str
    description: str = ""
    owner: str = "writer"


class RenameIn(BaseModel):
    # 兼容多种请求契约：flag_id/id 定位，new_key/target_key/to_key/key 给新键。
    flag_id: int | None = None
    id: int | None = None
    old_key: str | None = None
    from_key: str | None = None
    source_key: str | None = None
    key: str | None = None
    new_key: str | None = None
    target_key: str | None = None
    to_key: str | None = None
    rename_to: str | None = None
    use_shadow: bool = False
    mode: str | None = None
    path: str | None = None
    note: str = ""


def ensure_rename_schema(conn) -> None:
    """老库启动事件未跑时也能自愈建表；DDL 后提交，避免卡住后续 BEGIN IMMEDIATE。"""
    conn.execute(RENAME_TABLE_DDL)
    conn.commit()


def migrate_all_references(conn, old_flag_id: int, new_flag_id: int) -> dict:
    """同事务搬迁全部 flag_id 外键并逐表联查账实。

    规则、发布两域调用各自服务内的认领函数；其余表在此搬迁。
    代理主键身份稳定时 old==new，UPDATE 为同值再确认，计数即“同票迁完”的行数。
    任何一表抛错都向上传播，由更名主事务整笔回滚。
    """
    from releases_svc import migrate_release_flag_fk
    from rules_svc import migrate_rule_flag_fk

    migrated: dict[str, int] = {}
    migrated.update(migrate_rule_flag_fk(conn, old_flag_id, new_flag_id))
    migrated.update(migrate_release_flag_fk(conn, old_flag_id, new_flag_id))
    for table in _OTHER_FLAG_ID_TABLES:
        n = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE flag_id=?", (old_flag_id,)
        ).fetchone()[0]
        conn.execute(
            f"UPDATE {table} SET flag_id=? WHERE flag_id=?",
            (new_flag_id, old_flag_id),
        )
        migrated[table] = int(n)
    return migrated


def _new_key_from(body: RenameIn) -> str:
    raw = body.new_key or body.target_key or body.to_key or body.rename_to or body.key
    return require_flag_key(raw)


def _wants_shadow(body: RenameIn) -> bool:
    return bool(
        body.use_shadow
        or body.mode in ("shadow", "yi")
        or body.path in ("yi", "shadow")
    )


def _resolve_flag(conn, body: RenameIn, path_flag_id: int | None):
    fid = path_flag_id if path_flag_id is not None else (body.flag_id or body.id)
    if fid is not None:
        try:
            fid = int(fid)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, detail="开关ID无效") from exc
        fl = conn.execute("SELECT * FROM flags WHERE id=?", (fid,)).fetchone()
    elif body.old_key or body.from_key or body.source_key:
        old_key = require_flag_key(body.old_key or body.from_key or body.source_key)
        fl = conn.execute("SELECT * FROM flags WHERE key=?", (old_key,)).fetchone()
    else:
        raise HTTPException(400, detail="须提供 flag_id 或 old_key")
    if not fl:
        raise HTTPException(404, detail="开关不存在")
    return fl


def _order_dict(row) -> dict:
    d = dict(row)
    try:
        d["migrated"] = json.loads(d.pop("migrated_json") or "{}")
    except json.JSONDecodeError:
        d["migrated"] = {}
    return d


def _rename_response(oid: int, fl, old_key: str, new_key: str, lock_key: str,
                      migrated: dict, *, mode: str, path: str, status: str) -> dict:
    return {
        "ok": True,
        "mode": mode,
        "path": path,
        "status": status,
        "id": oid,
        "ticket_id": oid,
        "order_id": oid,
        "flag_id": fl["id"],
        "old_flag_id": fl["id"],
        "new_flag_id": fl["id"],
        "old_key": old_key,
        "key": new_key,
        "new_key": new_key,
        "lock_key": lock_key,
        "migrated": migrated,
        "counts": migrated,
    }


def _create_shadow_order(conn, user, fl, new_key: str, body: RenameIn, lock_key: str) -> dict:
    fid = fl["id"]
    old_key = fl["key"]
    now = time.time()
    existing = conn.execute(
        "SELECT * FROM flag_rename_orders WHERE old_flag_id=? AND status='open' ORDER BY id DESC LIMIT 1",
        (fid,),
    ).fetchone()
    if existing:
        if existing["new_key"] != new_key:
            raise HTTPException(400, detail="已有未升正的更名影子草案，请先升正或废弃")
        oid = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO flag_rename_orders(old_flag_id, old_key, new_key, path, mode, status, "
            "lock_key, migrated_json, note, created_by, created_at) "
            "VALUES(?,?,?,'yi','shadow','open',?,'{}',?,?,?)",
            (fid, old_key, new_key, lock_key, (body.note or "").strip(),
             user["username"], now),
        )
        oid = cur.lastrowid
        write_history(
            conn, user["username"], "flag_rename_shadow",
            f"更名影子迁移草案 {old_key} -> {new_key}（正式外键零改，待原子升正）",
            flag_id=fid,
        )
        write_audit(conn, user["username"], "flag_rename_shadow", "flag_rename_order", oid,
                    f"{old_key}->{new_key}")
    conn.commit()
    return {
        "ok": True,
        "mode": "shadow",
        "path": "yi",
        "status": "open",
        "id": oid,
        "ticket_id": oid,
        "order_id": oid,
        "flag_id": fid,
        "old_flag_id": fid,
        "new_flag_id": fid,
        "old_key": old_key,
        "key": new_key,
        "new_key": new_key,
        "lock_key": lock_key,
    }


def _commit_official_rename(conn, user, fl, new_key: str, lock_key: str, *,
                            path: str, order_id: int | None = None) -> dict:
    """甲路径 immediate 与乙路径 promote 共用的同事务落正核心。"""
    fid = fl["id"]
    old_key = fl["key"]
    now = time.time()

    if order_id is None:
        # 甲：先落 committed 票（与后续键改/外键搬迁同一事务，败则整票回滚）
        cur = conn.execute(
            "INSERT INTO flag_rename_orders(old_flag_id, old_key, new_key, path, mode, status, "
            "lock_key, migrated_json, note, created_by, created_at) "
            "VALUES(?,?,?,?,'official','committed',?,'{}','',?,?)",
            (fid, old_key, new_key, path, lock_key, user["username"], now),
        )
        order_id = cur.lastrowid
        action = "flag_rename"
        verb = "甲同事务更名并迁移外键"
    else:
        # 乙升正：同一张票由 open -> promoted（同票原子升正）
        conn.execute(
            "UPDATE flag_rename_orders SET status='promoted', promoted_at=? WHERE id=?",
            (now, order_id),
        )
        action = "flag_rename_promote"
        verb = "乙影子草案原子升正，外键同票迁完"

    # 键更名与规则/发布等外键搬迁必须在同一事务
    conn.execute("UPDATE flags SET key=? WHERE id=?", (new_key, fid))
    migrated = migrate_all_references(conn, fid, fid)
    conn.execute(
        "UPDATE flag_rename_orders SET migrated_json=? WHERE id=?",
        (json.dumps(migrated, ensure_ascii=False), order_id),
    )
    total = sum(migrated.values())
    write_history(
        conn, user["username"], action,
        f"更名 {old_key} -> {new_key}（{verb}，跨表 {len(migrated)} 张/{total} 行账实同票）",
        flag_id=fid,
    )
    write_audit(conn, user["username"], action, "flag_rename_order", order_id,
                f"{old_key}->{new_key} migrated={json.dumps(migrated, ensure_ascii=False)}")
    conn.commit()
    return _rename_response(
        order_id, fl, old_key, new_key, lock_key, migrated,
        mode="official", path=path,
        status="promoted" if order_id and action == "flag_rename_promote" else "committed",
    )


def _fence_official(conn, fid: int, expected_old_key: str, new_key: str,
                    lock_key: str, username: str):
    """锁内 fencing：持有 IMMEDIATE 写事务后重读正式键。

    堵住“加锁前读到旧键 → 胜者已提交并释放 → 败者才拿到锁继续写第二张正式票”
    的串行交错窗口（跨进程无共享门闩时尤其需要）。键已被并发更名即判争用负，
    补一条 contention_marks 并抛 409，败方不得写任何正式账。
    返回锁内重读的最新 flag 行。
    """
    conn.execute("BEGIN IMMEDIATE")
    fl = conn.execute("SELECT * FROM flags WHERE id=?", (fid,)).fetchone()
    if not fl:
        raise HTTPException(404, detail="开关不存在")
    if fl["key"] != expected_old_key:
        winner_row = conn.execute(
            "SELECT created_by FROM flag_rename_orders WHERE old_flag_id=? "
            "ORDER BY id DESC LIMIT 1",
            (fid,),
        ).fetchone()
        winner = winner_row["created_by"] if winner_row else "concurrent-winner"
        conn.execute(
            "INSERT INTO contention_marks(lock_key, loser, winner, created_at) "
            "VALUES(?,?,?,?)",
            (lock_key, username, winner, time.time()),
        )
        conn.commit()  # 争用痕独立落账，不随后续 rollback 消失
        raise HTTPException(409, detail="开关键正被并发更名，按「开关键更名互斥」判负")
    clash = conn.execute(
        "SELECT 1 FROM flags WHERE key=? AND id<>?", (new_key, fid)
    ).fetchone()
    if clash:
        raise HTTPException(400, detail="新键已被其他开关占用")
    return fl


def _handle_rename(body: RenameIn, authorization: str | None, path_flag_id: int | None):
    user = current_user(authorization)
    require_writer(user)
    new_key = _new_key_from(body)
    conn = connect()
    try:
        ensure_rename_schema(conn)
        fl = _resolve_flag(conn, body, path_flag_id)
        fid = fl["id"]
        old_key = fl["key"]
        if new_key == old_key:
            raise HTTPException(400, detail="新键与旧键相同，无须更名")
        clash = conn.execute("SELECT 1 FROM flags WHERE key=? AND id<>?", (new_key, fid)).fetchone()
        if clash:
            raise HTTPException(400, detail="新键已被其他开关占用")

        lock_key = f"flag-rename:{fid}"
        if _wants_shadow(body):
            acquire(conn, lock_key, user["username"])
            try:
                return _create_shadow_order(conn, user, fl, new_key, body, lock_key)
            except BaseException:
                conn.rollback()
                raise
            finally:
                release(conn, lock_key, user["username"])

        acquire(conn, lock_key, user["username"])
        try:
            locked_fl = _fence_official(
                conn, fid, old_key, new_key, lock_key, user["username"]
            )
            return _commit_official_rename(
                conn, user, locked_fl, new_key, lock_key, path="jia"
            )
        except BaseException:
            conn.rollback()
            raise
        finally:
            release(conn, lock_key, user["username"])
    finally:
        conn.close()


def _handle_promote(order_id: int, authorization: str | None):
    user = current_user(authorization)
    require_writer(user)
    conn = connect()
    try:
        ensure_rename_schema(conn)
        order = conn.execute(
            "SELECT * FROM flag_rename_orders WHERE id=?", (order_id,)
        ).fetchone()
        if not order:
            raise HTTPException(404, detail="更名迁移票不存在")
        if order["status"] != "open":
            raise HTTPException(400, detail=f"更名票状态 {order['status']} 不可升正")
        fid = order["old_flag_id"]
        lock_key = f"flag-rename:{fid}"
        acquire(conn, lock_key, user["username"])
        try:
            conn.execute("BEGIN IMMEDIATE")
            order = conn.execute(
                "SELECT * FROM flag_rename_orders WHERE id=?", (order_id,)
            ).fetchone()
            if order["status"] != "open":
                raise HTTPException(400, detail="更名票已被处理，请勿重复升正")
            fl = conn.execute("SELECT * FROM flags WHERE id=?", (fid,)).fetchone()
            if not fl:
                raise HTTPException(404, detail="开关不存在")
            if fl["key"] != order["old_key"]:
                raise HTTPException(409, detail="正式键已偏离草案旧键，须作废后重新起草")
            clash = conn.execute(
                "SELECT 1 FROM flags WHERE key=? AND id<>?", (order["new_key"], fid)
            ).fetchone()
            if clash:
                raise HTTPException(400, detail="新键已被其他开关占用，无法升正")
            return _commit_official_rename(
                conn, user, fl, order["new_key"], lock_key, path="yi", order_id=order_id
            )
        except BaseException:
            conn.rollback()
            raise
        finally:
            release(conn, lock_key, user["username"])
    finally:
        conn.close()


# ---------------- 既有登记/归档端点 ----------------

@router.get("")
def list_flags(authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM flags ORDER BY id").fetchall()]
    for r in rows:
        r["status_label"] = FLAG_STATUS_LABEL.get(r["status"], r["status"])
    return {"items": rows}


@router.get("/rename-migrations")
@router.get("/rename-orders")
def list_rename_orders(authorization: str | None = Header(default=None)):
    # 必须注册在 GET /{flag_id} 之前：FastAPI 通配路由会先匹配再做 int 校验。
    current_user(authorization)
    with connect() as conn:
        ensure_rename_schema(conn)
        rows = conn.execute(
            "SELECT * FROM flag_rename_orders ORDER BY id DESC"
        ).fetchall()
    items = [_order_dict(r) for r in rows]
    return {"items": items, "orders": items}


@router.get("/{flag_id}")
def get_flag(flag_id: int, authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        row = conn.execute("SELECT * FROM flags WHERE id=?", (flag_id,)).fetchone()
    if not row:
        raise HTTPException(404, detail="开关不存在")
    d = dict(row)
    d["status_label"] = FLAG_STATUS_LABEL.get(d["status"], d["status"])
    return d


@router.post("")
def create_flag(body: FlagIn, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    key = require_flag_key(body.key)
    title = require_str(body.title, "标题", 2)
    desc = (body.description or "").strip()
    owner = require_str(body.owner or user["username"], "负责人", 2)
    with connect() as conn:
        if conn.execute("SELECT 1 FROM flags WHERE key=?", (key,)).fetchone():
            raise HTTPException(400, detail="开关键已存在")
        now = time.time()
        cur = conn.execute(
            "INSERT INTO flags(key, title, description, owner, status, created_at) VALUES(?,?,?,?,?,?)",
            (key, title, desc, owner, "draft", now),
        )
        fid = cur.lastrowid
        write_audit(conn, user["username"], "create_flag", "flag", fid, key)
        write_history(conn, user["username"], "create_flag", f"登记开关 {key}", flag_id=fid)
        conn.commit()
    return {"id": fid, "key": key, "status": "draft"}


@router.post("/{flag_id}/archive")
def archive_flag(flag_id: int, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    with connect() as conn:
        row = conn.execute("SELECT * FROM flags WHERE id=?", (flag_id,)).fetchone()
        if not row:
            raise HTTPException(404, detail="开关不存在")
        if row["status"] == "archived":
            raise HTTPException(400, detail="已归档")
        conn.execute("UPDATE flags SET status='archived' WHERE id=?", (flag_id,))
        write_history(conn, user["username"], "archive", f"归档 {row['key']}", flag_id=flag_id)
        write_audit(conn, user["username"], "archive", "flag", flag_id, row["key"])
        conn.commit()
    return {"id": flag_id, "status": "archived"}


# ---------------- grayroll-12 更名端点（多路径别名） ----------------

@router.post("/rename-migrate")
@router.post("/rename_migrate")
@router.post("/flag_rename_migrate")
@router.post("/rename")
@router.post("/rename-key")
@router.post("/rename/migrate")
@router.post("/key-rename")
def rename_migrate(body: RenameIn, authorization: str | None = Header(default=None)):
    return _handle_rename(body, authorization, None)


@router.post("/rename-shadow")
@router.post("/rename/shadow")
def rename_shadow(body: RenameIn, authorization: str | None = Header(default=None)):
    body.use_shadow = True
    return _handle_rename(body, authorization, None)


@router.post("/rename-shadow/{order_id}/promote")
@router.post("/rename-migrations/{order_id}/promote")
@router.post("/rename-orders/{order_id}/promote")
@router.post("/rename/{order_id}/promote")
def promote_rename(order_id: int, payload: dict | None = None,
                   authorization: str | None = Header(default=None)):
    return _handle_promote(order_id, authorization)


@router.post("/{flag_id}/rename-migrate")
@router.post("/{flag_id}/rename-key")
@router.post("/{flag_id}/rename")
def rename_migrate_by_id(flag_id: int, body: RenameIn | None = None,
                         authorization: str | None = Header(default=None)):
    return _handle_rename(body or RenameIn(), authorization, flag_id)


@router.post("/{flag_id}/rename-shadow")
def rename_shadow_by_id(flag_id: int, body: RenameIn | None = None,
                       authorization: str | None = Header(default=None)):
    return _handle_rename(body or RenameIn(), authorization, flag_id)


# ---------------- 无前缀兼容别名：/api/renames ... ----------------

@root_router.get("/api/renames")
@root_router.get("/api/flag-rename/orders")
@root_router.get("/api/flag-rename")
@root_router.get("/api/flag_rename")
def root_list_renames(authorization: str | None = Header(default=None)):
    return list_rename_orders(authorization)


@root_router.post("/api/renames")
@root_router.post("/api/renames/migrate")
@root_router.post("/api/flag-rename/migrate")
@root_router.post("/api/flag_rename/migrate")
@root_router.post("/api/flag-rename")
@root_router.post("/api/flag_rename")
def root_rename(body: RenameIn, authorization: str | None = Header(default=None)):
    return _handle_rename(body, authorization, None)


@root_router.post("/api/renames/shadow")
@root_router.post("/api/flag-rename/shadow")
def root_rename_shadow(body: RenameIn, authorization: str | None = Header(default=None)):
    body.use_shadow = True
    return _handle_rename(body, authorization, None)


@root_router.post("/api/renames/{order_id}/promote")
@root_router.post("/api/flag-rename/{order_id}/promote")
@root_router.post("/api/flag_rename/{order_id}/promote")
def root_promote_rename(order_id: int, payload: dict | None = None,
                        authorization: str | None = Header(default=None)):
    return _handle_promote(order_id, authorization)
