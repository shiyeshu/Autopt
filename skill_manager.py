# skill_manager.py - Skill 系统
# 用户可以随时增删 skill、激活/关闭 skill；已激活的 skill 内容会注入 Agent 的 system prompt，增强 Agent 能力。
# 存储方式：
#   1) SQLite: data/skills.db 中的 skills 表
#   2) 文件式: skills/*.md（文件名=skill名，front matter 含 title/description/enabled，正文=content）
# 文件式 skill 以文件为准：scan_skills_dir() 时未入库自动入库，已存在则更新内容。
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
SKILLS_DIR = BASE_DIR / "skills"
SKILLS_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "skills.db"

# 所有 SQLite 操作共享同一把锁，保证线程安全
_lock = threading.Lock()

# skill 名：英文+下划线
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _parse_md_skill(file_path: Path) -> dict:
    """解析 skills/*.md：front matter(title/description/enabled) + 正文。

    front matter 格式：第一行 ---，然后 key: value 键值行，最后 --- 分隔正文。
    无 front matter 时整个文件内容视为正文。
    """
    text = file_path.read_text(encoding="utf-8")
    title = None
    description = None
    enabled = 1
    content = text
    if text.lstrip().startswith("---"):
        lines = text.splitlines()
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end = i
                break
        if end is not None:
            fm_lines = lines[1:end]
            content = "\n".join(lines[end + 1:]).strip()
            for line in fm_lines:
                if ":" not in line:
                    continue
                key, _, val = line.partition(":")
                key = key.strip().lower()
                val = val.strip()
                if key == "title":
                    title = val
                elif key == "description":
                    description = val
                elif key == "enabled":
                    enabled = 1 if val.lower() in ("1", "true", "yes", "on") else 0
    return {"title": title, "description": description, "enabled": enabled, "content": content}


class SkillManager:
    """Skill 管理器：SQLite 持久化 + skills/ 目录文件式 skill。"""

    def __init__(self):
        self._init_db()
        # 启动时自动扫描 skills/ 目录，导入/更新文件式 skill
        self.scan_skills_dir()

    # ---------- 内部 ----------
    def _init_db(self):
        with _lock, _conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS skills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    title TEXT DEFAULT '',
                    description TEXT DEFAULT '',
                    content TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1,   -- 1激活 0关闭
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_skills_name ON skills(name)")

    # ---------- 基础 CRUD ----------
    def list_skills(self) -> list[dict]:
        """返回所有 skill（含 enabled 状态）。"""
        with _lock, _conn() as conn:
            rows = conn.execute("SELECT * FROM skills ORDER BY id ASC").fetchall()
            return [dict(r) for r in rows]

    def get_skill(self, name: str) -> Optional[dict]:
        """按名称获取单个 skill，不存在返回 None。"""
        with _lock, _conn() as conn:
            r = conn.execute("SELECT * FROM skills WHERE name=?", (name,)).fetchone()
            return dict(r) if r else None

    def create_skill(self, name: str, title: str = None, description: str = "",
                     content: str = "", enabled: bool = True) -> int:
        """创建 skill，返回新记录 id。name 重复或格式非法时抛 ValueError。"""
        name = (name or "").strip()
        if not _NAME_RE.fullmatch(name):
            raise ValueError(f"skill 名只能包含英文和下划线: {name!r}")
        with _lock, _conn() as conn:
            exists = conn.execute("SELECT id FROM skills WHERE name=?", (name,)).fetchone()
            if exists:
                raise ValueError(f"skill 已存在: {name}")
            cur = conn.execute(
                "INSERT INTO skills(name, title, description, content, enabled, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (name, title if title is not None else name, description or "",
                 content or "", 1 if enabled else 0, _now(), _now()),
            )
            return cur.lastrowid

    def update_skill(self, name: str, **fields):
        """更新 skill 的 title/description/content/enabled（只接受这几个字段）。"""
        allowed = {"title", "description", "content", "enabled"}
        sets, values = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "enabled":
                v = 1 if v else 0
            sets.append(f"{k}=?")
            values.append(v)
        if not sets:
            return
        values.append(_now())
        with _lock, _conn() as conn:
            conn.execute(
                f"UPDATE skills SET {', '.join(sets)}, updated_at=? WHERE name=?",
                (*values, name),
            )

    def delete_skill(self, name: str):
        with _lock, _conn() as conn:
            conn.execute("DELETE FROM skills WHERE name=?", (name,))

    # ---------- 激活/关闭 ----------
    def set_enabled(self, name: str, enabled: bool):
        """激活(True)/关闭(False) skill。"""
        with _lock, _conn() as conn:
            conn.execute(
                "UPDATE skills SET enabled=?, updated_at=? WHERE name=?",
                (1 if enabled else 0, _now(), name),
            )

    def get_active_skill_names(self) -> list[str]:
        """返回所有已激活 skill 的名称列表。"""
        with _lock, _conn() as conn:
            rows = conn.execute(
                "SELECT name FROM skills WHERE enabled=1 ORDER BY id ASC"
            ).fetchall()
            return [r["name"] for r in rows]

    def get_active_skills_content(self) -> str:
        """返回所有已激活 skill 的格式化文本，用于注入 Agent 的 system prompt。"""
        with _lock, _conn() as conn:
            rows = conn.execute(
                "SELECT * FROM skills WHERE enabled=1 ORDER BY id ASC"
            ).fetchall()
        if not rows:
            return ""
        parts = ["【已启用的技能】"]
        for r in rows:
            parts.append(f"{r['name']}: {r['title'] or ''}")
            parts.append(r["content"] or "")
        return "\n\n".join(parts).strip()

    # ---------- 文件式 skill ----------
    def scan_skills_dir(self):
        """扫描 skills/ 目录的 .md 文件：未入库的自动入库，已存在的更新内容（以文件为准）。"""
        for f in sorted(SKILLS_DIR.glob("*.md")):
            name = f.stem
            data = _parse_md_skill(f)
            with _lock, _conn() as conn:
                exists = conn.execute("SELECT id FROM skills WHERE name=?", (name,)).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO skills(name, title, description, content, enabled, created_at, updated_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (name, data["title"] or name, data["description"] or "",
                         data["content"], data["enabled"], _now(), _now()),
                    )
                else:
                    conn.execute(
                        "UPDATE skills SET title=?, description=?, content=?, enabled=?, updated_at=? WHERE name=?",
                        (data["title"] or name, data["description"] or "",
                         data["content"], data["enabled"], _now(), name),
                    )


# 模块级单例；导入即建表并自动扫描 skills/ 目录
skill_manager = SkillManager()
