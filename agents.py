# team/agents.py
import time
import json
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from langchain_openai import ChatOpenAI
from state import PenTestState
from config import config
import yaml
import os
import sys

# 添加项目根目录到 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.system_tools import file_read_tool, file_write_tool
from tools.custom_tools import execution_tool, list_custom_tool

# 加载配置
with open("config/agents.yaml", "r", encoding="utf-8") as f:
    agents_config = yaml.safe_load(f)
with open("config/tasks.yaml", "r", encoding="utf-8") as f:
    tasks_config = yaml.safe_load(f)

# ========== 1. 强制思考指令 (CoT) ==========
COT_INSTRUCTION = """
IMPORTANT:
1. You represent the internal thought process of the AI.
2. Before calling ANY tool, you MUST explain your reasoning starting with "Thought: ...".
3. If you decide to call a tool, generate the tool call immediately after the thought.
"""

# ========== 2. LLM 创建 ==========
def create_llm(role: str, temp: float = 0.1):
    api_key = config.get_agent_api_key(role)
    model = config.get_agent_model(role)
    base_url = config.get_agent_base_url(role)
    
    if not api_key:
        raise ValueError(f"Missing API key for {role}")
    
    return ChatOpenAI(
        model=model,
        temperature=temp,
        openai_api_key=api_key,
        openai_api_base=base_url
    )

# ========== 3. 通用 Agent 执行逻辑 (替代 create_react_agent) ==========
def execute_agent_logic(role_name, tools, system_text, user_text, max_steps=5):
    # 【关键】：禁用 print，只返回结果
    # print(f"[{role_name}] 🚀 开始执行...")  # 注释掉
    
    llm = create_llm(role_name, 0.05)
    
    if tools:
        llm_with_tools = llm.bind_tools(tools)
        tool_map = {t.name: t for t in tools}
    else:
        llm_with_tools = llm
        tool_map = {}

    messages = [SystemMessage(content=system_text), HumanMessage(content=user_text)]

    for step in range(max_steps):
        try:
            response = llm_with_tools.invoke(messages)
            messages.append(response)

            if not response.tool_calls:
                return response.content

            for tool_call in response.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                tool_id = tool_call["id"]

                # 【关键】：工具执行不打印，只记录到文件
                # print(f"[{role_name}] 🛠️ 调用工具: {tool_name}")  # 注释掉
                
                if tool_name in tool_map:
                    tool_output = tool_map[tool_name].invoke(tool_args)
                else:
                    tool_output = f"Error: Tool {tool_name} not found."

                messages.append(ToolMessage(content=str(tool_output), tool_call_id=tool_id))

        except Exception as e:
            # 错误也不打印到前端
            # print(f"⚠️ [{role_name}] Step {step} Error: {e}")  # 注释掉
            if step == max_steps - 1:
                return f"SYSTEM ERROR: {str(e)}"

    return messages[-1].content

# ========== 4. 节点定义 ==========

def strategist_node(state: PenTestState) -> PenTestState:
    cfg = agents_config['strategist']
    task = tasks_config['strategy_task']
    
    sys_prompt = f"Role: {cfg['role']}\nGoal: {cfg['goal']}\nBackstory: {cfg['backstory']}\n{COT_INSTRUCTION}"
    task_prompt = task['description'].format(target=state['target'], mission_history=state['mission_history'])
    
    # 替换 create_react_agent，使用自定义逻辑
    result = execute_agent_logic(
        "STRATEGIST", 
        [file_read_tool], 
        sys_prompt, 
        task_prompt
    )
    
    print(f"[Strategist] Output: {result}...")
    return {**state, "strategy": result}

def deputy_node(state: PenTestState) -> PenTestState:
    cfg = agents_config['deputy']
    task = tasks_config['deputy_task']
    
    sys_prompt = f"Role: {cfg['role']}\nGoal: {cfg['goal']}\nBackstory: {cfg['backstory']}"
    task_prompt = f"战略意图: {state['strategy']}\n\n{task['description']}\n请输出技术需求："
    
    # Deputy 不需要工具，传空列表
    result = execute_agent_logic(
        "DEPUTY", 
        [], 
        sys_prompt, 
        task_prompt
    )
    
    print(f"[Deputy] Output: {result}...")
    return {**state, "deputy_requirement": result}

def operator_node(state: PenTestState) -> PenTestState:
    cfg = agents_config['operator']
    task = tasks_config['operator_task']
    
    sys_prompt = f"Role: {cfg['role']}\nGoal: {cfg['goal']}\nBackstory: {cfg['backstory']}\n{COT_INSTRUCTION}"
    task_prompt = task['description'].format(target=state['target']) + f"\n\nDeputy需求: {state['deputy_requirement']}"
    
    result = execute_agent_logic(
        "OPERATOR", 
        [list_custom_tool], 
        sys_prompt, 
        task_prompt
    )
    
    print(f"[Operator] Output: {result}...")
    return {**state, "operator_command": result}

def auditor_node(state: PenTestState) -> PenTestState:
    cfg = agents_config['auditor']
    task = tasks_config['auditor_task']
    
    sys_prompt = f"Role: {cfg['role']}\nGoal: {cfg['goal']}\nBackstory: {cfg['backstory']}\n{COT_INSTRUCTION}"
    task_prompt = f"Operator生成的命令: {state['operator_command']}\n\n{task['description']}\n请执行命令："
    
    result = execute_agent_logic(
        "AUDITOR", 
        [execution_tool], 
        sys_prompt, 
        task_prompt
    )
    
    # 结果截断
    if len(result) > 1000:
        result = result + "\n[...Truncated...]"
        
    print(f"[Auditor] Output: {result}...")
    return {**state, "execution_result": result}



def reporter_node(state: PenTestState) -> PenTestState:
    cfg = agents_config['reporter']
    task = tasks_config['reporting_task']
    
    sys_prompt = f"Role: {cfg['role']}\nGoal: {cfg['goal']}\nBackstory: {cfg['backstory']}\n{COT_INSTRUCTION}"
    
    result = execute_agent_logic(
        "REPORTER", 
        [file_read_tool, file_write_tool], 
        sys_prompt, 
        task['description']
    )
    
    return {**state, "final_report": result}

def html_reporter_node(state: PenTestState) -> PenTestState:
    # 1. 读取配置
    cfg = agents_config['html_reporter']
    task = tasks_config['html_reporting_task']
        # 硬编码 CSS 模板
    css_template = """
    :root { --bg: #f4f6f8; --card: #ffffff; --text: #2d3748; --meta: #718096; --accent: #4a5568; --border: #e2e8f0; }
       body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text); padding: 40px; line-height: 1.7; }
       .container { max-width: 850px; margin: 0 auto; background: var(--card); padding: 60px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); border-radius: 8px; }
       h1 { font-size: 2.2rem; color: #1a202c; border-bottom: 2px solid var(--border); padding-bottom: 20px; margin-bottom: 30px; letter-spacing: -0.025em; }
       h2 { font-size: 1.4rem; color: var(--accent); margin-top: 40px; margin-bottom: 15px; font-weight: 600; display: flex; align-items: center; }
       h2::before { content: ''; width: 4px; height: 20px; background: #cbd5e0; margin-right: 12px; display: inline-block; }
       p { margin-bottom: 1.5em; color: #4a5568; text-align: justify; }
       .highlight-box { background: #f7fafc; border-left: 4px solid #4299e1; padding: 15px 20px; margin: 20px 0; font-size: 0.95rem; color: #2c5282; }
       .warning-box { background: #fffaf0; border-left: 4px solid #ed8936; padding: 15px 20px; margin: 20px 0; color: #744210; }
       code { background: #edf2f7; color: #c53030; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; font-family: 'SFMono-Regular', Consolas, monospace; }
       table { width: 100%; border-collapse: collapse; margin: 25px 0; font-size: 0.9rem; }
       th { text-align: left; padding: 12px; background: #f7fafc; color: var(--accent); border-bottom: 1px solid var(--border); font-weight: 600; }
       td { padding: 12px; border-bottom: 1px solid var(--border); color: var(--text); }
       .empty-state { color: var(--meta); font-style: italic; text-align: center; padding: 20px; background: #f9fafb; border-radius: 6px; }
       footer { margin-top: 60px; text-align: center; font-size: 0.85rem; color: var(--meta); border-top: 1px solid var(--border); padding-top: 20px; }
    """
    
    # 2. 组装 Prompt
    # 注意：我们复用了之前的 COT_INSTRUCTION (思维链指令)
    sys_prompt = f"{cfg['role']}\n{cfg['goal']}\n{COT_INSTRUCTION}\n请在 HTML 中嵌入以下 CSS：\n{css_template}"
    
    # 3. 执行 Agent 逻辑
    # 我们直接使用之前封装好的 execute_agent_logic
    result = execute_agent_logic(
        "HTML_REPORTER",                 # 角色名 (用于日志打印)
        [file_read_tool, file_write_tool], # 需要用到的工具
        sys_prompt,                      # 系统提示词
        task['description']              # 任务描述
    )
    
    print(f"[HTML Reporter] 报告生成完成")
    
    # 4. 更新状态
    return {**state, "final_html": result}