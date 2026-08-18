# -*- coding: utf-8 -*-
"""
agent_manager.py — Agent 角色管理模块

将 Agent 角色的编排权交给用户：
- 首次初始化时（agents 表为空）从 config/agents.yaml 导入 6 个内置角色，
  内置角色 is_builtin=1，不可删除但可编辑；
- 支持用户自定义角色的增删改查，持久化到 SQLite: data/agents.db；
- 提供 load_agent_configs() 兼容层，返回结构与
  yaml.safe_load(config/agents.yaml) 一致（{key: {role, goal, backstory}}），
  供 agents.py 无缝切换数据源。

线程安全：所有数据库操作由 threading.Lock 串行化（每次操作独立连接，
锁内完成读-改-写，避免多线程下 SQLITE_BUSY）。

典型用法:
    from agent_manager import agent_manager, load_agent_configs

    agent_manager.create_agent("scanner", "端口扫描专家", "扫描目标端口", "行为准则",
                               tools=["file_read_tool"])
    configs = load_agent_configs()          # {key: {role, goal, backstory}}
    all_cfg = agent_manager.get_all_agent_configs()  # 含 tools，仅 enabled
    yaml_text = agent_manager.to_yaml()     # 生成 agents.yaml 格式文本
"""

import json
import os
import sqlite3
import threading
import warnings
from datetime import datetime
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# 路径常量（基于本文件所在目录，即项目根，避免依赖 config.py/agents.py）
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
DATABASE_PATH = DATA_DIR / "agents.db"
AGENTS_YAML_PATH = PROJECT_ROOT / "config" / "agents.yaml"

# 内置角色默认工具（从 agents.py 各节点 bind_tools 定义推断 + 内置网络工具）
# 含 http_get/http_post/dns_lookup 等内置工具，新环境自动生效
BUILTIN_TOOLS = {
    "strategist": ["file_read_tool", "http_get_tool", "dns_lookup_tool"],
    "deputy": [],
    "operator": ["list_custom_tool", "http_get_tool"],
    "auditor": ["execution_tool", "http_get_tool", "http_post_tool", "dns_lookup_tool"],
    "reporter": ["file_read_tool", "file_write_tool"],
    "html_reporter": ["file_read_tool", "file_write_tool"],
}

# update_agent 允许更新的字段
EDITABLE_FIELDS = ("role", "goal", "backstory", "tools", "enabled")


# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _normalize_key(key) -> str:
    """角色 key 归一化：去空白 + 小写（与 agents.yaml 的 key 风格一致）。"""
    k = str(key).strip().lower()
    if not k:
        raise ValueError("agent key 不能为空")
    return k


def _dumps_tools(tools) -> str | None:
    """工具列表 -> 存储值：None/空列表 -> NULL（可空表示无工具），否则 JSON 数组字符串。"""
    if tools is None:
        return None
    if not isinstance(tools, (list, tuple, set)):
        raise ValueError("tools 必须是工具名列表或 JSON 数组字符串")
    items = [str(t) for t in tools]
    if not items:
        return None
    return json.dumps(items, ensure_ascii=False)


def _loads_tools(raw) -> list:
    """数据库 tools 字段 -> 工具名列表（NULL/空/非法 JSON -> []）。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(t) for t in raw]
    text = str(raw).strip()
    if not text:
        return []
    try:
        val = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if isinstance(val, list):
        return [str(t) for t in val]
    return []


def _parse_tools_param(tools):
    """create/update 入参的 tools 校验：接受列表或 JSON 数组字符串。"""
    if tools is None:
        return None
    if isinstance(tools, str):
        text = tools.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            raise ValueError(
                'tools 字符串必须是 JSON 数组，如 \'["file_read_tool"]\''
            )
        if not isinstance(parsed, list):
            raise ValueError("tools JSON 必须是数组")
        return _dumps_tools(parsed)
    return _dumps_tools(tools)


# 无损 YAML 导出：含换行的字符串用字面块（保留 \\n），单行字符串保持简洁
class _LosslessDumper(yaml.SafeDumper):
    pass


def _str_representer(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_LosslessDumper.add_representer(str, _str_representer)


# ---------------------------------------------------------------------------
# AgentManager
# ---------------------------------------------------------------------------
class AgentManager:
    """基于 SQLite 的 Agent 角色管理器（threading.Lock 保证线程安全）。

    数据表 agents:
        id INTEGER PRIMARY KEY
        key TEXT UNIQUE NOT NULL           角色 key，如 strategist
        role TEXT NOT NULL                 角色名中文，如 渗透指挥官
        goal TEXT                          目标
        backstory TEXT                     背景故事/行为准则
        tools TEXT                         工具名列表（JSON 数组），可空表示无工具
        enabled INTEGER DEFAULT 1
        is_builtin INTEGER DEFAULT 0       内置角色不可删除但可编辑
        created_at TEXT
        updated_at TEXT
    """

    def __init__(self, db_path=None):
        self._db_path = str(db_path or DATABASE_PATH)
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        self._init_schema()
        self._import_builtins_if_empty()

    @property
    def db_path(self) -> str:
        return self._db_path

    # ---------- 内部实现 ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agents (
                        id INTEGER PRIMARY KEY,
                        key TEXT UNIQUE NOT NULL,
                        role TEXT NOT NULL,
                        goal TEXT,
                        backstory TEXT,
                        tools TEXT,
                        enabled INTEGER DEFAULT 1,
                        is_builtin INTEGER DEFAULT 0,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def _load_yaml_config(self) -> dict:
        try:
            with open(AGENTS_YAML_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception as e:  # noqa: BLE001 - 初始化不因配置缺失而崩溃
            warnings.warn(f"[agent_manager] 读取 {AGENTS_YAML_PATH} 失败: {e}")
            return {}
        return cfg if isinstance(cfg, dict) else {}

    def _import_builtins_if_empty(self):
        """agents 表为空时，从 config/agents.yaml 导入内置角色（is_builtin=1）。
        非空时也会把内置角色的 tools 同步为 BUILTIN_TOOLS（保留用户改的
        role/goal/backstory，仅确保内置工具接入）。"""
        with self._lock:
            conn = self._connect()
            try:
                count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
                if count > 0:
                    # 同步内置角色的工具（新内置工具接入已有环境）
                    now = _now()
                    for key, tools in BUILTIN_TOOLS.items():
                        k = _normalize_key(key)
                        conn.execute(
                            "UPDATE agents SET tools=?, updated_at=? WHERE key=? AND is_builtin=1",
                            (_dumps_tools(tools), now, k),
                        )
                    conn.commit()
                    return
                cfg = self._load_yaml_config()
                now = _now()
                for key, data in cfg.items():
                    k = _normalize_key(key)
                    # 原样保存 yaml 值（含折叠标量 clip 产生的结尾 \n），
                    # 保证 load_agent_configs() 与 yaml.safe_load 逐字节一致
                    role = str(data.get("role") or k)
                    if not role.strip():
                        role = k
                    conn.execute(
                        """
                        INSERT INTO agents
                            (key, role, goal, backstory, tools,
                             enabled, is_builtin, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?)
                        """,
                        (
                            k,
                            role,
                            data.get("goal"),
                            data.get("backstory"),
                            _dumps_tools(BUILTIN_TOOLS.get(k, [])),
                            now,
                            now,
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "key": row["key"],
            "role": row["role"],
            "goal": row["goal"],
            "backstory": row["backstory"],
            "tools": _loads_tools(row["tools"]),
            "enabled": bool(row["enabled"]),
            "is_builtin": int(row["is_builtin"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _fetch(self, key: str):
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM agents WHERE key = ?", (_normalize_key(key),)
                ).fetchone()
                return self._row_to_dict(row) if row else None
            finally:
                conn.close()

    # ---------- 增删改查 ----------

    def list_agents(self, include_disabled: bool = True) -> list:
        """列出全部角色；include_disabled=False 时只返回启用中的角色。"""
        with self._lock:
            conn = self._connect()
            try:
                sql = "SELECT * FROM agents"
                if not include_disabled:
                    sql += " WHERE enabled = 1"
                sql += " ORDER BY id"
                rows = conn.execute(sql).fetchall()
                return [self._row_to_dict(r) for r in rows]
            finally:
                conn.close()

    def get_agent(self, key: str) -> dict | None:
        """按 key 获取单个角色（含 id/enabled/is_builtin 等完整信息）；不存在返回 None。"""
        return self._fetch(key)

    def create_agent(self, key, role, goal=None, backstory=None,
                     tools=None, enabled=True) -> int:
        """新增自定义角色，返回新角色的 id。key 重复抛 ValueError。

        tools 接受工具名列表或 JSON 数组字符串（如 '["file_read_tool"]'）。
        """
        k = _normalize_key(key)
        role = str(role).strip() if role is not None else ""
        if not role:
            raise ValueError("role（角色名）不能为空")
        tools_sql = _parse_tools_param(tools)
        now = _now()
        with self._lock:
            conn = self._connect()
            try:
                exist = conn.execute(
                    "SELECT 1 FROM agents WHERE key = ?", (k,)
                ).fetchone()
                if exist:
                    raise ValueError(f"agent key 已存在: {k}")
                cur = conn.execute(
                    """
                    INSERT INTO agents
                        (key, role, goal, backstory, tools,
                         enabled, is_builtin, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (k, role, goal, backstory, tools_sql,
                     1 if enabled else 0, now, now),
                )
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    def update_agent(self, key, **fields):
        """更新角色字段（role/goal/backstory/tools/enabled），返回更新后的角色 dict。

        角色不存在或字段不在允许集合内抛 ValueError。
        """
        k = _normalize_key(key)
        unknown = set(fields) - set(EDITABLE_FIELDS)
        if unknown:
            raise ValueError(f"不支持更新的字段: {sorted(unknown)}")
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT id FROM agents WHERE key = ?", (k,)
                ).fetchone()
                if not row:
                    raise ValueError(f"agent 不存在: {k}")
                updates = dict(fields)
                if "role" in updates:
                    role = str(updates["role"]).strip() if updates["role"] is not None else ""
                    if not role:
                        raise ValueError("role（角色名）不能为空")
                    updates["role"] = role
                if "tools" in updates:
                    updates["tools"] = _parse_tools_param(updates["tools"])
                if "enabled" in updates:
                    updates["enabled"] = 1 if updates["enabled"] else 0
                updates["updated_at"] = _now()
                set_clause = ", ".join(f"{col} = ?" for col in updates)
                conn.execute(
                    f"UPDATE agents SET {set_clause} WHERE key = ?",
                    (*updates.values(), k),
                )
                conn.commit()
            finally:
                conn.close()
        return self._fetch(k)

    def delete_agent(self, key):
        """删除角色；内置角色（is_builtin=1）拒绝删除，抛 ValueError。"""
        k = _normalize_key(key)
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT is_builtin FROM agents WHERE key = ?", (k,)
                ).fetchone()
                if not row:
                    raise ValueError(f"agent 不存在: {k}")
                if row["is_builtin"]:
                    raise ValueError(f"内置角色不可删除: {k}")
                conn.execute("DELETE FROM agents WHERE key = ?", (k,))
                conn.commit()
            finally:
                conn.close()

    def set_enabled(self, key: str, enabled: bool):
        """启用/停用角色（UI 便捷方法）。"""
        return self.update_agent(key, enabled=bool(enabled))

    # ---------- 配置读取（供 agents.py / UI 使用） ----------

    def get_agent_config(self, key: str) -> dict:
        """返回 {role, goal, backstory, tools}；角色不存在抛 ValueError。"""
        agent = self._fetch(key)
        if not agent:
            raise ValueError(f"agent 不存在: {_normalize_key(key)}")
        return {
            "role": agent["role"],
            "goal": agent["goal"],
            "backstory": agent["backstory"],
            "tools": agent["tools"],
        }

    def get_all_agent_configs(self) -> dict:
        """返回 {key: {role, goal, backstory, tools}}，仅 enabled 的角色。"""
        result = {}
        for agent in self.list_agents(include_disabled=False):
            result[agent["key"]] = {
                "role": agent["role"],
                "goal": agent["goal"],
                "backstory": agent["backstory"],
                "tools": agent["tools"],
            }
        return result

    def get_tools_for(self, key: str) -> list:
        """返回该角色的工具名列表；角色不存在抛 ValueError。"""
        agent = self._fetch(key)
        if not agent:
            raise ValueError(f"agent 不存在: {_normalize_key(key)}")
        return list(agent["tools"])

    # ---------- YAML 导出 ----------

    def to_yaml(self) -> str:
        """生成 agents.yaml 格式文本（全部角色，含禁用角色，供保存/查看）。

        导出内容 {key: {role, goal, backstory}} 与 config/agents.yaml 结构一致；
        使用字面块（|）保证多行文本换行无损。
        """
        data = {}
        for agent in self.list_agents(include_disabled=True):
            data[agent["key"]] = {
                "role": agent["role"],
                "goal": agent["goal"],
                "backstory": agent["backstory"],
            }
        header = (
            "# agents.yaml (由 agent_manager.to_yaml() 生成，数据源: data/agents.db)\n"
            "# 说明: 提示词不显式指名工具（工具通过 bind_tools 提供给模型），\n"
            "# role/goal/backstory 可在管理界面编辑；本文件仅作保存/查看。\n"
        )
        body = yaml.dump(
            data,
            Dumper=_LosslessDumper,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        return header + body


# ---------------------------------------------------------------------------
# 兼容层 + 模块级单例
# ---------------------------------------------------------------------------
def load_agent_configs() -> dict:
    """返回 {key: {role, goal, backstory}}，与 yaml.safe_load(config/agents.yaml)
    结构一致，仅包含 enabled 的角色，供 agents.py 无缝改用 agent_manager 数据
    （agents_config['strategist']['role'] 这类用法不变）。"""
    return {
        key: {
            "role": cfg["role"],
            "goal": cfg["goal"],
            "backstory": cfg["backstory"],
        }
        for key, cfg in agent_manager.get_all_agent_configs().items()
    }


# 模块级单例（导入即初始化数据库并导入内置角色）
agent_manager = AgentManager()


if __name__ == "__main__":
    # 简易自检：打印当前角色清单与 YAML 预览
    for a in agent_manager.list_agents():
        flag = "[内置]" if a["is_builtin"] else "[自定义]"
        status = "启用" if a["enabled"] else "禁用"
        print(f"{flag} {a['key']:<14} {status} tools={a['tools']} role={a['role'][:30]}")
    print("---- to_yaml() 预览（前 12 行）----")
    print("\n".join(agent_manager.to_yaml().splitlines()[:12]))
