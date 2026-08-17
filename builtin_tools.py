# builtin_tools.py - 内置常用网络工具
# 提供: HTTP GET / HTTP POST / DNS 解析 / TCP 端口探测 / WHOIS 查询
# 全部使用 Python 标准库实现（urllib / socket / subprocess），不依赖第三方库。
# 每个工具都: 中文 docstring、print 日志、异常捕获返回错误信息（不抛异常）。
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

from langchain_core.tools import tool
from pydantic import BaseModel, Field

# ============================================================
# UTF-8 控制台修复（Windows GBK 下中文/emoji print 崩溃，参照 security.py）
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
# 常量与公共辅助
# ============================================================
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AutoPT/1.0 (Security Testing Tool)"
)
BODY_LIMIT = 5000          # HTTP 响应正文返回上限
WHOIS_OUTPUT_LIMIT = 8000  # WHOIS 输出返回上限
WHOIS_TIMEOUT = 30         # whois 命令超时秒数


def _parse_headers(headers: str) -> dict:
    """把 "Key: Value\\nKey2: Value2" 格式的字符串解析为 dict。"""
    out = {}
    if not headers:
        return out
    for line in str(headers).splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, _, v = line.partition(":")
        if k.strip():
            out[k.strip()] = v.strip()
    return out


def _decode_body(raw: bytes) -> str:
    """按常见编码顺序解码响应正文，兜底用 replace。"""
    if not raw:
        return ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _format_http_response(url, status, reason, headers: dict, body: str) -> str:
    """把 HTTP 响应整理为文本: 状态码 + 响应头 + 正文(前 BODY_LIMIT 字符)。"""
    lines = [f"[HTTP] {url}", f"状态码: {status} {reason}", "响应头:"]
    for k, v in (headers or {}).items():
        lines.append(f"  {k}: {v}")
    if body is None:
        lines.append("（无响应正文）")
    else:
        truncated = len(body) > BODY_LIMIT
        lines.append(f"响应正文 (前 {BODY_LIMIT} 字符{'，已截断' if truncated else ''}):")
        lines.append(body[:BODY_LIMIT])
    return "\n".join(lines)


# ============================================================
# 1) HTTP GET
# ============================================================
class HttpGetToolInput(BaseModel):
    url: str = Field(..., description="要请求的 URL 地址，支持 http:// 与 https://，例如 https://example.com")
    timeout: int = Field(30, description="请求超时时间（秒），默认 30")
    headers: str = Field("", description='可选的自定义请求头，一行一个 "Key: Value"，例如 "User-Agent: test\\nX-Custom: 1"')


@tool(args_schema=HttpGetToolInput)
def http_get_tool(url: str, timeout: int = 30, headers: str = "") -> str:
    """发送 HTTP GET 请求，返回状态码、响应头与响应正文（正文最多前 5000 字符），用于信息收集与漏洞验证。"""
    print(f"[BUILTIN TOOL] 🔗 HTTP GET -> {url}")
    try:
        timeout = max(1, int(timeout))
    except Exception:
        timeout = 30

    extra = _parse_headers(headers)
    extra.setdefault("User-Agent", DEFAULT_USER_AGENT)
    try:
        req = urllib.request.Request(url, headers=extra, method="GET")
    except ValueError as e:
        return f"[HTTP GET 失败] URL 格式错误: {e}"

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = _decode_body(resp.read())
            return _format_http_response(url, resp.status, resp.reason, dict(resp.headers.items()), body)
    except urllib.error.HTTPError as e:
        # 4xx/5xx 也返回状态码与响应头，方便观察
        body = _decode_body(e.read()) if e.fp else ""
        hdrs = dict(e.headers.items()) if e.headers else {}
        return _format_http_response(url, e.code, e.reason, hdrs, body)
    except urllib.error.URLError as e:
        return f"[HTTP GET 失败] {url}\n网络错误: {e.reason}"
    except ssl.SSLError as e:
        return f"[HTTP GET 失败] {url}\nSSL 错误: {e}"
    except socket.timeout:
        return f"[HTTP GET 失败] {url}\n请求超时（>{timeout}s）"
    except Exception as e:
        return f"[HTTP GET 失败] {url}\n异常: {type(e).__name__}: {e}"


# ============================================================
# 2) HTTP POST
# ============================================================
class HttpPostToolInput(BaseModel):
    url: str = Field(..., description="要请求的 URL 地址，支持 http:// 与 https://，例如 https://example.com/login")
    data: str = Field("", description="请求体字符串（POST 数据），例如 'username=admin&password=123'")
    content_type: str = Field("application/x-www-form-urlencoded", description="请求体 Content-Type，默认 application/x-www-form-urlencoded")
    timeout: int = Field(30, description="请求超时时间（秒），默认 30")
    headers: str = Field("", description='可选的自定义请求头，一行一个 "Key: Value"')


@tool(args_schema=HttpPostToolInput)
def http_post_tool(url: str, data: str = "", content_type: str = "application/x-www-form-urlencoded",
                   timeout: int = 30, headers: str = "") -> str:
    """发送 HTTP POST 请求，data 为请求体文本，返回状态码、响应头与响应正文（最多前 5000 字符），用于表单提交与接口测试。"""
    print(f"[BUILTIN TOOL] 📤 HTTP POST -> {url}")
    try:
        timeout = max(1, int(timeout))
    except Exception:
        timeout = 30

    extra = _parse_headers(headers)
    extra.setdefault("User-Agent", DEFAULT_USER_AGENT)
    extra.setdefault("Content-Type", content_type or "application/x-www-form-urlencoded")
    try:
        body_bytes = str(data or "").encode("utf-8")
        req = urllib.request.Request(url, data=body_bytes, headers=extra, method="POST")
    except ValueError as e:
        return f"[HTTP POST 失败] URL 格式错误: {e}"

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = _decode_body(resp.read())
            return _format_http_response(url, resp.status, resp.reason, dict(resp.headers.items()), body)
    except urllib.error.HTTPError as e:
        body = _decode_body(e.read()) if e.fp else ""
        hdrs = dict(e.headers.items()) if e.headers else {}
        return _format_http_response(url, e.code, e.reason, hdrs, body)
    except urllib.error.URLError as e:
        return f"[HTTP POST 失败] {url}\n网络错误: {e.reason}"
    except ssl.SSLError as e:
        return f"[HTTP POST 失败] {url}\nSSL 错误: {e}"
    except socket.timeout:
        return f"[HTTP POST 失败] {url}\n请求超时（>{timeout}s）"
    except Exception as e:
        return f"[HTTP POST 失败] {url}\n异常: {type(e).__name__}: {e}"


# ============================================================
# 3) DNS 解析
# ============================================================
class DnsLookupToolInput(BaseModel):
    hostname: str = Field(..., description="要解析的域名或主机名，例如 example.com")


@tool(args_schema=DnsLookupToolInput)
def dns_lookup_tool(hostname: str) -> str:
    """DNS 解析：使用系统 DNS 查询域名对应的所有 IPv4/IPv6 地址（socket.getaddrinfo），用于确认目标真实 IP。"""
    hostname = str(hostname).strip()
    print(f"[BUILTIN TOOL] 🌐 DNS lookup -> {hostname}")

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        return f"[DNS 解析失败] {hostname}\n错误: {e}"
    except Exception as e:
        return f"[DNS 解析失败] {hostname}\n异常: {type(e).__name__}: {e}"

    v4, v6 = [], []
    for family, _socktype, _proto, _canon, sockaddr in infos:
        addr = sockaddr[0]
        if family == socket.AF_INET and addr not in v4:
            v4.append(addr)
        elif family == socket.AF_INET6 and addr not in v6:
            v6.append(addr)

    lines = [f"[DNS 解析] {hostname}"]
    lines.append("IPv4 ({} 个): {}".format(len(v4), ", ".join(v4) if v4 else "无"))
    lines.append("IPv6 ({} 个): {}".format(len(v6), ", ".join(v6) if v6 else "无"))
    if not v4 and not v6:
        lines.append("提示: 未解析到任何地址（域名不存在或 DNS 无记录）")
    return "\n".join(lines)


# ============================================================
# 4) TCP 端口探测
# ============================================================
class PortProbeToolInput(BaseModel):
    host: str = Field(..., description="目标主机 IP 或域名，例如 127.0.0.1 或 example.com")
    port: int = Field(..., description="要探测的 TCP 端口号（1-65535），例如 80")
    timeout: int = Field(3, description="连接超时时间（秒），默认 3")


@tool(args_schema=PortProbeToolInput)
def port_probe_tool(host: str, port: int, timeout: int = 3) -> str:
    """TCP 端口探测：尝试与目标主机指定端口建立 TCP 连接，返回 open/closed/filtered 状态及服务名（尽力而为）。"""
    try:
        port = int(port)
    except Exception:
        return f"[端口探测失败] 端口号无效: {port}"
    if not (1 <= port <= 65535):
        return f"[端口探测失败] 端口号越界: {port}（应为 1-65535）"
    try:
        timeout = max(1, int(timeout))
    except Exception:
        timeout = 3
    host = str(host).strip()
    print(f"[BUILTIN TOOL] 🔍 Port probe -> {host}:{port} (timeout={timeout}s)")

    service = "unknown"
    try:
        service = socket.getservbyport(port, "tcp")
    except Exception:
        pass

    try:
        with socket.create_connection((host, port), timeout=timeout):
            state, detail = "open", "TCP 连接成功"
    except socket.timeout:
        state, detail = "filtered", "连接超时（可能被防火墙过滤或主机不可达）"
    except ConnectionRefusedError:
        state, detail = "closed", "连接被拒绝（端口未监听）"
    except OSError as e:
        if getattr(e, "errno", None) == socket.errno.ECONNREFUSED:
            state, detail = "closed", "连接被拒绝（端口未监听）"
        else:
            state, detail = "filtered", f"{type(e).__name__}: {e}"
    except Exception as e:
        state, detail = "filtered", f"{type(e).__name__}: {e}"

    return f"[端口探测] {host}:{port} (tcp/{service}) 状态: {state}（{detail}）"


# ============================================================
# 5) WHOIS 查询
# ============================================================
class WhoisLookupToolInput(BaseModel):
    domain: str = Field(..., description="要查询 WHOIS 信息的域名，例如 example.com")


@tool(args_schema=WhoisLookupToolInput)
def whois_lookup_tool(domain: str) -> str:
    """WHOIS 查询：优先调用系统 whois 命令查询域名注册信息；系统未安装 whois 时返回友好提示。"""
    domain = str(domain).strip().lower()
    print(f"[BUILTIN TOOL] 📇 WHOIS lookup -> {domain}")

    try:
        proc = subprocess.run(
            ["whois", domain],
            capture_output=True,
            timeout=WHOIS_TIMEOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return (
            f"[WHOIS 查询失败] 系统未安装 whois 命令。\n"
            f"请先安装 whois（Linux: apt install whois；macOS: brew install whois），"
            f"或改用在线 WHOIS 服务查询 {domain}。"
        )
    except subprocess.TimeoutExpired:
        return f"[WHOIS 查询失败] whois 命令执行超时（{WHOIS_TIMEOUT}s）"
    except Exception as e:
        return f"[WHOIS 查询失败] 调用 whois 命令出错: {type(e).__name__}: {e}"

    output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if not output:
        return f"[WHOIS 查询] {domain} 无返回结果"
    if len(output) > WHOIS_OUTPUT_LIMIT:
        output = output[:WHOIS_OUTPUT_LIMIT] + "\n...[输出过长已截断]"
    return f"[WHOIS 查询] {domain}\n{output}"


# 内置工具列表（方便一次性注册/绑定）
BUILTIN_TOOLS = [
    http_get_tool,
    http_post_tool,
    dns_lookup_tool,
    port_probe_tool,
    whois_lookup_tool,
]
