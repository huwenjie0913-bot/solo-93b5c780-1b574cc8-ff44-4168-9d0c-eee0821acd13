"""SQLite 存储层：方案表 + 对比快照。"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    data_json    TEXT NOT NULL,
    route_json   TEXT,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id      INTEGER NOT NULL,
    name         TEXT NOT NULL,
    data_json    TEXT NOT NULL,
    route_json   TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    FOREIGN KEY (plan_id) REFERENCES plans(id) ON DELETE CASCADE
);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        path = Path(current_app.instance_path) / "patrol.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
        conn.executescript(SCHEMA)
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# plans
# ---------------------------------------------------------------------------

def list_plans() -> list[dict]:
    rows = get_db().execute(
        "SELECT id, name, created_at, updated_at FROM plans ORDER BY updated_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_plan(plan_id: int) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM plans WHERE id = ?", (plan_id,)
    ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"], "name": row["name"],
        "data": json.loads(row["data_json"]),
        "route": json.loads(row["route_json"]) if row["route_json"] else None,
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
    }


def create_plan(name: str, data: dict, route: dict | None) -> int:
    now = int(time.time() * 1000)
    cur = get_db().execute(
        "INSERT INTO plans (name, data_json, route_json, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (name, json.dumps(data, ensure_ascii=False),
         json.dumps(route, ensure_ascii=False) if route else None, now, now),
    )
    get_db().commit()
    return cur.lastrowid


def update_plan(plan_id: int, name: str | None, data: dict,
                route: dict | None) -> bool:
    now = int(time.time() * 1000)
    get_db().execute(
        "UPDATE plans SET name = COALESCE(?, name), data_json = ?, "
        "route_json = ?, updated_at = ? WHERE id = ?",
        (name, json.dumps(data, ensure_ascii=False),
         json.dumps(route, ensure_ascii=False) if route is not None else None,
         now, plan_id),
    )
    get_db().commit()
    return True


def delete_plan(plan_id: int) -> bool:
    get_db().execute("DELETE FROM plans WHERE id = ?", (plan_id,))
    get_db().commit()
    return True


# ---------------------------------------------------------------------------
# snapshots（另存比较）
# ---------------------------------------------------------------------------

def list_snapshots(plan_id: int) -> list[dict]:
    rows = get_db().execute(
        "SELECT id, plan_id, name, created_at FROM snapshots "
        "WHERE plan_id = ? ORDER BY created_at DESC", (plan_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def add_snapshot(plan_id: int, name: str, data: dict, route: dict) -> int:
    cur = get_db().execute(
        "INSERT INTO snapshots (plan_id, name, data_json, route_json, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (plan_id, name, json.dumps(data, ensure_ascii=False),
         json.dumps(route, ensure_ascii=False), int(time.time() * 1000)),
    )
    get_db().commit()
    return cur.lastrowid


def get_snapshot(snap_id: int) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM snapshots WHERE id = ?", (snap_id,)
    ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"], "planId": row["plan_id"], "name": row["name"],
        "data": json.loads(row["data_json"]),
        "route": json.loads(row["route_json"]),
        "createdAt": row["created_at"],
    }


def delete_snapshot(snap_id: int) -> bool:
    get_db().execute("DELETE FROM snapshots WHERE id = ?", (snap_id,))
    get_db().commit()
    return True
