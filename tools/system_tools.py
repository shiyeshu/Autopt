# tools/system_tools.py (修复版 + 安全沙箱)
import os
from pathlib import Path
from langchain_core.tools import tool
from pydantic import BaseModel, Field

# 安全模块
from security import (
    ensure_utf8_console,
    is_sensitive_file,
    sanitize_html_output,
    file_write_lock,
    PathSandbox,
)
from config import config

ensure_utf8_console()

# 项目根目录沙箱：文件工具只允许访问项目根内，禁止 .env / .git 等敏感文件
PROJECT_SANDBOX = PathSandbox(str(config.PROJECT_ROOT))


# ========== 文件读取工具 ==========
class FileReadToolInput(BaseModel):
    filename: str = Field(..., description="要读取的文件完整路径，例如：D:/project/autopt/mission.log")


@tool(args_schema=FileReadToolInput)
def file_read_tool(filename: str) -> str:
    """读取文本文件**完整**内容，支持UTF-8编码。仅允许项目目录内的文件。"""
    clean_path = filename.replace("\\", "/")

    print(f"[SYSTEM TOOL] 📖 正在读取文件: {clean_path}")

    # --- P0-3: 路径沙箱 + 敏感文件拦截 ---
    try:
        resolved = PROJECT_SANDBOX.resolve(clean_path)
    except ValueError as e:
        return f"错误：{str(e)}"
    if is_sensitive_file(resolved):
        return f"错误：禁止读取敏感文件 {resolved.name}（含 API 密钥）"

    clean_path = str(resolved)

    if not os.path.exists(clean_path):
        return f"错误：文件 {clean_path} 不存在"

    try:
        with open(clean_path, "r", encoding="utf-8", errors="ignore") as file:
            content = file.read()
        # 【关键修复】返回完整内容，不再预览截断！
        preview_info = f"(文件大小: {len(content)} 字符)"
        return f"文件读取成功 {preview_info}\n\n完整内容：\n{content}"

    except Exception as e:
        return f"读取失败：{str(e)}"


# ========== 文件写入工具 (重点修复) ==========
class FileWriterToolInput(BaseModel):
    filename: str = Field(..., description="要写入的文件完整路径")
    content: str = Field(..., description="要写入文件的文本内容")
    overwrite: bool = Field(True, description="是否覆盖原有内容：True=覆盖，False=追加")


@tool(args_schema=FileWriterToolInput)
def file_write_tool(filename: str, content: str, overwrite: bool = True) -> str:
    """将文本内容写入本地文件，支持UTF-8编码。自动创建不存在的目录。仅允许项目目录内。"""

    # 1. 路径清洗：解决 Windows 反斜杠问题
    clean_path = filename.replace("\\", "/")

    # --- P0-3: 路径沙箱 + 敏感文件拦截 ---
    try:
        resolved = PROJECT_SANDBOX.resolve(clean_path)
    except ValueError as e:
        return f"错误：{str(e)}"
    if is_sensitive_file(resolved):
        return f"错误：禁止写入敏感文件 {resolved.name}（含 API 密钥）"

    clean_path = str(resolved)

    print(f"[SYSTEM TOOL] ✍️ 正在写入文件: {clean_path} (覆盖模式: {overwrite})")

    try:
        # 2. 确保目录存在
        directory = os.path.dirname(clean_path)
        if directory and not os.path.exists(directory):
            print(f"[SYSTEM TOOL] 📂 创建目录: {directory}")
            os.makedirs(directory, exist_ok=True)

        # 3. 写入操作
        mode = "w" if overwrite else "a"

        # 问题3修复：写入 .html 报告时做 XSS 消毒，
        # 防止日志中的 payload（<script>/onerror 等）在打开报告时执行
        write_content = content
        if clean_path.lower().endswith((".html", ".htm")):
            write_content = sanitize_html_output(content)

        # 写入时增加换行符，防止追加时连在一起
        if not overwrite and not write_content.startswith("\n"):
            write_content = "\n" + write_content

        # --- P1-8: 文件写入加锁 ---
        with file_write_lock:
            with open(clean_path, mode, encoding="utf-8", newline="") as file:
                file.write(write_content)
                # 只有追加模式才添加分隔符
                if not overwrite:
                    file.write("\n=========== 以上是上一轮结果 ===========\n")

        return f"✅ 内容写入成功！\n文件路径：{clean_path}"

    except Exception as e:
        error_msg = f"❌ 写入失败：{str(e)}"
        print(f"[SYSTEM TOOL] {error_msg}")
        return error_msg
