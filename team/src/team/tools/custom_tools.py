# tools/custom_tools.py
from pydantic import BaseModel, Field
# 修正1：基类路径错误 → 从 crewai_tools 导入（而非 crewai）
from crewai.tools import BaseTool 
import subprocess
import time
import os
import re

toolspath = "D:/project/autopt/aptools/"

# ========== 执行器工具（无问题，保留） ==========
class ExecutionToolInput(BaseModel):
    cmd : str = Field(...,description="要执行的命令",)

class ExecutionTool(BaseTool):
    name: str = "执行器"
    description: str = "用于执行系统shell命令，返回执行结果。参数cmd为要执行的命令字符串。"
    args_schema: BaseModel = ExecutionToolInput

    def _run(self, cmd: str) -> str:
        """执行 Shell 命令并返回结果。参数名应与 `ExecutionToolInput` 字段 `cmd` 一致。"""
        command = cmd
        print(f"\n[SYSTEM TOOL] ⚡ 正在执行: {command}")

        # 1. 黑名单检查
        forbidden = ["rm -rf", "mkfs", "shutdown", "format"]
        if any(f in command for f in forbidden):
            return "SECURITY ALERT: Command blocked."

        try:
            # 2. 执行命令
            start_time = time.time()
            result = subprocess.run(
                command, 
                shell=True, 
                capture_output=True, 
                text=True, 
                timeout=60,
                encoding='gbk', 
                errors='replace'
            )
            duration = time.time() - start_time

            # 3. 构造输出
            output_block = f"CMD: {command}\n"
            output_block += f"Time: {duration:.2f}s\n"
            
            # 4. 防幻觉检查 (空输出拦截)
            has_output = False
            if result.stdout and result.stdout.strip():
                output_block += f"STDOUT:\n{result.stdout}\n"
                has_output = True
            if result.stderr and result.stderr.strip():
                output_block += f"STDERR:\n{result.stderr}\n"
                has_output = True
            
            if not has_output:
                output_block += "RESULT: [NO OUTPUT] (Command executed but returned nothing)\n"
                print("[SYSTEM TOOL] ⚠️ 命令无输出")
            
            if not has_output:
                return "SYSTEM WARNING: Command executed successfully but returned NO OUTPUT."
            #将结果写入日志
            print(f"[SYSTEM TOOL] ✅ 命令执行完成，耗时 {duration:.2f}s")
            #用正则过滤删除“You ONLY have”开头之后的所有内容
            output_block = re.sub(r'^You ONLY have.*', '', output_block, flags=re.MULTILINE)



            return output_block

        except subprocess.TimeoutExpired:
            return "SYSTEM ERROR: Command timed out (60s)."
        except Exception as e:
            return f"SYSTEM ERROR: Execution failed: {str(e)}"

# ========== 工具查看器工具（核心修复） ==========
class ListCustomToolInput(BaseModel):
    # 修正2：参数名与 _run 方法一致，且描述清晰
    subdir : str = Field(default=toolspath, description="子目录名称，不填则列出工具根目录")

class ListCustomTool(BaseTool):
    name: str = "工具查看器"
    description: str = "用于列出指定目录下的内容，返回文件和子目录列表。只接受一个参数subdir，值为子目录名称。不填则为空，列出工具根目录。"
    # 修正3：绑定入参模型
    args_schema: BaseModel = ListCustomToolInput
    

    # 修正4：_run 方法必须带 self，参数名与 args_schema 一致
    def _run(self, subdir: str = "") -> str:  
        """
        列出工具目录下的文件。
        Args:
            subdir (str): 子目录名称。如果不填则列出根目录。
        """
        # --- 1. 参数清洗 ---
        if subdir is None:
            subdir = ""
        clean_subdir = str(subdir).strip(" /\\\"'") 
        print(f"\n[SYSTEM TOOL] 🔍 请求列出目录: '{clean_subdir}'")

        # --- 2. 路径构建 ---
        base_path = toolspath
        target_path = os.path.join(base_path, clean_subdir)
        target_path = os.path.normpath(target_path)
        print(f"[SYSTEM TOOL] 📂 尝试访问绝对路径: {target_path}")

        # --- 3. 存在性检查 ---
        if not os.path.exists(target_path):
            msg = f"SYSTEM ERROR: 目录不存在: {target_path} (请检查 TOOLS_ROOT_DIR 配置)"
            print(f"[SYSTEM TOOL] ❌ {msg}")
            return msg
            
        if not os.path.isdir(target_path):
            msg = f"SYSTEM ERROR: 这是一个文件，不是目录: {target_path}"
            print(f"[SYSTEM TOOL] ❌ {msg}")
            return msg

        # --- 4. 获取文件列表 ---
        try:
            items = os.listdir(target_path)
            
            if not items:
                print(f"[SYSTEM TOOL] ⚠️ 目录是空的")
                return f"SYSTEM NOTICE: 目录 '{target_path}' 存在，但是里面是空的。"

            result_lines = [f"Found {len(items)} items in {target_path}:"]
            for item in items:
                full_path = os.path.join(target_path, item)
                if os.path.isdir(full_path):
                    result_lines.append(f"[DIR]  {full_path}")
                else:
                    result_lines.append(f"[FILE] {full_path}")

            output = "\n".join(result_lines)
            print(f"[SYSTEM TOOL] ✅ 成功列出文件。")
            return output

        except Exception as e:
            err = f"SYSTEM ERROR: 读取目录失败: {str(e)}"
            print(f"[SYSTEM TOOL] ❌ {err}")
            return err

# ========== 实例化工具（核心修复：避免重复赋值） ==========
executor_tool = ExecutionTool()       # 执行器工具实例
list_custom_tool = ListCustomTool()   # 工具查看器工具实例（单独命名，不覆盖）