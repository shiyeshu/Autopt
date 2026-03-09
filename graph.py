# graph.py (新建文件)
from langgraph.graph import StateGraph, END
from state import PenTestState
from agents import (
    strategist_node,
    deputy_node,
    operator_node,
    auditor_node,
    reporter_node,
    html_reporter_node
)

# 创建 Assault Crew Graph
def create_assault_graph():
    workflow = StateGraph(PenTestState)
    
    # 添加节点
    workflow.add_node("strategist", strategist_node)
    workflow.add_node("deputy", deputy_node)
    workflow.add_node("operator", operator_node)
    workflow.add_node("auditor", auditor_node)
    
    # 定义边（顺序执行）
    workflow.set_entry_point("strategist")
    workflow.add_edge("strategist", "deputy")
    workflow.add_edge("deputy", "operator")
    workflow.add_edge("operator", "auditor")
    workflow.add_edge("auditor", END)
    
    return workflow.compile()

# 创建 Reporting Crew Graph
def create_reporting_graph():
    workflow = StateGraph(PenTestState)
    
    workflow.add_node("reporter", reporter_node)
    workflow.add_node("html_reporter", html_reporter_node)

    workflow.set_entry_point("reporter")
    
    workflow.add_edge("reporter", "html_reporter")
    workflow.add_edge("html_reporter", END)
    

    return workflow.compile()
