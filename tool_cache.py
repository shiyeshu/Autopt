# tool_cache.py - 工具参数缓存模块
# 用途:
#   1) 缓存 langchain 工具的 description / args_schema，避免每次调用工具时
#      都让 LLM 重新推测"工具是干嘛的、参数怎么传"，节省 AI token。
#   2) 记录工具调用历史(tool_calls_history)，供 LLM 参考之前的成功参数。
#   3) build_tool_hint() 从 langchain tool 对象提取信息，生成中文友好的提示文本。
#
# 存储: SQLite (data/tool_cache.db)，所有操作加 threading.Lock 保证线程安全。
import json
import sqlite3
import sys
import threading
from datetime import datetime
from pathlib import Path

# ============================================================
# UTF-8 控制台修复（Windows GBK 下中文/emoji print 崩溃）
# ============================================================
def ensure_utf8_console() -> None:
    """修复 Windows GBK 控制台下 emoji/中文 print 崩溃的问题。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


ensure_utf8_console()

# ============================================================
# 常量
# ============================================================
DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "tool_cache.db"

MAX_HISTORY_PER_TOOL = 200   # 每个工具最多保留的调用历史条数
RESULT_PREVIEW_LIMIT = 2000  # record_call 存入 result_preview 的最大长度
HINT_RESULT_PREVIEW_LIMIT = 200  # build_tool_hint 示例中展示的结果预览长度


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ToolCache:
    """工具描述/参数 schema 的 SQLite 缓存 + 调用历史记录。"""

    def __init__(self, db_path=None):
        self._db_path = str(db_path or DB_PATH)
        self._lock = threading.Lock()
        self._init_db()

    # ---------- 内部: 连接与建表 ----------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS tool_cache (
                        tool_name   TEXT PRIMARY KEY,
                        description TEXT,
                        args_schema TEXT,            -- JSON: {参数名: {type/required/default/description...}}
                        usage_count INTEGER DEFAULT 0,
                        last_used   TEXT,
                        created_at  TEXT
                    );
                    CREATE TABLE IF NOT EXISTS tool_calls_history (
                        id             INTEGER PRIMARY KEY AUTOINCREMENT,
                        tool_name      TEXT,
                        args           TEXT,          -- JSON: 调用参数
                        result_preview TEXT,          -- 结果预览（截断）
                        created_at     TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_calls_tool_time
                        ON tool_calls_history (tool_name, id DESC);
                    """
                )

    # ---------- 缓存读写 ----------
    def get_cache(self, tool_name: str):
        """返回缓存的工具元信息 dict，未命中返回 None。

        返回结构:
        {
            "tool_name": str, "description": str, "args_schema": dict,
            "usage_count": int, "last_used": str, "created_at": str,
        }
        """
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM tool_cache WHERE tool_name = ?", (tool_name,)
                ).fetchone()
        if row is None:
            return None
        try:
            args_schema = json.loads(row["args_schema"]) if row["args_schema"] else {}
        except Exception:
            args_schema = {}
        return {
            "tool_name": row["tool_name"],
            "description": row["description"],
            "args_schema": args_schema,
            "usage_count": row["usage_count"],
            "last_used": row["last_used"],
            "created_at": row["created_at"],
        }

    def set_cache(self, tool_name: str, description: str, args_schema: dict) -> None:
        """写入/更新缓存。已存在的工具保留 usage_count 与 created_at。"""
        now = _now()
        args_json = json.dumps(args_schema or {}, ensure_ascii=False)
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT usage_count, created_at FROM tool_cache WHERE tool_name = ?",
                    (tool_name,),
                ).fetchone()
                usage = row["usage_count"] if row else 0
                created = row["created_at"] if row else now
                conn.execute(
                    """
                    INSERT INTO tool_cache (tool_name, description, args_schema,
                                            usage_count, last_used, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tool_name) DO UPDATE SET
                        description = excluded.description,
                        args_schema = excluded.args_schema,
                        last_used   = excluded.last_used
                    """,
                    (tool_name, description, args_json, usage, now, created),
                )

    def increment_usage(self, tool_name: str) -> None:
        """工具调用次数 +1，并更新 last_used。"""
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE tool_cache SET usage_count = usage_count + 1, last_used = ? "
                    "WHERE tool_name = ?",
                    (_now(), tool_name),
                )

    # ---------- 调用历史 ----------
    def record_call(self, tool_name: str, args: dict, result_preview: str) -> None:
        """记录一次工具调用（参数 + 结果预览）。自动截断预览并清理过期历史。"""
        try:
            args_json = json.dumps(args or {}, ensure_ascii=False)
        except Exception:
            args_json = "{}"
        preview = str(result_preview or "")[:RESULT_PREVIEW_LIMIT]
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO tool_calls_history (tool_name, args, result_preview, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (tool_name, args_json, preview, _now()),
                )
                # 每个工具最多保留 MAX_HISTORY_PER_TOOL 条
                conn.execute(
                    """
                    DELETE FROM tool_calls_history
                    WHERE tool_name = ? AND id NOT IN (
                        SELECT id FROM tool_calls_history
                        WHERE tool_name = ? ORDER BY id DESC LIMIT ?
                    )
                    """,
                    (tool_name, tool_name, MAX_HISTORY_PER_TOOL),
                )

    def get_recent_calls(self, tool_name: str, limit: int = 5) -> list:
        """返回最近调用历史（按时间倒序），供 LLM 参考之前的成功参数。

        返回: [{"tool_name": str, "args": dict, "result_preview": str, "created_at": str}, ...]
        """
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT tool_name, args, result_preview, created_at "
                    "FROM tool_calls_history WHERE tool_name = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (tool_name, limit),
                ).fetchall()
        calls = []
        for r in rows:
            try:
                args = json.loads(r["args"]) if r["args"] else {}
            except Exception:
                args = {}
            calls.append(
                {
                    "tool_name": r["tool_name"],
                    "args": args,
                    "result_preview": r["result_preview"],
                    "created_at": r["created_at"],
                }
            )
        return calls

    # ---------- 提示文本生成 ----------
    def build_tool_hint(self, tool) -> str:
        """从 langchain tool 对象提取 name/description/args_schema，生成中文提示文本并写入缓存。

        提示内容: 工具名 / 用途描述 / 参数说明（名称、类型、是否必填、默认值、描述）/
                  最近 3 条调用示例（来自 tool_calls_history）。
        每次生成提示视为一次工具使用，自动 usage_count + 1。
        """
        name = self._extract_name(tool)
        description = self._extract_description(tool)
        args_schema = self._extract_args_schema(tool)

        self.set_cache(name, description, args_schema)
        self.increment_usage(name)

        recent_calls = self.get_recent_calls(name, limit=3)
        return self._format_hint(name, description, args_schema, recent_calls)

    def _extract_name(self, tool) -> str:
        name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
        return str(name) if name else "unknown"

    def _extract_description(self, tool) -> str:
        return str(getattr(tool, "description", "") or "")

    def _extract_args_schema(self, tool) -> dict:
        """优先用 args_schema(pydantic model) 的 JSON Schema；退回 tool.args dict。"""
        schema_obj = getattr(tool, "args_schema", None)
        if schema_obj is not None:
            try:
                if hasattr(schema_obj, "model_json_schema"):  # pydantic v2
                    js = schema_obj.model_json_schema()
                elif hasattr(schema_obj, "schema"):           # pydantic v1
                    js = schema_obj.schema()
                else:
                    js = {}
                required = set(js.get("required") or [])
                props = js.get("properties") or {}
                out = {}
                for pname, pmeta in props.items():
                    entry = dict(pmeta)
                    entry["required"] = pname in required
                    out[pname] = entry
                if out:
                    return out
            except Exception:
                pass
        args = getattr(tool, "args", None)
        if isinstance(args, dict) and args:
            return args
        return {}

    def _format_hint(self, name, description, args_schema, recent_calls) -> str:
        lines = []
        lines.append("【工具名】" + name)
        lines.append("【用途】" + (description or "（无描述）"))
        if args_schema:
            lines.append("【参数】")
            for pname, pmeta in args_schema.items():
                ptype = pmeta.get("type", "any")
                req_txt = "必填" if pmeta.get("required") else "可选"
                pdesc = pmeta.get("description", "")
                default = pmeta.get("default")
                default_txt = f"，默认值: {default}" if default is not None else ""
                lines.append(f"  - {pname} ({ptype}，{req_txt}{default_txt})：{pdesc}")
        else:
            lines.append("【参数】无")
        if recent_calls:
            lines.append("【最近调用示例】（参考历史成功参数）")
            for i, call in enumerate(recent_calls, 1):
                try:
                    args_txt = json.dumps(call["args"], ensure_ascii=False)
                except Exception:
                    args_txt = "{}"
                result_txt = (call["result_preview"] or "无")[:HINT_RESULT_PREVIEW_LIMIT]
                lines.append(f"  {i}. [{call['created_at']}] 参数: {args_txt}")
                lines.append(f"     结果: {result_txt}")
        return "\n".join(lines)

    # ---------- 清空 ----------
    def clear_cache(self) -> None:
        """清空所有工具缓存与调用历史。"""
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM tool_cache")
                conn.execute("DELETE FROM tool_calls_history")


# 模块级单例
tool_cache = ToolCache()
