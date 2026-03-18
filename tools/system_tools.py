# tools/system_tools.py (修复版)
import os
from langchain_core.tools import tool
from pydantic import BaseModel, Field

# ========== 文件读取工具 ==========
class FileReadToolInput(BaseModel):
    filename: str = Field(..., description="要读取的文件完整路径，例如：D:/project/autopt/mission.log")

@tool(args_schema=FileReadToolInput)
def file_read_tool(filename: str) -> str:
    """读取文本文件**完整**内容，支持UTF-8编码。"""
    clean_path = filename.replace("\\", "/")
    
    print(f"[SYSTEM TOOL] 📖 正在读取文件: {clean_path}")
    
    if not os.path.exists(clean_path):
        return f"错误：文件 {clean_path} 不存在"
    
    try:
        with open(clean_path, "r", encoding="utf-8", errors="ignore") as file:
            content = file.read()
        DEBUG_THINK_BUG = False 
        if DEBUG_THINK_BUG:
            content = content.replace("<think", "<th_ink").replace("</think>", "</th_ink>")
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
    """将文本内容写入本地文件，支持UTF-8编码。自动创建不存在的目录。"""
    
    # 1. 路径清洗：解决 Windows 反斜杠问题
    clean_path = filename.replace("\\", "/")
    clean_path = os.path.normpath(clean_path)
    
    print(f"[SYSTEM TOOL] ✍️ 正在写入文件: {clean_path} (覆盖模式: {overwrite})")
    
    try:
        # 2. 确保目录存在
        directory = os.path.dirname(clean_path)
        if directory and not os.path.exists(directory):
            print(f"[SYSTEM TOOL] 📂 创建目录: {directory}")
            os.makedirs(directory, exist_ok=True)
        
        # 3. 写入操作
        mode = "w" if overwrite else "a"
        
        # 写入时增加换行符，防止追加时连在一起
        write_content = content
        if not overwrite and not content.startswith("\n"):
             write_content = "\n" + content

        with open(clean_path, mode, encoding="utf-8", newline='') as file:
            file.write(write_content)
            # 只有追加模式才添加分隔符
            if not overwrite:
                file.write("\n=========== 以上是上一轮结果 ===========\n")
                
        return f"✅ 内容写入成功！\n文件路径：{clean_path}"
        
    except Exception as e:
        error_msg = f"❌ 写入失败：{str(e)}"
        print(f"[SYSTEM TOOL] {error_msg}")
        return error_msg
