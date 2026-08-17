import os
import sys
import json
import time
import threading
import yaml
from pathlib import Path
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from state import PenTestState
from config import config
from security import (
    ensure_utf8_console,
    mission_control,
    file_write_lock,
)

ensure_utf8_console()

THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = config.PROJECT_ROOT
env_path = PROJECT_ROOT / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)

sys.path.insert(0, str(PROJECT_ROOT))

from tools.system_tools import file_read_tool, file_write_tool
from tools.custom_tools import execution_tool, list_custom_tool

# 模块7: Agent 配置优先从 agent_manager 读取（用户可增删改查角色）；
# 回退到 YAML 文件（内置默认）
from agent_manager import load_agent_configs as _load_agent_cfg

try:
    agents_config = _load_agent_cfg()
except Exception:
    with open(str(PROJECT_ROOT / "config" / "agents.yaml"), "r", encoding="utf-8") as f:
        agents_config = yaml.safe_load(f)
with open(str(PROJECT_ROOT / "config" / "tasks.yaml"), "r", encoding="utf-8") as f:
    tasks_config = yaml.safe_load(f)

# ========== 模块2: 插话机制（用户临时需求） ==========
# 用户随时插入的新需求，会在下一个 agent 执行时附加到 system prompt。
# 使用锁保证线程安全（GUI 线程写入，agent 线程读取）。
_interrupt_lock = threading.Lock()
_interrupt_requests: list[str] = []


def add_interrupt_request(text: str):
    """用户插入一条临时需求（GUI 调用）。"""
    with _interrupt_lock:
        _interrupt_requests.append(text)


def consume_interrupt_requests() -> str:
    """取出所有待处理的插话请求，返回格式化文本；无则返回空串。"""
    global _interrupt_requests
    with _interrupt_lock:
        if not _interrupt_requests:
            return ""
        reqs = _interrupt_requests
        _interrupt_requests = []
    lines = ["【用户临时插话】用户在任务进行中提出了以下新需求，请在本次执行中优先考虑："]
    for i, r in enumerate(reqs, 1):
        lines.append(f"{i}. {r}")
    lines.append("【插话结束】")
    return "\n".join(lines)


# ========== 模块1/10: RAG 与 Skill 注入 ==========
# 懒加载避免循环依赖
_rag_store = None
_skill_manager = None


def _get_rag_store():
    global _rag_store
    if _rag_store is None:
        from rag_store import rag_store
        _rag_store = rag_store
    return _rag_store


def _get_skill_manager():
    global _skill_manager
    if _skill_manager is None:
        from skill_manager import skill_manager
        _skill_manager = skill_manager
    return _skill_manager


def build_context_injections(target: str = "", mission_history: str = "") -> str:
    """构建注入 system prompt 的上下文（RAG 知识 + 激活的 Skill）。
    返回格式化文本；无可用内容返回空串。"""
    parts = []
    try:
        rag_prompt = _get_rag_store().build_rag_prompt(
            f"{target} {mission_history[:500]}", limit=3
        )
        if rag_prompt:
            parts.append(rag_prompt)
    except Exception:
        pass
    try:
        skills = _get_skill_manager().get_active_skills_content()
        if skills:
            parts.append(skills)
    except Exception:
        pass
    return "\n\n".join(parts)


# 当前任务上下文（供 RAG 检索注入用，由各 node 入口更新）
CURRENT_TARGET = ""
CURRENT_HISTORY = ""

ROLE_NAME_MAP = {
    "STRATEGIST": "渗透指挥官",
    "DEPUTY": "副指挥官",
    "OPERATOR": "战术执行专家",
    "AUDITOR": "执行引擎",
    "REPORTER": "日志摘要专家",
    "HTML_REPORTER": "高级渗透测试报告分析师",
}


COT_INSTRUCTION = "If you decide to call a tool, generate the tool call immediately after the thought."


def read_mission_log(max_chars: int = 20000) -> str:
    log_path = config.LOG_FILE_PATH
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
    ui_log_path = config.UI_LOG_FILE_PATH
    try:
        with file_write_lock:
            os.makedirs(os.path.dirname(ui_log_path) or ".", exist_ok=True)
            with open(ui_log_path, "a", encoding="utf-8") as f:
                f.write(text + "\n")
    except Exception:
        with file_write_lock:
            with open(str(PROJECT_ROOT / "ui_mission.log"), "a", encoding="utf-8") as f:
                f.write(text + "\n")


def write_core_log(text: str):
    log_path = config.LOG_FILE_PATH
    try:
        with file_write_lock:
            os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(text + "\n")
    except Exception:
        with file_write_lock:
            with open(str(PROJECT_ROOT / "mission.log"), "a", encoding="utf-8") as f:
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


def _extract_reasoning(response) -> str:
    """提取 LLM 真实推理内容，避免把模型表面输出冒充为思考。

    优先级：
    1. response.reasoning_content（langchain 对部分模型自动映射）
    2. response.additional_kwargs.reasoning_content（deepseek 等原生存取）
    3. content 中显式的 "Thought:" 前缀文本
    返回空串表示没有真实推理内容。
    """
    # 1. langchain 映射字段
    reasoning = getattr(response, "reasoning_content", None)
    if reasoning and str(reasoning).strip():
        return str(reasoning)

    # 2. additional_kwargs 原生态（deepseek-reasoner / 部分网关）
    kwargs = getattr(response, "additional_kwargs", None) or {}
    reasoning = kwargs.get("reasoning_content") or kwargs.get("reasoning")
    if reasoning and str(reasoning).strip():
        return str(reasoning)

    # 3. content 显式 "Thought:" 前缀（模型主动输出时视为思考）
    content = getattr(response, "content", "") or ""
    if isinstance(content, str):
        stripped = content.strip()
        if stripped.lower().startswith("thought:"):
            return stripped
    return ""


# ========== 流式输出支持 ==========
STREAM_FLUSH_INTERVAL = 0.3  # reasoning 刷盘间隔（秒）
STREAM_LINE_MAX = 200        # 每次刷出的最大字符数
# 换行编码标记：流式片段写入单行日志时，把真实换行替换为 ⏎，
# GUI 端解码回 \n，避免 markdown 结构（标题/段落/列表）被空格抹平
NEWLINE_MARK = "⏎"


def _encode_stream_text(text: str) -> str:
    """把文本中的换行编码为 ⏎（单行日志内保持结构信息）。"""
    return text.replace("\n", NEWLINE_MARK)


def _patch_reasoning_content_support():
    """
    langchain-openai 1.1.8 不解析 deepseek 的 reasoning_content 字段
    （_convert_delta_to_message_chunk 只处理 function_call/tool_calls），
    导致流式/非流式都拿不到模型思考内容。

    这里 monkey-patch 该转换函数，把 delta.reasoning_content 和
    message.reasoning_content 写入 additional_kwargs，兼容 deepseek-reasoner。
    """
    try:
        import langchain_openai.chat_models.base as _lc_base
        import langchain_core.messages as _lc_msg

        orig = _lc_base._convert_delta_to_message_chunk

        def _patched(delta: dict, default_class):
            chunk = orig(delta, default_class)
            if isinstance(chunk, _lc_msg.AIMessageChunk):
                rk = delta.get("reasoning_content")
                if rk:
                    chunk.additional_kwargs.setdefault("reasoning_content", "") 
                    # 注意: delta 转换的 chunk 每次都是新的，流式聚合靠 + 拼接；
                    # 这里直接写入，聚合时 LangChain 会自动拼接字符串字段
                    chunk.additional_kwargs["reasoning_content"] = rk
            return chunk

        if _lc_base._convert_delta_to_message_chunk is not orig:
            return  # 已打过补丁
        _lc_base._convert_delta_to_message_chunk = _patched
    except Exception:
        pass  # 补丁失败不影响主流程（仅拿不到思考）

    # 非流式路径: ChatOpenAI._create_chat_result 里的 message 解析
    try:
        import langchain_openai.chat_models.base as _lc_base2
        import langchain_core.messages as _lc_msg2

        # 检查 _create_chat_result 是否透传 reasoning_content
        orig_create = _lc_base2.ChatOpenAI._create_chat_result

        def _patched_create(self, response, generation_info=None):
            result = orig_create(self, response, generation_info)
            try:
                if response and response.choices:
                    msg = response.choices[0].message
                    # deepseek 的 message 可能是 dict 或 pydantic 对象
                    if isinstance(msg, dict):
                        rk = msg.get("reasoning_content")
                    else:
                        rk = getattr(msg, "reasoning_content", None)
                    if rk and result.generations:
                        # generations 结构可能是 List[ChatGeneration] 或 List[List[ChatGeneration]]
                        first = result.generations[0]
                        gen = first[0] if isinstance(first, list) and first else first
                        if hasattr(gen, "message") and hasattr(gen.message, "additional_kwargs"):
                            gen.message.additional_kwargs["reasoning_content"] = rk
            except Exception:
                pass
            return result

        if orig_create is not _patched_create:
            _lc_base2.ChatOpenAI._create_chat_result = _patched_create
    except Exception:
        pass


# 启动时安装补丁
_patch_reasoning_content_support()


def _stream_llm(llm_with_tools, messages, role_display):
    """
    流式调用 LLM：
    - 实时提取 reasoning_content（思考）写入 UI 日志，用户可见模型正在工作
    - 实时提取 content（回答文本）写入 UI 日志，用户可见模型的具体返回内容
    - 同时聚合所有 chunk 为完整响应（含 tool_calls），供后续驱动执行
    - reasoning_content 在 additional_kwargs 中是覆盖式，这里手动拼接保留完整
    返回: 聚合后的完整响应对象（与 invoke 返回结构兼容）
    """
    from langchain_core.messages import AIMessageChunk, AIMessage

    final_response = None
    reasoning_parts = []
    pending_reasoning = ""
    pending_content = ""
    last_flush = time.time()

    def flush_buffers():
        """把累积的思考/回答文本刷到 UI（按行分批，避免刷屏）。"""
        nonlocal pending_reasoning, pending_content, last_flush
        now = time.time()

        if pending_reasoning.strip():
            line = _encode_stream_text(pending_reasoning).strip()
            if line:
                if len(line) > STREAM_LINE_MAX:
                    write_ui_log(f"[THOUGHT] 🧠 {role_display} 思考: {line[:STREAM_LINE_MAX]}")
                    write_ui_log(f"[THOUGHT] ... {line[STREAM_LINE_MAX:STREAM_LINE_MAX * 2]}")
                else:
                    write_ui_log(f"[THOUGHT] 🧠 {role_display} 思考: {line}")
            pending_reasoning = ""

        if pending_content.strip():
            # 保留换行结构（编码为 ⏎），避免 markdown 被抹平成单行
            line = _encode_stream_text(pending_content).strip()
            if line:
                if len(line) > STREAM_LINE_MAX:
                    write_ui_log(f"[REPLY] 💬 {role_display} 返回: {line[:STREAM_LINE_MAX]}")
                    write_ui_log(f"[REPLY] ... {line[STREAM_LINE_MAX:STREAM_LINE_MAX * 2]}")
                else:
                    write_ui_log(f"[REPLY] 💬 {role_display} 返回: {line}")
            pending_content = ""

        last_flush = now

    # 流式循环
    for chunk in llm_with_tools.stream(messages):
        # 停止检查
        if mission_control.stopped:
            write_ui_log(f"[ERROR] ⏹ {role_display} 流式输出被用户停止")
            break

        # 提取 reasoning_content（deepseek 等思考模型）
        rk = chunk.additional_kwargs.get("reasoning_content")
        if rk and str(rk).strip():
            reasoning_parts.append(str(rk))
            pending_reasoning += str(rk)

        # 提取 content（回答文本）
        c = getattr(chunk, "content", None)
        if isinstance(c, str) and c.strip():
            pending_content += c

        # 聚合 chunk（content 自动拼接，tool_calls 由 langchain 内部合并）
        if final_response is None:
            final_response = chunk
        else:
            final_response = final_response + chunk

        # 定时把新增思考/回答刷到 UI（避免每个 chunk 写一行导致刷屏）
        now = time.time()
        if (pending_reasoning or pending_content) and (now - last_flush >= STREAM_FLUSH_INTERVAL):
            flush_buffers()

    # 刷出剩余思考/回答
    flush_buffers()

    # 手动补全 reasoning_content（AIMessageChunk + 不会合并 additional_kwargs 内的字符串字段）
    if reasoning_parts and final_response is not None:
        full_reasoning = "".join(reasoning_parts)
        if hasattr(final_response, "additional_kwargs"):
            final_response.additional_kwargs["reasoning_content"] = full_reasoning

    # 统一转为 AIMessage，保证与 invoke 返回结构完全一致
    if isinstance(final_response, AIMessageChunk):
        final_response = AIMessage(
            content=final_response.content or "",
            tool_calls=list(final_response.tool_calls or []),
            additional_kwargs=dict(final_response.additional_kwargs or {}),
        )

    return final_response


def _invoke_llm(llm_with_tools, messages, role_display):
    """
    优先流式调用；流式失败自动回退到非流式 invoke，保证稳定性。
    返回: (response, streamed_content)
      - streamed_content: 流式已输出的 content 文本（避免重复记录）
    """
    try:
        resp = _stream_llm(llm_with_tools, messages, role_display)
        return resp, True
    except Exception as e:
        write_ui_log(f"[WARN] ⚠️ {role_display} 流式调用失败({str(e)[:120]})，回退到非流式")
        return llm_with_tools.invoke(messages), False


def execute_agent_logic(role_name, tools, system_text, user_text, max_steps=5):
    role_display = get_role_display_name(role_name)
    write_ui_log(f"[AGENT] 🎯 {role_display} 已接管任务，开始分析...")

    # ========== 模块2: 插话注入（用户临时需求，在下一个 agent 执行时生效） ==========
    interrupt_text = consume_interrupt_requests()
    if interrupt_text:
        system_text = system_text + "\n\n" + interrupt_text
        write_ui_log(f"[AGENT] 📢 已注入用户插话需求")

    # ========== 模块1/10: RAG + Skill 上下文注入 ==========
    try:
        ctx = build_context_injections(
            target=CURRENT_TARGET,
            mission_history=CURRENT_HISTORY,
        )
        if ctx:
            system_text = system_text + "\n\n" + ctx
    except Exception:
        pass

    llm = create_llm(role_name, temperature=0.05)

    # 模块7/9: 按 agent_manager 配置的工具 + 内置工具扩展角色工具集
    if tools:
        # 加载 agent_manager 为该角色配置的工具（若存在）
        try:
            from agent_manager import agent_manager as _am
            cfg_tools = _am.get_tools_for(role_name.lower())
            if cfg_tools:
                all_tools = {t.name: t for t in tools}
                # 内置工具补充
                try:
                    import builtin_tools as _bt
                    for bt in _bt.BUILTIN_TOOLS:
                        all_tools.setdefault(bt.name, bt)
                except Exception:
                    pass
                # 按配置选择（配置名可能包含内置工具名）
                merged = []
                for name in cfg_tools:
                    if name in all_tools:
                        merged.append(all_tools[name])
                if merged:
                    tools = merged
        except Exception:
            pass

        llm_with_tools = llm.bind_tools(tools)
        tool_map = {t.name: t for t in tools}
        # ========== 模块8: 工具缓存提示（节省 AI 消耗） ==========
        try:
            from tool_cache import tool_cache as _tc
            hints = []
            for t in tools:
                h = _tc.build_tool_hint(t)
                if h:
                    hints.append(h)
            if hints:
                system_text = system_text + "\n\n【工具使用参考】以下为工具说明与常用参数（来自缓存，可减少试错）：\n" + "\n".join(hints)
        except Exception:
            pass
    else:
        llm_with_tools = llm
        tool_map = {}

    messages = [SystemMessage(content=system_text), HumanMessage(content=user_text)]
    streamed = True  # 默认视为已流式输出（循环异常退出时不重复记录）

    for step in range(max_steps):
        # --- 架构4: 停止检查 ---
        if mission_control.stopped:
            write_ui_log(f"[ERROR] ❌ {role_display} 任务已被用户停止")
            return "SYSTEM: 任务已被用户停止。"

        try:
            # --- 问题1修复: 调用前状态日志（证明程序在运行） ---
            write_ui_log(f"[AGENT] ⏳ {role_display} 正在调用模型 API（流式），请稍候...")
            _t0 = time.time()
            # 流式调用：思考/回答实时滚动输出，同时聚合完整响应
            response, streamed = _invoke_llm(llm_with_tools, messages, role_display)
            _elapsed = time.time() - _t0
            # (优化) 不打印"响应完成"，只保留开始行为，减少噪音
            messages.append(response)

            response_content = ""
            if hasattr(response, "content"):
                response_content = response.content or ""
            elif isinstance(response, str):
                response_content = response

            tool_calls = getattr(response, "tool_calls", [])
            has_tool_calls = bool(tool_calls)

            # 问题2修复: 记录 LLM 具体返回内容（最终回答）
            if not has_tool_calls:
                # 流式已输出则不再重复；仅非流式回退时补充记录
                if not streamed and response_content and response_content.strip():
                    write_ui_log(f"[REPLY] 💬 {role_display} 返回: {_encode_stream_text(str(response_content))[:500]}")
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

                # 模块8: 记录工具调用到缓存（供后续 LLM 参考成功参数）
                try:
                    from tool_cache import tool_cache as _tc
                    _tc.increment_usage(tool_name)
                    _tc.record_call(tool_name, tool_args, summary[:200])
                except Exception:
                    pass

                messages.append(ToolMessage(content=out_str, tool_call_id=tool_id))

        except Exception as e:
            write_ui_log(f"[ERROR] ❌ {role_display} 执行出错: {str(e)}")
            write_core_log(f"[ERROR] {role_display} 执行出错: {str(e)}")
            if step == max_steps - 1:
                return f"SYSTEM ERROR: {str(e)}"
            # P2: 简单指数退避后重试
            time.sleep(min(2 ** (step + 1), 8))

    last_message = messages[-1]
    if hasattr(last_message, "content"):
        content = last_message.content
        # 流式已实时输出过 content，这里仅在非流式回退路径下补记
        if not streamed and content and str(content).strip():
            write_ui_log(f"[REPLY] 💬 {role_display} 最终返回: {_encode_stream_text(str(content))[:500]}")
        return content
    elif isinstance(last_message, str):
        if not streamed and last_message.strip():
            write_ui_log(f"[REPLY] 💬 {role_display} 最终返回: {_encode_stream_text(last_message)[:500]}")
        return last_message
    return str(last_message)


def strategist_node(state: PenTestState) -> PenTestState:
    cfg = agents_config["strategist"]
    task = tasks_config["strategy_task"]

    # 更新当前任务上下文（供 RAG/skill 注入用）
    global CURRENT_TARGET, CURRENT_HISTORY
    CURRENT_TARGET = state.get("target", "")
    CURRENT_HISTORY = state.get("mission_history", "") or ""

    # 问题2优化: mission_history 从 state 读取（跨轮累积的摘要），
    # mission.log 仍作为完整证据源读取（限长防膨胀）
    mission_hist = state.get("mission_history", "") or ""
    log_text = read_mission_log(max_chars=20000)

    sys_prompt = (
        f"Role: {cfg['role']}\n"
        f"Goal: {cfg['goal']}\n"
        f"Backstory: {cfg['backstory']}\n"
        f"{COT_INSTRUCTION}\n"
        "你会收到任务描述、历史战况摘要（mission_history）和 mission.log 最新正文。\n"
        "mission.log 是权威证据源；mission_history 是先前轮次的摘要，供快速了解进展。\n"
        "你必须严格基于日志中出现的真实文本做判断，严禁编造未发生的发现。\n"
        "如果日志中包含 HTTP 响应、HTML、命令输出、报错、版本、框架指纹，"
        "不得说“日志为空”或“没有有效信息”，除非日志正文确实为空。"
    )

    task_prompt = (
        f"{task['description']}\n\n"
        f"目标: {state['target']}\n\n"
        "=== 历史战况摘要 (mission_history) 开始 ===\n"
        f"{mission_hist if mission_hist else '(首轮，暂无历史战况)'}\n"
        "=== 历史战况摘要结束 ===\n\n"
        "=== mission.log 最新正文开始 ===\n"
        f"{log_text}\n"
        "=== mission.log 最新正文结束 ===\n\n"
        "请结合以上历史摘要与最新日志：\n"
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
    return {**state, "strategy": result}


def deputy_node(state: PenTestState) -> PenTestState:
    cfg = agents_config["deputy"]
    task = tasks_config["deputy_task"]

    sys_prompt = f"Role: {cfg['role']}\nGoal: {cfg['goal']}\nBackstory: {cfg['backstory']}"
    task_prompt = f"战略意图: {state['strategy']}\n\n{task['description']}\n请输出技术需求："

    result = execute_agent_logic("DEPUTY", [], sys_prompt, task_prompt)
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

    return {**state, "execution_result": result}


def reporter_node(state: PenTestState) -> PenTestState:
    cfg = agents_config["reporter"]
    task = tasks_config["reporting_task"]
    log_path = config.LOG_FILE_PATH

    # 问题3: 使用带时间戳的报告文件名（由 GUI 预置到 state，或动态生成）
    md_path = state.get("report_md_path") or config.REPORT_FILE_PATH

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
        f"报告写入路径: {md_path}\n"
        "请先读取日志，再撰写报告并写入文件（写入上述报告路径）。"
    )

    result = execute_agent_logic("REPORTER", [file_read_tool, file_write_tool], sys_prompt, task_prompt, max_steps=6)
    return {**state, "final_report": result, "report_md_path": md_path}


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

    # 问题3: 使用带时间戳的报告文件名（与 reporter 对应）
    md_path = state.get("report_md_path") or config.REPORT_FILE_PATH
    html_path = state.get("report_html_path") or (
        str(Path(md_path).with_suffix(".html"))
    )

    sys_prompt = (
        f"{cfg['role']}\n{cfg['goal']}\n{COT_INSTRUCTION}\n"
        f"请在 HTML 中嵌入以下 CSS：\n{css_template}\n\n"
        "【安全要求】日志中捕获的目标 HTML/JS 内容（如 <script>、<img onerror> 等 XSS payload）"
        "必须转义为纯文本展示（如 &lt;script&gt;），严禁原样嵌入可执行标签。"
    )
    task_prompt = (
        task["description"]
        + "\n\n以下是报告基础内容：\n"
        + state.get("final_report", "")
        + f"\n\nMD 报告文件: {md_path}\nHTML 报告写入路径: {html_path}"
    )

    result = execute_agent_logic("HTML_REPORTER", [file_read_tool, file_write_tool], sys_prompt, task_prompt)
    return {**state, "final_html": result, "report_html_path": html_path}
