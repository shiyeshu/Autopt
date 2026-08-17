# tools/custom_tools.py (LangGraph 版本, 安全加固)
from langchain_core.tools import tool, StructuredTool
from pydantic import BaseModel, Field
import subprocess
import time
import os
import re
import sys
from pathlib import Path
from dotenv import load_dotenv

# 安全模块
from security import (
    ensure_utf8_console,
    check_command_safety,
    mission_control,
    file_write_lock,
    PathSandbox,
)

ensure_utf8_console()

# ========== 1) 路径与环境变量 ==========
THIS_FILE = Path(__file__).resolve()
TEAM_DIR = THIS_FILE.parent.parent  # D:\project\autopt (项目根)
PROJECT_ROOT = TEAM_DIR

# 确保能 import project_root 下的模块（如 config.py），以及 team 下的 graph.py/tools/
sys.path.insert(0, str(TEAM_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

# 加载 .env（位于项目根目录）
from config import config

toolspath = config.TOOLS_ROOT_DIR
# 工具沙箱：限制 list_custom_tool 只能访问 aptools 内部
tools_sandbox = PathSandbox(toolspath)


# ========== 执行器工具 ==========
class ExecutionToolInput(BaseModel):
    cmd: str = Field(..., description="要执行的命令字符串")


@tool(args_schema=ExecutionToolInput)
def execution_tool(cmd: str) -> str:
    """执行 shell 命令并返回输出，同时把结果写入 mission.log。"""
    command = cmd.replace("`", "").strip()

    print(f"[SYSTEM TOOL] Received command: {command}")

    # --- P0-2: 黑名单加固（防绕过） ---
    safe, reason = check_command_safety(command)
    if not safe:
        print(f"[SECURITY ALERT] Blocked command: {command} | reason: {reason}")
        return f"SECURITY ALERT: Command blocked due to security policy. ({reason})"

    # --- 架构4: 停止检查 ---
    if mission_control.stopped:
        return "SYSTEM: 任务已被用户停止，命令未执行。"

    try:
        start_time = time.time()

        # 使用 Popen 以便支持停止按钮终止进程
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        mission_control.register_proc(proc)

        try:
            stdout_text, stderr_text = proc.communicate(timeout=500)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_text, stderr_text = proc.communicate()
            return f"执行超时(>500s)，已强制终止。\n{stdout_text.decode('utf-8', errors='replace')[:2000]}"
        finally:
            mission_control.unregister_proc(proc)

        duration = time.time() - start_time

        stdout_text = stdout_text.decode("utf-8", errors="replace")
        stderr_text = stderr_text.decode("utf-8", errors="replace")

        # 【已修复/默认关闭】DEBUG_THINK_BUG: 曾把工具输出中的 <think>/</think>
        # 替换为 <th_ink>/</th_ink>，以规避某些模型把工具输出误认为思考标签。
        # 根因是早期 prompt 模拟过 <think> 标签；现 prompt 已不再模拟，
        # 且 reasoning_content 已单独提取，故默认关闭以保持工具输出真实（渗透证据保真）。
        # 如仍需防御目标站点 HTML 中的 <think> 字符串，可设 DEBUG_THINK_BUG=true 开启。
        if os.getenv("DEBUG_THINK_BUG", "false").lower() == "true":
            stdout_text = stdout_text.replace("<think", "<th_ink").replace("</think>", "</th_ink>")
            stderr_text = stderr_text.replace("<think", "<th_ink").replace("</think>", "</th_ink>")

        # --- P1-6: 日志限长（防止 mission.log 无限膨胀 & 上下文爆掉） ---
        MAX_LOG = 50000
        stdout_log = stdout_text[-MAX_LOG:] if len(stdout_text) > MAX_LOG else stdout_text
        stderr_log = stderr_text[-MAX_LOG:] if len(stderr_text) > MAX_LOG else stderr_text
        truncated = len(stdout_text) > MAX_LOG or len(stderr_text) > MAX_LOG

        log_entry = (
            f"\n[EXECUTION TOOL]\n"
            f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Command: {command}\n"
            f"Duration: {duration:.2f}s\n"
            f"STDOUT:\n{stdout_log}\n"
            f"STDERR:\n{stderr_log}\n"
            + ("[输出过长已截断]\n" if truncated else "")
            + "=" * 80
            + "\n"
        )

        # --- P1-8: 文件写入加锁 ---
        log_path = config.LOG_FILE_PATH
        with file_write_lock:
            os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as logfile:
                logfile.write(log_entry)
                logfile.flush()

        # --- P1-6: 返回给 LLM 的输出也限长 ---
        MAX_RETURN = 20000
        if stdout_text.strip():
            out = f"执行完成 ({duration:.1f}s)\n{stdout_text}"
        elif stderr_text.strip():
            out = f"执行完成 ({duration:.1f}s)\n{stderr_text}"
        else:
            out = f"执行完成 ({duration:.1f}s) - 无标准输出"
        if len(out) > MAX_RETURN:
            out = out[:MAX_RETURN] + "\n...[输出过长已截断]"
        return out

    except Exception as e:
        log_path = config.LOG_FILE_PATH
        with file_write_lock:
            os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as logfile:
                logfile.write(f"[ERROR] Command failed: {command}\n{str(e)}\n{'=' * 80}\n")
        return f"执行失败: {str(e)}"


# ========== 工具查看器 ==========
class ListCustomToolInput(BaseModel):
    subdir: str = Field(default="", description="子目录名称，不填则列出工具根目录")


@tool(args_schema=ListCustomToolInput)
def list_custom_tool(subdir: str = "") -> str:
    """用于列出指定目录下的内容，返回文件和子目录列表。"""
    if subdir is None:
        subdir = ""
    clean_subdir = str(subdir).strip(" /\\\"\\'")
    print(f"\n[SYSTEM TOOL] 🔍 请求列出目录: '{clean_subdir}'")

    base_path = toolspath

    # --- P1-5: 路径穿越修复：用沙箱解析，越界直接拒绝 ---
    try:
        if clean_subdir:
            target_path = tools_sandbox.resolve(clean_subdir)
        else:
            target_path = Path(base_path).resolve()
    except ValueError as e:
        msg = f"SYSTEM ERROR: {str(e)}"
        print(f"[SYSTEM TOOL] ❌ {msg}")
        return msg

    target_path = str(target_path)
    print(f"[SYSTEM TOOL] 📂 尝试访问绝对路径: {target_path}")

    if not os.path.exists(target_path):
        msg = f"SYSTEM ERROR: 目录不存在: {target_path}"
        print(f"[SYSTEM TOOL] ❌ {msg}")
        return msg

    if not os.path.isdir(target_path):
        msg = f"SYSTEM ERROR: 这是一个文件，不是目录: {target_path}"
        print(f"[SYSTEM TOOL] ❌ {msg}")
        return msg

    try:
        items = os.listdir(target_path)
        if not items:
            print(f"[SYSTEM TOOL] ⚠️ 目录是空的")
            return f"SYSTEM NOTICE: 目录 '{target_path}' 存在，但是里面是空的。"

        result_lines = [f"Found {len(items)} items in {target_path}:"]
        for item in items:
            full_path = os.path.join(target_path, item)
            full_path = full_path.replace("\\", "/")
            if os.path.isdir(full_path):
                result_lines.append(f"[DIR] {full_path}")
            else:
                result_lines.append(f"[FILE] {full_path}")

        output = "\n".join(result_lines)
        print(f"[SYSTEM TOOL] ✅ 成功列出文件。")
        return output

    except Exception as e:
        err = f"SYSTEM ERROR: 读取目录失败: {str(e)}"
        print(f"[SYSTEM TOOL] ❌ {err}")
        return err
