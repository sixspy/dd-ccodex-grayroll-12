"""行锁与争用痕（嵌进发布/熔断/开关键更名主写路径）。

grayroll-12「开关键更名互斥」要求两路并发只许一成一败，败方只记争用痕：
  1) 进程内：按 lock_key 分片的非阻塞 threading 门闩。SQLite 毫秒级短事务里，
     败方 BEGIN IMMEDIATE 可能等胜者提交后才拿到写锁，届时行锁已释放，单靠
     check-then-upsert 会“无锁双成”；故同进程争用由门闩立即判负。
  2) 跨进程：row_locks 行锁 + BEGIN IMMEDIATE + UNIQUE(lock_key)，挡多进程/
     多实例并发。
两层争用都写同一张 contention_marks，败方只留争用痕、正式账零改。
"""
from __future__ import annotations

import sqlite3
import threading
import time

from fastapi import APIRouter, Header, HTTPException

from auth import current_user
from db import connect

router = APIRouter(prefix="/api/locks", tags=["locks"])
TTL = 20.0


class _InProcessContention(Exception):
    def __init__(self, key: str, loser: str, winner: str | None):
        self.key = key
        self.loser = loser
        self.winner = winner or "unknown"
        super().__init__(f"contention {key} loser={loser}")


class _KeyGate:
    """按 key 分片的非阻塞进程内门闩，记录当前持有者以写争用痕。"""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._holders: dict[str, str] = {}

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            lk = self._locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._locks[key] = lk
            return lk

    def try_acquire(self, key: str, holder: str) -> None:
        lk = self._lock_for(key)
        if not lk.acquire(blocking=False):
            with self._guard:
                winner = self._holders.get(key)
            raise _InProcessContention(key, holder, winner)
        with self._guard:
            self._holders[key] = holder

    def release(self, key: str) -> None:
        with self._guard:
            self._holders.pop(key, None)
        self._lock_for(key).release()


_GATE = _KeyGate()


def _write_contention(conn, key: str, loser: str, winner: str) -> None:
    conn.execute(
        "INSERT INTO contention_marks(lock_key, loser, winner, created_at) VALUES(?,?,?,?)",
        (key, loser, winner, time.time()),
    )
    conn.commit()


def acquire(conn, key: str, holder: str) -> None:
    """占双层锁。任何一层失败都留争用痕并抛 409，调用方正式写不会发生。"""
    now = time.time()
    # 第一层：进程内门闩（非阻塞，立即判同进程争用）
    try:
        _GATE.try_acquire(key, holder)
    except _InProcessContention as exc:
        _write_contention(conn, exc.key, exc.loser, exc.winner)
        raise HTTPException(409, detail="发布锁争用，请稍后重试")
    try:
        # 第二层：跨进程 DB 行锁。IMMEDIATE 立即取库级写锁；预约登记单独
        # 提交，使持锁状态对其他进程立即可见（业务事务与占锁事务分离）。
        conn.execute(f"PRAGMA busy_timeout={int(TTL * 1000)}")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO row_locks(lock_key, holder, expires_at) VALUES(?,?,?)",
                (key, holder, now + TTL),
            )
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT holder FROM row_locks WHERE lock_key=?", (key,)
            ).fetchone()
            # 同名 holder 重入：视为同会话，放行
            if row and row["holder"] == holder:
                conn.commit()
                return
            winner = row["holder"] if row else "unknown"
            _write_contention(conn, key, holder, winner)
            raise HTTPException(409, detail="发布锁争用，请稍后重试")
        # 预约落账并提交：此后跨进程可查见该锁，直到 release 删除
        conn.commit()
    except BaseException:
        _GATE.release(key)
        raise


def release(conn, key: str, holder: str) -> None:
    """释放双层锁：先删 DB 行锁并提交，再退进程门闩。"""
    try:
        conn.execute(
            "DELETE FROM row_locks WHERE lock_key=? AND holder=?", (key, holder)
        )
        conn.commit()
    finally:
        _GATE.release(key)


@router.get("/contention")
def list_contention(authorization: str | None = Header(default=None), limit: int = 40):
    current_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_marks ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"items": [dict(r) for r in rows]}
