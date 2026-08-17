# security.py - 集中安全逻辑
# 1) 命令安全检测（防黑名单绕过）
# 2) 路径沙箱（防路径穿越 / 任意文件读写）
# 3) 全局任务控制（GUI 停止按钮）
# 4) UTF-8 控制台修复（Windows GBK 崩溃）
import os
import re
import shlex
import subprocess
import threading
from pathlib import Path

# ============================================================
# 4) UTF-8 控制台修复
# ============================================================
def ensure_utf8_console() -> None:
    """修复 Windows GBK 控制台下 emoji/中文 print 崩溃的问题。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys := __import__("sys"), stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


# ============================================================
# 3) 全局任务控制（停止按钮）
# ============================================================
class MissionControl:
    """全局任务控制：支持 GUI 一键停止，可终止运行中的子进程。"""

    def __init__(self):
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._active_procs = set()

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    def request_stop(self) -> None:
        """请求停止：置位停止标记，并终止所有运行中的子进程。"""
        self._stop_event.set()
        with self._lock:
            procs = list(self._active_procs)
        for p in procs:
            try:
                p.terminate()
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def reset(self) -> None:
        """重置停止标记（新任务开始前调用）。"""
        self._stop_event.clear()

    def register_proc(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._active_procs.add(proc)

    def unregister_proc(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._active_procs.discard(proc)


mission_control = MissionControl()

# 文件写入锁（mission.log / ui_mission.log 跨模块共享）
file_write_lock = threading.Lock()


# ============================================================
# 1) 命令安全检测（防绕过）
# ============================================================
# 绝对禁止的破坏性命令（无论参数如何都拒绝）
FORBIDDEN_COMMANDS = {
    "shutdown", "reboot", "halt", "poweroff", "reboot.exe",
    "mkfs", "mkfs.ext2", "mkfs.ext3", "mkfs.ext4", "mkfs.xfs", "mkfs.btrfs",
    "format", "diskpart", "fdisk", "parted", "sfdisk", "gdisk",
    "dd",  # 磁盘复制/覆盖，破坏性极强
    "rm", "del", "erase", "rd", "rmdir", "remove-item",
    "clear-disk", "format-volume",
}

# 危险命令名前缀（如 mkfs.xxx / del.xxx 变体）
FORBIDDEN_PREFIXES = ("mkfs.", "del.", "format-")


def _normalize_command(command: str) -> str:
    """归一化命令：去除引号/反引号/IFS 变量/多余空白，便于检测。"""
    c = command
    c = c.replace("${IFS}", " ").replace("$IFS", " ")
    c = re.sub(r"[\"'`]", "", c)          # 去掉所有引号
    c = re.sub(r"[\\]+([ -])", r"\1", c)  # 去掉转义空白（rm\ -rf -> rm -rf）
    c = re.sub(r"\s+", " ", c).strip()
    return c.lower()


def _command_tokens(normalized: str):
    """将归一化命令拆成 token 列表。"""
    try:
        return shlex.split(normalized, posix=False)
    except Exception:
        return normalized.split()


def check_command_safety(command: str) -> tuple[bool, str]:
    """
    检查命令是否安全。
    返回 (是否安全, 拒绝原因)。
    安全返回 (True, "")，危险返回 (False, 原因)。
    """
    if not command or not command.strip():
        return True, ""

    norm = _normalize_command(command)

    # --- fork bomb ---
    if re.search(r":\s*\(\s*\)\s*\{", norm) or ":(){ :|:& }" in norm:
        return False, "fork bomb 检测到"

    # --- 下载执行链：curl|sh / wget|bash / iwr|iex ---
    if re.search(
        r"(^|\s|[|;&])(curl|wget|iwr|invoke-webrequest|invoke-restmethod)"
        r"[^|;]*\|[^|;]*(sh|bash|zsh|powershell|pwsh|iex|invoke-expression)(\s|$)",
        norm,
    ):
        return False, "检测到下载后执行链 (curl|sh 等)"

    # --- base64 解码后执行 ---
    if re.search(
        r"(base64\s*-d|frombase64string|decodebase64)[^|;]*\|"
        r"[^|;]*(sh|bash|python|perl|powershell)(\s|$)",
        norm,
    ):
        return False, "检测到 base64 解码执行链"

    # --- 编码的 PowerShell 命令 ---
    if re.search(r"powershell\s+(-e|-enc|-\s*encodedcommand)\b", norm):
        return False, "检测到编码的 PowerShell 命令"

    # --- 首命令或任意 token 命中绝对禁止命令 ---
    tokens = _command_tokens(norm)
    for tok in tokens:
        base = tok.rstrip(";,&|()").strip()
        if not base:
            continue
        if base in FORBIDDEN_COMMANDS:
            return False, f"禁止的命令: {base}"
        for prefix in FORBIDDEN_PREFIXES:
            if base.startswith(prefix):
                return False, f"禁止的命令: {base}"

    return True, ""


# ============================================================
# 2) 路径沙箱
# ============================================================
class PathSandbox:
    """路径沙箱：确保文件/目录访问被限制在允许的根目录内。"""

    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def resolve(self, path: str) -> Path:
        """将任意路径解析为沙箱内路径，越界则抛 ValueError。

        修复: 相对路径必须基于沙箱根(root)解析，而不是基于进程 CWD，
        否则 subdir="dirsearch" 会被解析成 CWD/dirsearch 而非 root/dirsearch。
        """
        raw = str(path).strip().strip('"')
        p = Path(raw)
        if p.is_absolute():
            p = p.expanduser()
        else:
            # 相对路径 → 基于沙箱根拼接后再规范化
            p = (self.root / p).resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            raise ValueError(
                f"路径越界: 只允许访问 {self.root} 内的文件，收到 {p}"
            )
        return p

    def contains(self, path: str) -> bool:
        try:
            self.resolve(path)
            return True
        except ValueError:
            return False


# 敏感文件（禁止读写）：密钥/备份等
SENSITIVE_FILENAMES = {
    ".env", ".env_b", ".env.github_template", ".gitignore",
    "cookies.txt",
}


def is_sensitive_file(path: Path) -> bool:
    """判断是否为敏感文件（API 密钥等）。"""
    if path.name in SENSITIVE_FILENAMES:
        return True
    # .git 目录内的任何文件
    if ".git" in path.parts:
        return True
    return False


# ============================================================
# XSS 报告消毒
# ============================================================
def sanitize_html_output(html: str) -> str:
    """
    对将要写入 .html 文件的报告内容做 XSS 消毒：
    破坏可执行标签/事件属性/危险协议，同时保留正常文档结构（h1/table/pre/style 等）。

    用于防止：渗透测试时日志中捕获的 XSS payload
    （如 <script>alert(1)</script> 或 <img src=x onerror=...>）
    被原样嵌入 HTML 报告，导致打开报告时 payload 在本地浏览器执行。
    """
    if not html:
        return html

    # 1) 完整 <script>...</script> 块实体化（显示为文本，不执行）
    html = re.sub(
        r"<\s*script\b[^>]*>.*?<\s*/\s*script\s*>",
        lambda m: m.group(0).replace("<", "&lt;").replace(">", "&gt;"),
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # 2) 孤立的 <script / </script> 开闭标签
    html = re.sub(r"<\s*script\b", "&lt;script", html, flags=re.IGNORECASE)
    html = re.sub(r"<\s*/\s*script\s*>", "&lt;/script&gt;", html, flags=re.IGNORECASE)

    # 2.5) 可加载外部内容/执行的内嵌标签实体化（iframe/object/embed/applet）
    for tag in ("iframe", "object", "embed", "applet"):
        html = re.sub(
            rf"<\s*{tag}\b[^>]*>.*?<\s*/\s*{tag}\s*>",
            lambda m, t=tag: m.group(0).replace("<", "&lt;").replace(">", "&gt;"),
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        html = re.sub(rf"<\s*{tag}\b", f"&lt;{tag}", html, flags=re.IGNORECASE)
        html = re.sub(rf"<\s*/\s*{tag}\s*>", f"&lt;/{tag}&gt;", html, flags=re.IGNORECASE)

    # 3) 事件处理器属性 onXXX= → onXXX_=（属性名无效化，不再被浏览器执行）
    html = re.sub(
        r"\son[a-z]+\s*=",
        lambda m: m.group(0).rstrip("= \t") + "_=",
        html,
        flags=re.IGNORECASE,
    )

    # 4) 危险协议 javascript: / vbscript: / data:text/html
    html = re.sub(r"javascript\s*:", "javascript_:", html, flags=re.IGNORECASE)
    html = re.sub(r"vbscript\s*:", "vbscript_:", html, flags=re.IGNORECASE)
    html = re.sub(r"data\s*:\s*text/html", "data_:text/html", html, flags=re.IGNORECASE)

    return html
