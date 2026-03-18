import os
import sys
import json
import yaml
from pathlib import Path
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from state import PenTestState
from config import config

THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = THIS_FILE.parent.parent
env_path = PROJECT_ROOT / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.system_tools import file_read_tool, file_write_tool
from tools.custom_tools import execution_tool, list_custom_tool

with open("config/agents.yaml", "r", encoding="utf-8") as f:
    agents_config = yaml.safe_load(f)
with open("config/tasks.yaml", "r", encoding="utf-8") as f:
    tasks_config = yaml.safe_load(f)

ROLE_NAME_MAP = {
    "STRATEGIST": "渗透指挥官",
    "DEPUTY": "副指挥官",
    "OPERATOR": "战术执行专家",
    "AUDITOR": "执行引擎",
    "REPORTER": "日志摘要专家",
    "HTML_REPORTER": "高级渗透测试报告分析师",
}

# #COT_INSTRUCTION = """
# IMPORTANT:
# 1. You represent the internal thought process of the AI.
# 2. Before calling ANY tool, you MUST explain your reasoning starting with "Thought: ...".
# 3. If you decide to call a tool, generate the tool call immediately after the thought.
# """

COT_INSTRUCTION = "If you decide to call a tool, generate the tool call immediately after the thought."

def read_mission_log(max_chars: int = 20000) -> str:
    log_path = os.getenv("LOG_FILE_PATH", "mission.log")
    if not os.path.exists(log_path):
        return "[mission.log 不存在]"

    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        if not content.strip():
            return "[mission.log 为空]"
        if len(content) > max_chars:
            return content[-max_chars:]
        return content
    except Exception as e:
        return f"[读取 mission.log 失败: {str(e)}]"


def get_role_display_name(role_name: str) -> str:
    return ROLE_NAME_MAP.get(role_name, role_name)


def write_ui_log(text: str):
    ui_log_path = os.getenv("UI_LOG_FILE_PATH", "ui_mission.log")
    try:
        with open(ui_log_path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        with open("ui_mission.log", "a", encoding="utf-8") as f:
            f.write(text + "\n")


def write_core_log(text: str):
    log_path = os.getenv("LOG_FILE_PATH", "mission.log")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        with open("mission.log", "a", encoding="utf-8") as f:
            f.write(text + "\n")


def create_llm(role: str, temperature: float = 0.1):
    api_key = config.get_agent_api_key(role)
    model = config.get_agent_model(role)
    base_url = config.get_agent_base_url(role)

    if not api_key:
        raise ValueError(f"Missing API key for {role}")

    return ChatOpenAI(
        model=model,
        temperature=temperature,
        openai_api_key=api_key,
        openai_api_base=base_url,
        timeout=60,
        max_retries=2,
    )


def get_local_system_type() -> str:
    import platform
    system = platform.system().lower()
    if "windows" in system:
        return "windows"
    elif "linux" in system:
        return "linux"
    elif "darwin" in system:
        return "macos"
    return "unknown"


def build_os_hint() -> str:
    local_os = get_local_system_type()
    if local_os == "windows":
        return (
            "【重要提示：当前执行环境是 Windows】\n"
            "命令必须可在 Windows 上执行；不要生成或执行 bash/sh 专用命令。\n"
        )
    elif local_os == "linux":
        return (
            "【重要提示：当前执行环境是 Linux】\n"
            "命令必须可在 Linux 上执行；不要生成或执行 cmd/powershell 专用命令。\n"
        )
    elif local_os == "macos":
        return (
            "【重要提示：当前执行环境是 macOS】\n"
            "命令必须按类 Unix 环境执行；优先使用 bash/zsh 兼容语法。\n"
        )
    return "【重要提示：当前执行环境未知】请谨慎判断命令兼容性。\n"


def format_tool_call_detail(tool_name: str, tool_args) -> str:
    if not isinstance(tool_args, dict):
        return str(tool_args)

    if tool_name == "execution_tool":
        return tool_args.get("cmd", "")
    if tool_name == "list_custom_tool":
        subdir = tool_args.get("subdir", "")
        return f"subdir={subdir or '.'}"
    if tool_name == "file_read_tool":
        return tool_args.get("filename", "")
    if tool_name == "file_write_tool":
        filename = tool_args.get("filename", "")
        overwrite = tool_args.get("overwrite", True)
        return f"filename={filename}, overwrite={overwrite}"

    try:
        return json.dumps(tool_args, ensure_ascii=False)
    except Exception:
        return str(tool_args)


def summarize_tool_output(tool_name: str, tool_output: str) -> str:
    out = str(tool_output).strip()
    if not out:
        return "无输出"

    if tool_name == "execution_tool":
        lines = [x.strip() for x in out.splitlines() if x.strip()]
        if not lines:
            return "命令已执行"
        return lines[0]

    if tool_name == "file_read_tool":
        return "文件已读取"

    if tool_name == "file_write_tool":
        first_line = out.splitlines()[0].strip() if out.splitlines() else ""
        return first_line or "文件已写入"

    if tool_name == "list_custom_tool":
        lines = [x.strip() for x in out.splitlines() if x.strip()]
        if not lines:
            return "目录读取完成"
        if len(lines) == 1:
            return lines[0]
        return f"{lines[0]}（共 {len(lines) - 1} 条项目）"

    first_line = out.splitlines()[0].strip() if out.splitlines() else ""
    return first_line[:160] if first_line else "工具执行完成"


def log_final_agent_output(role_name: str, result: str):
    role_display = get_role_display_name(role_name)
    if not result:
        #write_ui_log(f"[RESULT] 📤 {role_display} 输出: 无输出")
        return

    compact = str(result).replace("\n", " ").strip()
    if len(compact) > 500:
        compact = compact[:500] + " ...[已截断]"

    #write_ui_log(f"[RESULT] 📤 {role_display} 输出: {compact}")


def execute_agent_logic(role_name, tools, system_text, user_text, max_steps=5):
    role_display = get_role_display_name(role_name)
    write_ui_log(f"[AGENT] 🎯 {role_display} 已接管任务，开始分析...")

    llm = create_llm(role_name, temperature=0.05)

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

            response_content = ""
            if hasattr(response, "content"):
                response_content = response.content or ""
            elif isinstance(response, str):
                response_content = response

            if response_content.strip():
                thought = response_content.replace("\n", " ").strip()
                if len(thought) > 500:
                    #thought = thought[:500] + " ...[已截断]" #只输出前500字符
                    thought = "【大字符串】" + thought
                write_ui_log(f"[THOUGHT] 🧠 {role_display} 思考: {thought}")

            tool_calls = getattr(response, "tool_calls", [])
            if not tool_calls:
                #write_ui_log(f"[AGENT] ✅ {role_display} 阶段任务输出完成。")
                return response_content

            for tool_call in tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                tool_id = tool_call["id"]

                detail = format_tool_call_detail(tool_name, tool_args)
                write_ui_log(f"[TOOL] 🛠️ {role_display} 调用工具 [{tool_name}] 参数: {detail}")

                if tool_name in tool_map:
                    tool_output = tool_map[tool_name].invoke(tool_args)
                else:
                    tool_output = f"Error: Tool {tool_name} not found."

                out_str = str(tool_output)
                summary = summarize_tool_output(tool_name, out_str)
                write_ui_log(f"[RESULT] 📥 工具 [{tool_name}] 结果: {summary}")

                messages.append(ToolMessage(content=out_str, tool_call_id=tool_id))

        except Exception as e:
            write_ui_log(f"[ERROR] ❌ {role_display} 执行出错: {str(e)}")
            write_core_log(f"[ERROR] {role_display} 执行出错: {str(e)}")
            if step == max_steps - 1:
                return f"SYSTEM ERROR: {str(e)}"

    last_message = messages[-1]
    if hasattr(last_message, "content"):
        return last_message.content
    elif isinstance(last_message, str):
        return last_message
    return str(last_message)


def strategist_node(state: PenTestState) -> PenTestState:
    cfg = agents_config["strategist"]
    task = tasks_config["strategy_task"]

    log_text = read_mission_log(max_chars=20000)

    sys_prompt = (
        f"Role: {cfg['role']}\n"
        f"Goal: {cfg['goal']}\n"
        f"Backstory: {cfg['backstory']}\n"
        f"{COT_INSTRUCTION}\n"
        "你会直接收到任务描述和 mission.log 正文。\n"
        "mission.log 是唯一证据源，mission_history 不是证据源。\n"
        "你必须严格基于 mission.log 中出现的真实文本做判断。\n"
        "如果日志中包含 HTTP 响应、HTML、命令输出、报错、版本、框架指纹，"
        "不得说“日志为空”或“没有有效信息”，除非日志正文确实为空。"
    )

    task_prompt = (
        f"{task['description']}\n\n"
        f"目标: {state['target']}\n\n"
        "=== mission.log 正文开始 ===\n"
        f"{log_text}\n"
        "=== mission.log 正文结束 ===\n\n"
        "请基于以上 mission.log 正文：\n"
        "1. 提取已确认事实；\n"
        "2. 识别技术栈/指纹；\n"
        "3. 给出下一步策略。\n"
    )

    result = execute_agent_logic(
        "STRATEGIST",
        [file_read_tool],
        sys_prompt,
        task_prompt,
        max_steps=6,
    )
    log_final_agent_output("STRATEGIST", result)
    return {**state, "strategy": result}


def deputy_node(state: PenTestState) -> PenTestState:
    cfg = agents_config["deputy"]
    task = tasks_config["deputy_task"]

    sys_prompt = f"Role: {cfg['role']}\nGoal: {cfg['goal']}\nBackstory: {cfg['backstory']}"
    task_prompt = f"战略意图: {state['strategy']}\n\n{task['description']}\n请输出技术需求："

    result = execute_agent_logic("DEPUTY", [], sys_prompt, task_prompt)
    log_final_agent_output("DEPUTY", result)
    return {**state, "deputy_requirement": result}


def operator_node(state: PenTestState) -> PenTestState:
    cfg = agents_config["operator"]
    task = tasks_config["operator_task"]

    os_hint = build_os_hint()

    sys_prompt = f"Role: {cfg['role']}\nGoal: {cfg['goal']}\nBackstory: {cfg['backstory']}\n{COT_INSTRUCTION}"
    task_prompt = (
        task["description"].format(target=state["target"])
        + f"\n\n{os_hint}\nDeputy需求: {state['deputy_requirement']}"
    )

    result = execute_agent_logic("OPERATOR", [list_custom_tool], sys_prompt, task_prompt)
    log_final_agent_output("OPERATOR", result)
    return {**state, "operator_command": result}


def auditor_node(state: PenTestState) -> PenTestState:
    cfg = agents_config["auditor"]
    task = tasks_config["auditor_task"]

    os_hint = build_os_hint()

    sys_prompt = (
        f"Role: {cfg['role']}\n"
        f"Goal: {cfg['goal']}\n"
        f"Backstory: {cfg['backstory']}\n"
        f"{COT_INSTRUCTION}\n"
        f"{os_hint}"
        "你必须执行命令，并返回真实执行结果摘要。不要把结果改写成固定话术。\n"
        "如果命令明显不属于当前操作系统，请明确指出兼容性问题。"
    )
    task_prompt = f"Operator生成的命令: {state['operator_command']}\n\n{task['description']}\n请执行命令："

    result = execute_agent_logic("AUDITOR", [execution_tool], sys_prompt, task_prompt)

    if not result:
        result = "无输出"
    elif len(result) > 4000:
        result = result[:4000] + "\n...[Truncated]"

    log_final_agent_output("AUDITOR", result)
    return {**state, "execution_result": result}


def reporter_node(state: PenTestState) -> PenTestState:
    cfg = agents_config["reporter"]
    task = tasks_config["reporting_task"]
    log_path = os.getenv("LOG_FILE_PATH", "./mission.log")

    sys_prompt = (
        f"Role: {cfg['role']}\n"
        f"Goal: {cfg['goal']}\n"
        f"Backstory: {cfg['backstory']}\n"
        f"{COT_INSTRUCTION}\n"
        "你必须基于 mission.log 生成报告，mission.log 是唯一证据源。"
    )
    task_prompt = (
        f"{task['description']}\n\n"
        f"目标: {state['target']}\n"
        f"日志文件: {log_path}\n"
        "请先读取日志，再撰写报告并写入文件。"
    )

    result = execute_agent_logic("REPORTER", [file_read_tool, file_write_tool], sys_prompt, task_prompt, max_steps=6)
    log_final_agent_output("REPORTER", result)
    return {**state, "final_report": result}


def html_reporter_node(state: PenTestState) -> PenTestState:
    cfg = agents_config["html_reporter"]
    task = tasks_config["html_reporting_task"]

    css_template = """
:root {
--bg: #f9f9f9;
--card: #ffffff;
--text: #333333;
--meta: #666666;
--accent: #b91c1c;
--border: #dddddd;
}
body {
font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
background: var(--bg);
color: var(--text);
padding: 30px;
line-height: 1.6;
}
.container {
max-width: 900px;
margin: 0 auto;
background: var(--card);
padding: 50px 60px;
border: 1px solid #d1d5db;
box-shadow: 0 4px 12px rgba(0,0,0,0.05);
}
h1 {
font-size: 2.2rem;
color: #111111;
border-bottom: 3px solid var(--accent);
padding-bottom: 15px;
margin-bottom: 30px;
text-transform: uppercase;
letter-spacing: 1px;
}
h2 {
font-size: 1.3rem;
color: #1a1a1a;
margin-top: 40px;
margin-bottom: 15px;
font-weight: bold;
border-bottom: 1px solid var(--border);
padding-bottom: 8px;
}
pre {
background: #111827;
color: #e5e7eb;
padding: 15px;
overflow-x: auto;
border-left: 3px solid var(--accent);
}
table { width: 100%; border-collapse: collapse; margin: 25px 0; font-size: 0.9rem; }
th, td { padding: 12px; border: 1px solid var(--border); }
"""

    sys_prompt = f"{cfg['role']}\n{cfg['goal']}\n{COT_INSTRUCTION}\n请在 HTML 中嵌入以下 CSS：\n{css_template}"
    task_prompt = task["description"] + "\n\n以下是报告基础内容：\n" + state.get("final_report", "")

    result = execute_agent_logic("HTML_REPORTER", [file_read_tool, file_write_tool], sys_prompt, task_prompt)
    log_final_agent_output("HTML_REPORTER", result)
    return {**state, "final_html": result}
