# rag_store.py - RAG 存储与检索
# 模块1: 将已完成任务的关键信息手动收录进 RAG 知识库，
# 后续任务开始时检索相关知识注入 system prompt，提升 Agent 能力。
#
# 检索策略（两级）:
# 1. 若配置了远程 Embedding 服务（EMBEDDING_BASE_URL/API_KEY/MODEL）→ 语义向量检索（余弦相似度）
# 2. 否则回退本地 bigram 关键词 + TF 权重检索
#
# Embedding 配置（通过 .env 或 settings 表）:
#   EMBEDDING_BASE_URL: OpenAI 兼容 embedding 接口地址，如 https://api.siliconflow.cn/v1
#   EMBEDDING_API_KEY:  API Key
#   EMBEDDING_MODEL:    模型名，如 BAAI/bge-m3
#   RAG_EMBEDDING_ENABLED: true/false 显式开关（默认: 配置齐全则自动启用）
import json
import math
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "rag.db"

_lock = threading.Lock()

# 中文分词辅助：按常见分隔符切词，中文连续串切成 bigram（2字滑动窗口）
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:/\\-]+|[\u4e00-\u9fff]+")


def _tokenize(text: str) -> list[str]:
    """分词：英文/数字按原词，中文连续串切成 bigram 便于模糊匹配。"""
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text or ""):
        if re.fullmatch(r"[\u4e00-\u9fff]+", raw):
            if len(raw) >= 2:
                tokens.extend(raw[i : i + 2] for i in range(len(raw) - 1))
            else:
                tokens.append(raw)
        else:
            tokens.append(raw.lower())
    return tokens


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init():
    with _lock, _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rag_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                category TEXT DEFAULT 'general',  -- strategy/tool/vuln/target/lesson
                tags TEXT DEFAULT '[]',
                source_task_id INTEGER DEFAULT 0,
                source_task_title TEXT DEFAULT '',
                project_name TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rag_tokens (
                entry_id INTEGER NOT NULL,
                token TEXT NOT NULL,
                weight REAL NOT NULL,
                PRIMARY KEY(entry_id, token),
                FOREIGN KEY(entry_id) REFERENCES rag_entries(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS rag_vectors (
                entry_id INTEGER PRIMARY KEY,
                vector TEXT NOT NULL,   -- JSON 数组（embedding 向量）
                dim INTEGER NOT NULL,
                model TEXT NOT NULL,    -- 生成向量的模型名（模型变更需重算）
                updated_at TEXT NOT NULL,
                FOREIGN KEY(entry_id) REFERENCES rag_entries(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_rag_tokens_token ON rag_tokens(token);
            """
        )


# ==================== Embedding 客户端 ====================
def _embedding_config() -> dict:
    """读取 embedding 配置（优先 settings 表，其次 .env）。"""
    cfg = {}
    try:
        import database as db
        for key in ("EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL", "RAG_EMBEDDING_ENABLED"):
            v = db.get_setting(key, "")
            if v:
                cfg[key] = v
    except Exception:
        pass
    for key in ("EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL"):
        if key not in cfg:
            v = os.getenv(key, "").strip()
            if v:
                cfg[key] = v
    return cfg


def _embedding_enabled() -> bool:
    cfg = _embedding_config()
    if cfg.get("RAG_EMBEDDING_ENABLED", "").lower() == "false":
        return False
    return bool(cfg.get("EMBEDDING_BASE_URL") and cfg.get("EMBEDDING_API_KEY") and cfg.get("EMBEDDING_MODEL"))


def _get_embedding(texts: list[str]) -> list[list[float]]:
    """调用 OpenAI 兼容 embedding 接口，返回向量列表。"""
    cfg = _embedding_config()
    base_url = cfg["EMBEDDING_BASE_URL"].rstrip("/")
    api_key = cfg["EMBEDDING_API_KEY"]
    model = cfg["EMBEDDING_MODEL"]
    url = f"{base_url}/embeddings"

    import urllib.request
    body = json.dumps({"model": model, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    items = data.get("data", [])
    items.sort(key=lambda x: x.get("index", 0))
    return [it["embedding"] for it in items]


def _cosine_sim(a: list[float], b: list[float]) -> float:
    """余弦相似度。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class RagStore:
    """RAG 知识库：手动收录任务经验，按关键词/语义检索。"""

    def __init__(self):
        self._embedding_ok = False
        self._embedding_error = ""

    def add_entry(
        self,
        title: str,
        content: str,
        category: str = "general",
        tags: list[str] | None = None,
        source_task_id: int = 0,
        source_task_title: str = "",
        project_name: str = "",
    ) -> int:
        """添加一条知识（title+content 去重）。"""
        tags_json = json.dumps(tags or [], ensure_ascii=False)
        with _lock, _conn() as conn:
            exists = conn.execute(
                "SELECT id FROM rag_entries WHERE title=? AND content=?",
                (title, content),
            ).fetchone()
            if exists:
                conn.execute(
                    "UPDATE rag_entries SET category=?, tags=?, source_task_id=?, source_task_title=?, project_name=?, created_at=? WHERE id=?",
                    (category, tags_json, source_task_id, source_task_title, project_name, _now(), exists["id"]),
                )
                entry_id = exists["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO rag_entries(title, content, category, tags, source_task_id, source_task_title, project_name, created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (title, content, category, tags_json, source_task_id, source_task_title, project_name, _now()),
                )
                entry_id = cur.lastrowid

            # 重建 token 权重（TF，本地回退检索用）
            conn.execute("DELETE FROM rag_tokens WHERE entry_id=?", (entry_id,))
            tokens = _tokenize(content)
            token_counts: dict[str, int] = {}
            for t in tokens:
                token_counts[t] = token_counts.get(t, 0) + 1
            total = len(tokens) or 1
            for t, cnt in token_counts.items():
                conn.execute(
                    "INSERT OR REPLACE INTO rag_tokens(entry_id, token, weight) VALUES(?,?,?)",
                    (entry_id, t, cnt / total),
                )
            return entry_id

    def add_entry_with_embedding(
        self,
        title: str,
        content: str,
        category: str = "general",
        tags: list[str] | None = None,
        source_task_id: int = 0,
        source_task_title: str = "",
        project_name: str = "",
    ) -> int:
        """收录并计算 embedding 向量（若启用远程 embedding）。"""
        entry_id = self.add_entry(title, content, category, tags, source_task_id, source_task_title, project_name)
        if _embedding_enabled():
            try:
                text = f"{title}\n{content}"[:2000]
                vecs = _get_embedding([text])
                if vecs:
                    cfg = _embedding_config()
                    with _lock, _conn() as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO rag_vectors(entry_id, vector, dim, model, updated_at) VALUES(?,?,?,?,?)",
                            (entry_id, json.dumps(vecs[0]), len(vecs[0]), cfg.get("EMBEDDING_MODEL", ""), _now()),
                        )
                    self._embedding_ok = True
            except Exception as e:
                self._embedding_ok = False
                self._embedding_error = str(e)[:200]
        return entry_id

    def list_entries(self, category: str | None = None) -> list[dict]:
        with _lock, _conn() as conn:
            if category:
                rows = conn.execute(
                    "SELECT * FROM rag_entries WHERE category=? ORDER BY created_at DESC", (category,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM rag_entries ORDER BY created_at DESC").fetchall()
            return [dict(r) for r in rows]

    def get_entry(self, entry_id: int) -> Optional[dict]:
        with _lock, _conn() as conn:
            r = conn.execute("SELECT * FROM rag_entries WHERE id=?", (entry_id,)).fetchone()
            return dict(r) if r else None

    def delete_entry(self, entry_id: int):
        with _lock, _conn() as conn:
            conn.execute("DELETE FROM rag_entries WHERE id=?", (entry_id,))

    def clear(self):
        with _lock, _conn() as conn:
            conn.execute("DELETE FROM rag_entries")
            conn.execute("DELETE FROM rag_tokens")
            conn.execute("DELETE FROM rag_vectors")

    # ==================== 导出 / 导入（知识库迁移分享） ====================
    def export_to_dict(self) -> dict:
        """导出知识库为可分享的 dict（含条目 + 可选向量 + 元数据）。"""
        entries = self.list_entries()
        # 批量取向量
        with _lock, _conn() as conn:
            vec_rows = conn.execute("SELECT entry_id, vector, dim, model FROM rag_vectors").fetchall()
        vec_map = {r["entry_id"]: {"vector": r["vector"], "dim": r["dim"], "model": r["model"]} for r in vec_rows}

        items = []
        for e in entries:
            item = {
                "title": e["title"],
                "content": e["content"],
                "category": e["category"],
                "tags": e["tags"],
                "source_task_title": e["source_task_title"],
                "project_name": e["project_name"],
            }
            v = vec_map.get(e["id"])
            if v:
                item["vector"] = v["vector"]      # JSON 字符串
                item["vector_model"] = v["model"]
            items.append(item)

        return {
            "format": "autopt-rag",
            "version": 1,
            "exported_at": _now(),
            "entry_count": len(items),
            "embedding_model": (vec_map[next(iter(vec_map))]["model"] if vec_map else ""),
            "entries": items,
        }

    def export_to_json(self) -> str:
        """导出为 JSON 字符串（分享用）。"""
        return json.dumps(self.export_to_dict(), ensure_ascii=False, indent=2)

    def import_from_dict(self, data: dict, recompute_embedding: bool = True) -> tuple[int, str]:
        """从 dict 导入知识库条目。

        recompute_embedding=True: 配置了 embedding 时重新计算向量（推荐，
        因为向量依赖模型，分享方/接收方模型可能不同）。
        recompute_embedding=False: 直接沿用导出时的向量（需模型一致）。
        返回 (导入条数, 提示信息)。
        """
        if not isinstance(data, dict) or not data.get("entries"):
            return 0, "无效的知识库文件或没有条目"
        entries = data["entries"]
        imported = 0
        skipped = 0
        for item in entries:
            title = str(item.get("title", "")).strip()
            content = str(item.get("content", "")).strip()
            if not title or not content:
                skipped += 1
                continue
            category = str(item.get("category", "general"))
            try:
                tags = json.loads(item["tags"]) if isinstance(item.get("tags"), str) else (item.get("tags") or [])
            except Exception:
                tags = []
            source_task_title = str(item.get("source_task_title", ""))
            project_name = str(item.get("project_name", ""))

            if recompute_embedding:
                self.add_entry_with_embedding(
                    title, content, category, tags,
                    source_task_title=source_task_title,
                    project_name=project_name,
                )
            else:
                entry_id = self.add_entry(title, content, category, tags, source_task_title=source_task_title, project_name=project_name)
                # 沿用导出向量（若带且模型匹配）
                vec_str = item.get("vector")
                vec_model = item.get("vector_model")
                if vec_str:
                    cfg = _embedding_config()
                    cur_model = cfg.get("EMBEDDING_MODEL", "")
                    if cur_model and vec_model == cur_model:
                        try:
                            vec = json.loads(vec_str)
                            with _lock, _conn() as conn:
                                conn.execute(
                                    "INSERT OR REPLACE INTO rag_vectors(entry_id, vector, dim, model, updated_at) VALUES(?,?,?,?,?)",
                                    (entry_id, json.dumps(vec), len(vec), cur_model, _now()),
                                )
                        except Exception:
                            pass
            imported += 1
        msg = f"导入 {imported} 条"
        if skipped:
            msg += f"，跳过 {skipped} 条无效条目"
        return imported, msg

    def import_from_json(self, json_str: str, recompute_embedding: bool = True) -> tuple[int, str]:
        """从 JSON 字符串导入知识库。"""
        try:
            data = json.loads(json_str)
        except Exception as e:
            return 0, f"JSON 解析失败: {e}"
        if isinstance(data, list):
            # 兼容纯列表格式
            data = {"entries": data}
        return self.import_from_dict(data, recompute_embedding=recompute_embedding)

    def search(self, query: str, limit: int = 5, category: str | None = None) -> list[dict]:
        """检索：优先语义向量（远程 embedding），失败/未配置回退关键词。"""
        if _embedding_enabled():
            try:
                return self._search_semantic(query, limit, category)
            except Exception as e:
                self._embedding_ok = False
                self._embedding_error = str(e)[:200]
        return self._search_keyword(query, limit, category)

    def _search_semantic(self, query: str, limit: int = 5, category: str | None = None) -> list[dict]:
        """语义向量检索（余弦相似度）。"""
        q_vec = _get_embedding([query[:1000]])[0]
        with _lock, _conn() as conn:
            if category:
                rows = conn.execute(
                    "SELECT * FROM rag_entries WHERE category=?", (category,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM rag_entries").fetchall()
            scored = []
            for row in rows:
                vr = conn.execute(
                    "SELECT vector FROM rag_vectors WHERE entry_id=?", (row["id"],)
                ).fetchone()
                if not vr:
                    continue
                vec = json.loads(vr["vector"])
                sim = _cosine_sim(q_vec, vec)
                if sim > 0:
                    scored.append((sim, dict(row)))
            scored.sort(key=lambda x: -x[0])
            return [r for _, r in scored[:limit]]

    def _search_keyword(self, query: str, limit: int = 5, category: str | None = None) -> list[dict]:
        """按关键词权重检索（本地回退）。"""
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        with _lock, _conn() as conn:
            if category:
                rows = conn.execute(
                    "SELECT * FROM rag_entries WHERE category=?",
                    (category,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM rag_entries").fetchall()

            scored = []
            for row in rows:
                trows = conn.execute(
                    "SELECT token, weight FROM rag_tokens WHERE entry_id=?", (row["id"],)
                ).fetchall()
                weights = {t["token"]: t["weight"] for t in trows}
                score = 0.0
                hits = 0
                for q in q_tokens:
                    if q in weights:
                        score += weights[q]
                        hits += 1
                title_tokens = set(_tokenize(row["title"]))
                title_hits = len(title_tokens & set(q_tokens))
                score += title_hits * 2
                if score > 0:
                    scored.append((score, hits, dict(row)))
            scored.sort(key=lambda x: (-x[0], -x[1]))
            return [row for _, _, row in scored[:limit]]

    def embedding_status(self) -> dict:
        """返回 embedding 状态（供 GUI 展示）。"""
        return {
            "enabled": _embedding_enabled(),
            "ok": self._embedding_ok,
            "error": self._embedding_error,
            "config": {
                k: ("***" if "KEY" in k else v)
                for k, v in _embedding_config().items()
            },
        }

    def rebuild_all_vectors(self) -> tuple[int, str]:
        """为所有条目重建向量（模型变更/补录时调用）。"""
        entries = self.list_entries()
        if not entries:
            return 0, "知识库为空"
        if not _embedding_enabled():
            return 0, "未配置远程 embedding，跳过向量重建"
        texts = [f"{e['title']}\n{e['content']}"[:2000] for e in entries]
        try:
            vecs = _get_embedding(texts)
        except Exception as e:
            return 0, f"embedding 调用失败: {str(e)[:200]}"
        cfg = _embedding_config()
        with _lock, _conn() as conn:
            for e, vec in zip(entries, vecs):
                conn.execute(
                    "INSERT OR REPLACE INTO rag_vectors(entry_id, vector, dim, model, updated_at) VALUES(?,?,?,?,?)",
                    (e["id"], json.dumps(vec), len(vec), cfg.get("EMBEDDING_MODEL", ""), _now()),
                )
        self._embedding_ok = True
        return len(entries), "ok"

    def build_rag_prompt(self, query: str, limit: int = 5, category: str | None = None) -> str:
        """生成注入 system prompt 的 RAG 知识文本。"""
        entries = self.search(query, limit=limit, category=category)
        if not entries:
            return ""
        lines = ["【参考知识库（RAG）】以下是从过往任务中收录的经验，可作为决策参考："]
        for i, e in enumerate(entries, 1):
            lines.append(f"{i}. [{e['category']}] {e['title']}")
            lines.append(f"   {e['content'][:800]}")
        lines.append("【知识库结束】")
        return "\n".join(lines)


rag_store = RagStore()
_init()
