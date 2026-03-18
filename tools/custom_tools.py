# tools/custom_tools.py (LangGraph 版本)
from langchain_core.tools import tool, StructuredTool
from pydantic import BaseModel, Field
import subprocess
import time
import os
import re
import sys
from pathlib import Path
from dotenv import load_dotenv

# ========== 1) 路径与环境变量 ==========
THIS_FILE = Path(__file__).resolve()
TEAM_DIR = THIS_FILE.parent
PROJECT_ROOT = TEAM_DIR.parent

# 确保能 import project_root 下的模块（如 config.py），以及 team 下的 graph.py/tools/
sys.path.insert(0, str(TEAM_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

# 加载 .env（位于项目根目录）
env_path = PROJECT_ROOT / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)
toolspath = os.getenv("TOOLS_ROOT_DIR", "./aptools/")


# ========== 执行器工具 ==========
class ExecutionToolInput(BaseModel):
    cmd: str = Field(..., description="要执行的命令字符串")

@tool(args_schema=ExecutionToolInput)
def execution_tool(cmd: str) -> str:
    """执行 shell 命令并返回输出，同时把结果写入 mission.log。"""
    command = cmd.replace("`", "").strip()

    print(f"[SYSTEM TOOL] Received command: {command}")

    forbidden_patterns = [
        r"rm\s+-rf",
        r"del\s+/",
        r"shutdown",
        r"mkfs",
        r":[\s]*\(\)\s*{",
    ]
    command_lower = command.lower()

    for pattern in forbidden_patterns:
        if re.search(pattern, command_lower, re.IGNORECASE):
            print(f"[SECURITY ALERT] Blocked command: {command}")
            return "SECURITY ALERT: Command blocked due to security policy."

    try:
        start_time = time.time()

        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            timeout=500,
        )

        duration = time.time() - start_time

        stdout_text = result.stdout.decode("utf-8", errors="replace")
        stderr_text = result.stderr.decode("utf-8", errors="replace")

        DEBUG_THINK_BUG = os.getenv("DEBUG_THINK_BUG", "true").lower() == "true"
        if DEBUG_THINK_BUG:
            stdout_text = stdout_text.replace("<think", "<th_ink").replace("</think>", "</th_ink>")
            stderr_text = stderr_text.replace("<think", "<th_ink").replace("</think>", "</th_ink>")

        log_entry = (
            f"\n[EXECUTION TOOL]\n"
            f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Command: {command}\n"
            f"Duration: {duration:.2f}s\n"
            f"STDOUT:\n{stdout_text}\n"
            f"STDERR:\n{stderr_text}\n"
            + "=" * 80
            + "\n"
        )

        log_path = os.getenv("LOG_FILE_PATH", "mission.log")
        with open(log_path, "a", encoding="utf-8") as logfile:
            logfile.write(log_entry)
            logfile.flush()

        if stdout_text.strip():
            return f"执行完成 ({duration:.1f}s)\n{stdout_text}"
        elif stderr_text.strip():
            return f"执行完成 ({duration:.1f}s)\n{stderr_text}"
        else:
            return f"执行完成 ({duration:.1f}s) - 无标准输出"

    except Exception as e:
        log_path = os.getenv("LOG_FILE_PATH", "mission.log")
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
    target_path = os.path.join(base_path, clean_subdir)
    target_path = os.path.normpath(target_path)
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
