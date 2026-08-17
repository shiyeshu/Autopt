# database.py - SQLite 数据层
# 管理项目(Project)/任务(Task)/会话消息(Message)/资产(Asset)/设置(Setting)
# 模块3/4/5/6 的数据基础
import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "autopt.db"

_lock = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """初始化所有表结构（幂等）。"""
    with _lock, _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                title TEXT NOT NULL,
                target TEXT DEFAULT '',
                status TEXT DEFAULT 'running',  -- running/completed/stopped/failed
                max_rounds INTEGER DEFAULT 3,
                current_round INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                role TEXT NOT NULL,       -- user/assistant/system/tool
                agent_name TEXT DEFAULT '',
                content TEXT NOT NULL,
                round INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                task_id INTEGER,
                asset_type TEXT NOT NULL,  -- domain/ip/port/url/vuln/credential
                value TEXT NOT NULL,
                detail TEXT DEFAULT '',
                source TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
            CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id);
            CREATE INDEX IF NOT EXISTS idx_assets_project ON assets(project_id);
            """
        )
        # 轻量迁移: 老版本 assets 表缺 updated_at 列时补齐
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(assets)").fetchall()}
        if "updated_at" not in cols:
            conn.execute(
                "ALTER TABLE assets ADD COLUMN updated_at TEXT DEFAULT ''"
            )
            conn.execute("UPDATE assets SET updated_at=created_at")


# ==================== 项目 ====================
def create_project(name: str, description: str = "") -> int:
    with _lock, _conn() as conn:
        cur = conn.execute(
            "INSERT INTO projects(name, description, created_at, updated_at) VALUES(?,?,?,?)",
            (name, description, _now(), _now()),
        )
        return cur.lastrowid


def list_projects() -> list[dict]:
    with _lock, _conn() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_project(project_id: int) -> Optional[dict]:
    with _lock, _conn() as conn:
        r = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return dict(r) if r else None


def get_or_create_project(name: str) -> int:
    """按名称获取项目，不存在则创建（用于快速按名分组）。"""
    with _lock, _conn() as conn:
        r = conn.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()
        if r:
            conn.execute(
                "UPDATE projects SET updated_at=? WHERE id=?", (_now(), r["id"])
            )
            return r["id"]
        cur = conn.execute(
            "INSERT INTO projects(name, created_at, updated_at) VALUES(?,?,?)",
            (name, _now(), _now()),
        )
        return cur.lastrowid


def update_project(project_id: int, description: str | None = None, name: str | None = None):
    with _lock, _conn() as conn:
        if name is not None:
            conn.execute("UPDATE projects SET name=?, updated_at=? WHERE id=?", (name, _now(), project_id))
        if description is not None:
            conn.execute("UPDATE projects SET description=?, updated_at=? WHERE id=?", (description, _now(), project_id))


def delete_project(project_id: int):
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


# ==================== 任务 ====================
def create_task(project_id: int | None, title: str, target: str = "", max_rounds: int = 3) -> int:
    with _lock, _conn() as conn:
        cur = conn.execute(
            """INSERT INTO tasks(project_id, title, target, status, max_rounds, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            (project_id, title, target, "running", max_rounds, _now(), _now()),
        )
        return cur.lastrowid


def list_tasks(project_id: int | None = None) -> list[dict]:
    with _lock, _conn() as conn:
        if project_id is not None:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE project_id=? ORDER BY created_at DESC", (project_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_task(task_id: int) -> Optional[dict]:
    with _lock, _conn() as conn:
        r = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(r) if r else None


def update_task(task_id: int, **fields):
    allowed = {"title", "target", "status", "max_rounds", "current_round"}
    sets = [f"{k}=?" for k in fields if k in allowed]
    if not sets:
        return
    values = [fields[k] for k in fields if k in allowed]
    values.append(_now())
    with _lock, _conn() as conn:
        conn.execute(f"UPDATE tasks SET {', '.join(sets)}, updated_at=? WHERE id=?", (*values, task_id))


def delete_task(task_id: int):
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))


# ==================== 消息（会话历史） ====================
def add_message(task_id: int, role: str, content: str, agent_name: str = "", round: int = 0):
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO messages(task_id, role, agent_name, content, round, created_at) VALUES(?,?,?,?,?,?)",
            (task_id, role, agent_name, content, round, _now()),
        )


def list_messages(task_id: int) -> list[dict]:
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE task_id=? ORDER BY id ASC", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def delete_messages(task_id: int):
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM messages WHERE task_id=?", (task_id,))


# ==================== 资产 ====================
def add_asset(project_id: int, asset_type: str, value: str, detail: str = "", source: str = "", task_id: int | None = None):
    """添加资产，同类型同值去重。"""
    with _lock, _conn() as conn:
        exists = conn.execute(
            "SELECT id FROM assets WHERE project_id=? AND asset_type=? AND value=?",
            (project_id, asset_type, value),
        ).fetchone()
        if exists:
            conn.execute(
                "UPDATE assets SET updated_at=? WHERE id=?", (_now(), exists["id"])
            )
            return exists["id"]
        cur = conn.execute(
            """INSERT INTO assets(project_id, task_id, asset_type, value, detail, source, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (project_id, task_id, asset_type, value, detail, source, _now(), _now()),
        )
        return cur.lastrowid


def list_assets(project_id: int, asset_type: str | None = None) -> list[dict]:
    with _lock, _conn() as conn:
        if asset_type:
            rows = conn.execute(
                "SELECT * FROM assets WHERE project_id=? AND asset_type=? ORDER BY created_at DESC",
                (project_id, asset_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM assets WHERE project_id=? ORDER BY created_at DESC", (project_id,)
            ).fetchall()
        return [dict(r) for r in rows]


def delete_asset(asset_id: int):
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))


# ==================== 设置 ====================
def get_setting(key: str, default: Any = "") -> Any:
    with _lock, _conn() as conn:
        r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not r:
            return default
        try:
            return json.loads(r["value"])
        except Exception:
            return r["value"]


def set_setting(key: str, value: Any):
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=?, updated_at=?",
            (key, json.dumps(value, ensure_ascii=False), _now(), json.dumps(value, ensure_ascii=False), _now()),
        )


def all_settings() -> dict:
    with _lock, _conn() as conn:
        rows = conn.execute("SELECT * FROM settings").fetchall()
        out = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except Exception:
                out[r["key"]] = r["value"]
        return out


# 初始化
init_db()
