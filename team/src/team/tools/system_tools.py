# tools/builtin_tools.py
from crewai_tools import FileReadTool, FileWriterTool
import os
from typing import Optional
from pydantic import BaseModel, Field  # 新增：导入Pydantic模型

# ========== 1. 定义读取工具的入参模型 ==========
class EncodedFileReadToolInput(BaseModel):
    filename: str = Field(
        ...,  # 必填
        description="要读取的文件完整路径，例如：D:/project/autopt/mission.log"
    )

# ========== 2. 带编码的读取工具（补全args_schema） ==========
class EncodedFileReadTool(FileReadTool):
    encoding: str = "utf-8"
    name: str = "Encoded File Read Tool"
    description: str = "当需要将文本内容写入本地文件时调用此工具，入参为filename（文件完整路径）"
    args_schema: BaseModel = EncodedFileReadToolInput  # 绑定入参模型

    def _run(self, filename: str, **kwargs) -> str:
        try:
            with open(filename, "r", encoding=self.encoding, errors="ignore") as file:
                content = file.read()
            return f"文件读取成功（编码：{self.encoding}）\n内容：\n{content}"
        except FileNotFoundError:
            return f"错误：文件 {filename} 不存在"
        except Exception as e:
            return f"读取失败：{str(e)}"

# ========== 3. 定义写入工具的入参模型 ==========
class EncodedFileWriterToolInput(BaseModel):
    filename: str = Field(
        ...,
        description="要写入的文件完整路径，例如：D:/project/autopt/mission.log"
    )
    content: str = Field(
        ...,
        description="要写入文件的文本内容，支持中文"
    )
    directory: Optional[str] = Field(
        None,
        description="文件所在目录（若filename已包含完整路径则无需填写）"
    )
    overwrite: bool = Field(
        True,
        description="是否覆盖原有内容：True=覆盖，False=追加"
    )

# ========== 4. 带编码的写入工具（补全args_schema） ==========
class EncodedFileWriterTool(FileWriterTool):
    encoding: str = "utf-8"
    name: str = "Encoded File Writer Tool"
    description: str = "当需要将文本内容写入本地文件时调用此工具，必须传入filename（文件完整路径）和content（写入内容），支持UTF-8编码，返回写入结果"
    args_schema: BaseModel = EncodedFileWriterToolInput  # 绑定入参模型

    def _run(
        self,
        filename: str,
        content: str,
        directory: Optional[str] = None,
        overwrite: bool = True,
        **kwargs
    ) -> str:
        # 拼接完整路径
        if directory:
            full_path = os.path.join(directory, filename)
        else:
            full_path = filename
        full_path = os.path.normpath(full_path)

        # 确保目录存在
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        try:
            mode = "w" if overwrite else "a"
            with open(full_path, mode, encoding=self.encoding, newline='') as file:

                file.write(content)
                file.write("\n===========这一轮次结果如上===========\n")
            return f"内容写入成功（编码：{self.encoding}）\n文件路径：{full_path}"
        except Exception as e:
            return f"写入失败：{str(e)}"

# ========== 实例化工具 ==========
file_read_tool = EncodedFileReadTool(encoding="utf-8")
file_write_tool = EncodedFileWriterTool(encoding="utf-8")