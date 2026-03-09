# state.py (新建文件)
from typing import TypedDict, Annotated
from langgraph.graph.message import add_messages

class PenTestState(TypedDict):
    """渗透测试全局状态"""
    # 输入参数
    target: str                    # 目标 IP/URL
    mission_history: str           # 全局战况历史
    
    # 中间结果
    strategy: str                  # Strategist 输出的战术
    deputy_requirement: str        # Deputy 输出的技术需求
    operator_command: str          # Operator 生成的命令
    execution_result: str          # Auditor 执行结果
    log_result: str                # Logger 写入结果
    
    # 最终报告
    final_report: str              # Reporter 生成的报告
    final_html: str                # 生成HTML报告
