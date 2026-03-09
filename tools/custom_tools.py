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
    """用于执行系统shell命令，返回执行结果。"""
    
    command = cmd.replace("\\", "/") 
    print(f"\n[SYSTEM TOOL] ⚡ 正在执行: {command}")
    
    # 黑名单检查
    forbidden = ["rm -rf", "mkfs", "shutdown", "format"]
    if any(f in command for f in forbidden):
        return "SECURITY ALERT: Command blocked."
    
    try:
        start_time = time.time()
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=500, encoding='gbk', errors='replace')
        
        duration = time.time() - start_time
        
        # 写完整日志
        log_entry = f"=== [{time.strftime('%Y-%m-%d %H:%M:%S')}] {command} ===\nTime: {duration:.2f}s\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}\n{'='*80}\n"
        
        log_path = os.getenv("LOG_FILE_PATH", "mission.log")
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(log_entry)
            log_file.flush()
        
        # 【关键】：返回极简信息，不触发前端日志
        return f"执行完成 ({duration:.1f}s)"
        
    except Exception as e:
        # 错误也只写文件
        log_path = os.getenv("LOG_FILE_PATH", "mission.log")
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"ERROR: {command}\n{str(e)}\n{'='*80}\n")
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
