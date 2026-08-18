import sys
import os
import re
import asyncio
from pathlib import Path
import base64
from datetime import datetime
from nicegui import ui
from dotenv import load_dotenv

from security import ensure_utf8_console, mission_control
from config import config

ensure_utf8_console()

THIS_FILE = Path(__file__).resolve()
TEAM_DIR = THIS_FILE.parent
PROJECT_ROOT = config.PROJECT_ROOT

sys.path.insert(0, str(TEAM_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

env_path = PROJECT_ROOT / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)

os.environ.setdefault("OTEL_SDK_DISABLED", "true")

LOG_FILE_PATH = config.LOG_FILE_PATH
UI_LOG_FILE_PATH = config.UI_LOG_FILE_PATH

from graph import create_assault_graph, create_reporting_graph


class ColoredLogDisplay:
    """支持整行着色、智能自动滚动、REPLY 组装/打字机、Markdown 渲染。

    着色策略: 按行为类型着色（而非按 agent）：
    - agent(行为/状态): 绿色   - thought(思考): 紫色
    - tool(工具调用): 橙色      - result(工具结果): 青色
    - reply(回复内容): 深蓝     - error(错误): 红色
    - exec(命令执行): 深灰底    - plain: 灰色
    """

    # 底部判定阈值：滚动百分比 >= 98% 视为在底部
    AUTO_SCROLL_THRESHOLD = 0.98

    def __init__(self, max_lines=1000):
        self.max_lines = max_lines
        self.log_rows = []
        self._at_bottom = True  # 初始视为在底部（自动跟随）
        self._current_reply = None  # 当前正在组装的 REPLY 行
        self._scroll_task = None  # 串行化滚动任务，避免并发标记竞争

        with ui.scroll_area().classes(
            "w-full flex-grow bg-white overflow-y-auto"
        ) as self.scroll_area:
            self.container = ui.column().classes("w-full px-4 py-4 gap-3")

        # 监听用户滚动：根据滚动百分比维护"是否在底部"状态
        self.scroll_area.on_scroll(self._on_scroll)

    def _on_scroll(self, e):
        """用户滚动时更新底部状态（含程序滚动，统一以百分比判断）。"""
        try:
            pct = getattr(e, "vertical_percentage", None)
            if pct is None:
                return
            # 百分比接近 1.0 表示滚动到最底部；用户上翻则停止跟随
            self._at_bottom = pct >= self.AUTO_SCROLL_THRESHOLD
        except Exception:
            pass

    async def _scroll_to_bottom(self):
        try:
            await asyncio.sleep(0.04)
            # 仅在用户位于底部时才自动跟随
            if self._at_bottom:
                self.scroll_area.scroll_to(percent=1.0)
        except Exception:
            pass

    def _schedule_scroll(self):
        """串行化滚动：取消上一个未完成的滚动任务，避免并发竞争。"""
        if self._scroll_task and not self._scroll_task.done():
            self._scroll_task.cancel()
        self._scroll_task = asyncio.create_task(self._scroll_to_bottom())

    def _trim_rows_if_needed(self):
        while len(self.log_rows) > self.max_lines:
            oldest = self.log_rows.pop(0)
            try:
                oldest["row"].delete()
            except Exception:
                pass

    def _style_content(self, content_label, role: str):
        """按行为类型着色（深色系，保证可读）。"""
        if role == "agent":
            content_label.style("color: #059669; font-weight: 500;")
        elif role == "thought":
            content_label.style("color: #7C3AED; font-style: italic;")
        elif role == "tool":
            content_label.style("color: #D97706; font-weight: 500;")
        elif role == "result":
            content_label.style("color: #0D9488; font-weight: 500;")
        elif role == "reply":
            # 回复内容：深蓝，通过 CSS 变量作用于 markdown 正文
            content_label.style("--md-body-color: #1D4ED8;")
            content_label.style("color: #1D4ED8;")
        elif role == "error":
            content_label.style("color: #DC2626; font-weight: 600;")
        elif role == "exec":
            content_label.style(
                "color: #111827; "
                "background-color: #F3F4F6; "
                "padding: 4px 8px; "
                "border-radius: 6px; "
                "border: 1px solid #E5E7EB; "
                "display: inline-block;"
            )
        else:
            content_label.style("color: #374151;")

    def _create_row(self, prefix: str, content: str, role: str, extra_classes: str = ""):
        # prefix 与内容合成单条文本，避免两行显示
        display_text = f"{prefix} {content}" if prefix else content

        with self.container:
            with ui.row().classes(f"w-full items-start gap-2 py-1 log-row-animate {extra_classes}".strip()) as row:
                # REPLY 行用 Markdown 渲染正文（push 层已剥离 "💬 角色名 返回:" 前缀）
                if prefix == "[REPLY]":
                    content_label = ui.markdown(content).classes(
                        "text-sm flex-grow markdown-body"
                    )
                else:
                    content_label = ui.label(display_text).classes(
                        "text-sm font-mono whitespace-pre-wrap break-words flex-grow"
                    )
                self._style_content(content_label, role)

        row_info = {
            "row": row,
            "prefix_label": None,
            "content_label": content_label,
            "role": role,
        }
        self.log_rows.append(row_info)
        self._trim_rows_if_needed()
        self._schedule_scroll()
        return row_info

    @staticmethod
    def _strip_reply_prefix(content: str) -> str:
        """剥离 REPLY 行中的 '💬 角色名 返回:' 前缀，仅保留正文。

        流式分段会带前缀（agents.py 按 0.3s/200字符 flush），
        每条进入时独立剥离；多行时逐行处理。
        """
        lines = content.split("\n")
        cleaned = []
        for line in lines:
            m = re.search(r"💬\s*[\u4e00-\u9fff]+\s*返回:\s*(.*)", line, re.DOTALL)
            if m:
                cleaned.append(m.group(1))
            else:
                cleaned.append(line)
        return "\n".join(cleaned).strip()

    @staticmethod
    def _strip_md_headings(text: str) -> str:
        """剥离 Markdown 标题语法（#/##/###），避免终端显示大字标题。
        仅处理行首的标题标记，保留其余 Markdown（加粗/列表/代码块）。"""
        lines = text.split("\n")
        cleaned = []
        for line in lines:
            # 行首 1-6 个 # 后跟空格 → 去掉 # 号
            cleaned.append(re.sub(r"^#{1,6}\s+", "", line))
        return "\n".join(cleaned)

    def push(self, prefix: str, content: str, role: str):
        # 优化1: 连续 [REPLY] 行合并到同一行（打字机式追加）
        if prefix == "[REPLY]":
            # 解码换行标记(⏎→\n)，保留结构；再剥离前缀与标题语法
            piece = self._strip_reply_prefix(content).replace("⏎", "\n")
            piece = self._strip_md_headings(piece)
            cur = self._current_reply
            if cur is not None:
                # 追加到现有 REPLY 行：前一段末尾或本段开头有换行则换行拼接，
                # 否则空格拼接（流式碎片连句）
                try:
                    prev_body = cur.get("md_body", "")
                    if prev_body.endswith("\n") or piece.startswith("\n"):
                        merged_body = prev_body + piece
                    else:
                        merged_body = (prev_body + " " + piece) if prev_body else piece
                    cur["md_body"] = merged_body
                    cur["content_label"].set_content(merged_body)
                    self._schedule_scroll()
                    return
                except Exception:
                    pass
            # 无当前 REPLY 行 → 新建（reply 行为色，Markdown 渲染）
            row_info = self._create_row(prefix, piece, "reply")
            row_info["md_body"] = piece
            self._current_reply = row_info
            return

        # 非 REPLY 行：关闭当前 REPLY 组装；其余行也解码换行标记保持可读
        self._current_reply = None
        if "⏎" in content:
            content = content.replace("⏎", "\n")
        self._create_row(prefix, content, role)

    def clear(self):
        self._current_reply = None
        for row_info in self.log_rows:
            try:
                row_info["row"].delete()
            except Exception:
                pass
        self.log_rows.clear()




class LogPollingManager:
    def __init__(self, log_element, log_file_path: str, poll_interval: float = 2.0):
        self.log_element = log_element
        self.log_file_path = log_file_path
        self.poll_interval = poll_interval
        self.last_position = 0
        self.last_size = 0
        self.is_polling = False
        self.is_paused = False

    def _clean_ansi(self, text: str) -> str:
        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        box_chars = ["│", "┌", "┐", "└", "┘", "─", "┤", "├", "┴", "┬"]
        clean_msg = ansi_escape.sub("", text)
        for ch in box_chars:
            clean_msg = clean_msg.replace(ch, "")
        return clean_msg.strip()

    def _should_filter_line(self, clean_msg: str) -> bool:
        if not clean_msg:
            return True

        debug_keywords = [
            "[SYSTEM TOOL]",
            "[DEBUG]",
            "SYSTEM TOOL:",
            "DEBUG:",
            "完整内容：",
        ]
        if any(k in clean_msg for k in debug_keywords):
            return True

        if "文件读取成功" in clean_msg and "文件大小:" in clean_msg:
            return True

        if "工具 [file_read_tool] 执行结果:" in clean_msg and "文件读取成功" in clean_msg:
            return True

        return False

    def _parse_line(self, line: str):
        if not line or not line.strip():
            return None

        clean_msg = self._clean_ansi(line)
        if not clean_msg:
            return None

        if self._should_filter_line(clean_msg):
            return None

        prefix = ""
        role = "plain"
        content = clean_msg

        if "[AGENT]" in clean_msg:
            prefix = "[AGENT]"
            role = "agent"
            content = clean_msg.split("[AGENT]", 1)[-1].strip()

        elif "[THOUGHT]" in clean_msg:
            prefix = "[THOUGHT]"
            role = "thought"
            content = clean_msg.split("[THOUGHT]", 1)[-1].strip()

        elif "[REPLY]" in clean_msg:
            prefix = "[REPLY]"
            role = "reply"
            content = clean_msg.split("[REPLY]", 1)[-1].strip()

        elif "[TOOL]" in clean_msg:
            prefix = "[TOOL]"
            role = "tool"
            content = clean_msg.split("[TOOL]", 1)[-1].strip()

        elif "[RESULT]" in clean_msg:
            prefix = "[RESULT]"
            role = "result"
            content = clean_msg.split("[RESULT]", 1)[-1].strip()

        elif "[ERROR]" in clean_msg:
            prefix = "[ERROR]"
            role = "error"
            content = clean_msg.split("[ERROR]", 1)[-1].strip()

        elif "Thought:" in clean_msg:
            prefix = "[THOUGHT]"
            role = "thought"
            content = clean_msg.replace("Thought:", "", 1).strip()

        elif "CMD:" in clean_msg:
            prefix = "[EXEC]"
            role = "exec"
            content = clean_msg.replace("CMD:", "", 1).strip()

        elif "Error:" in clean_msg or "ERROR:" in clean_msg:
            prefix = "[ERROR]"
            role = "error"
            content = clean_msg.replace("Error:", "").replace("ERROR:", "").strip()

        if not content:
            return None

        return prefix, content, role

    def push_message(self, message: str):
        if self.is_paused:
            return

        parsed = self._parse_line(message)
        if not parsed:
            return

        prefix, content, role = parsed

        # 问题2修复：所有消息一律按读取顺序直接渲染，
        # 不做 thinking 占位/替换，保证严格线性输出
        self.log_element.push(prefix, content, role)


    def read_new_logs(self) -> str:
        try:
            if not os.path.exists(self.log_file_path):
                return ""

            current_size = os.path.getsize(self.log_file_path)

            if current_size < self.last_size:
                self.last_position = 0
                self.last_size = 0

            if current_size == self.last_size:
                return ""

            with open(self.log_file_path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self.last_position)
                new_content = f.read()
                self.last_position = f.tell()

            self.last_size = current_size

            new_content = re.sub(r"\(文件大小:\s*\d+\s*字符\)\s*完整内容：?", "", new_content)
            return new_content

        except Exception as e:
            return f"[ERROR] 轮询错误: {str(e)}"

    async def start_polling(self):
        if self.is_polling:
            return

        self.is_polling = True
        self.last_position = 0
        self.last_size = 0

        while self.is_polling:
            try:
                if not self.is_paused:
                    new_logs = self.read_new_logs()
                    if new_logs:
                        for line in new_logs.split("\n"):
                            self.push_message(line)

                await asyncio.sleep(self.poll_interval)

            except Exception as e:
                if not self.is_paused:
                    parsed = self.push_message(f"[ERROR] 轮询异常: {str(e)}")
                    if parsed:
                        prefix, content, role = parsed
                        self.log_element.push(prefix, content, role)
                await asyncio.sleep(self.poll_interval * 2)

    def stop_polling(self):
        self.is_polling = False

    def reset(self):
        self.last_position = 0
        self.last_size = 0
        self.is_paused = False


def download_local_file(filename: str):
    if not os.path.exists(filename):
        ui.notify(f"File {filename} not found!", type="negative")
        return
    try:
        with open(filename, "rb") as f:
            content_bytes = f.read()
        b64_content = base64.b64encode(content_bytes).decode("ascii")
        ui.run_javascript(f"""
            const bytes = Uint8Array.from(atob('{b64_content}'), c => c.charCodeAt(0));
            const blob = new Blob([bytes], {{type: 'text/plain;charset=utf-8'}});
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = '{filename}';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        """)
    except Exception as e:
        ui.notify(f"Download failed: {str(e)}", type="negative")


import re

ASSET_PATTERNS = {
    "ip": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "url": re.compile(r"https?://[^\s\"'<>]+"),
    "domain": re.compile(r"\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}\b"),
    "port": re.compile(r"\b(\d{1,5})/(?:tcp|udp)\b"),
    "vuln": re.compile(r"(CVE-\d{4}-\d{4,7})", re.IGNORECASE),
}


def _extract_assets_to_project(project_id, task_id, text: str):
    """从执行结果文本中提取资产（IP/URL/域名/端口/漏洞）并存入项目。"""
    import database as db
    if not project_id or not text:
        return
    text = str(text)
    # 端口: "80/tcp" 格式
    for m in ASSET_PATTERNS["port"].finditer(text):
        db.add_asset(project_id, "port", m.group(0), source="任务执行", task_id=task_id)
    # 漏洞 CVE
    for m in ASSET_PATTERNS["vuln"].finditer(text):
        db.add_asset(project_id, "vuln", m.group(1).upper(), source="任务执行", task_id=task_id)
    # URL
    for m in ASSET_PATTERNS["url"].finditer(text):
        db.add_asset(project_id, "url", m.group(0)[:200], source="任务执行", task_id=task_id)
    # IP（排除常见假 IP）
    seen = set()
    for m in ASSET_PATTERNS["ip"].finditer(text):
        ip = m.group(0)
        if ip.startswith(("127.", "0.", "255.", "169.254")):
            continue
        if ip in seen:
            continue
        seen.add(ip)
        db.add_asset(project_id, "ip", ip, source="任务执行", task_id=task_id)
    # 域名（排除 IP 和 URL 主机部分重复）
    for m in ASSET_PATTERNS["domain"].finditer(text):
        d = m.group(0).lower()
        if d.endswith((".com", ".net", ".org", ".cn", ".io", ".xyz", ".info", ".top", ".site", ".cc", ".me")) or "." in d:
            if not re.match(r"^\d+\.\d+\.\d+\.\d+$", d):
                db.add_asset(project_id, "domain", d[:200], source="任务执行", task_id=task_id)


async def run_mission(
    target,
    max_rounds,
    poll_interval,
    log_manager,
    artifacts_card,
    md_btn,
    html_btn,
    start_btn,
    stop_btn,
    status_label,
    log_pause_btn,
    spinner,
    current_report,
    project_id=None,
    task_title="",
):
    start_btn.disable()
    stop_btn.enable()
    spinner.visible = True
    artifacts_card.visible = False
    md_btn.disable()
    html_btn.disable()

    # 模块3/5: 创建任务记录（历史会话/项目归属）
    import database as db
    task_id = None
    try:
        task_id = db.create_task(project_id, task_title or target, target, max_rounds=int(max_rounds))
        db.add_message(task_id, "user", f"目标: {target}，最大轮次: {max_rounds}")
    except Exception:
        pass

    # 功能优化5: 创建任务专属工作目录（AI 产生的文件统一存放）
    try:
        import re as _re
        from datetime import datetime as _dt
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        safe_target = _re.sub(r"[^A-Za-z0-9_.-]+", "_", str(target)).strip("_")[:40] or "target"
        task_dir = config.PROJECT_ROOT / "task_workspace" / f"{ts}_{safe_target}"
        task_dir.mkdir(parents=True, exist_ok=True)
        os.environ["TASK_WORK_DIR"] = str(task_dir)
        log_manager.push_message(f"[INFO] 📁 任务工作目录: {task_dir.name}")
        # 提示 AI 文件统一存放（通过全局上下文）
        import agents as _agents
        _agents.TASK_WORK_DIR = str(task_dir)
        _agents.set_task_work_dir(str(task_dir))
    except Exception:
        pass

    # 架构4: 重置停止标记
    mission_control.reset()

    log_manager.stop_polling()
    log_manager.reset()
    log_manager.poll_interval = float(poll_interval or 2.0)
    log_manager.log_element.clear()

    status_label.set_text("● LIVE")
    status_label.style("color: #059669; font-size: 12px; font-weight: 700; letter-spacing: 0.08em;")
    log_pause_btn.text = "暂停日志"
    log_pause_btn.props("icon=pause_circle")
    log_pause_btn.style("color: #92400E; font-weight: 700;")

    os.makedirs(os.path.dirname(LOG_FILE_PATH) if os.path.dirname(LOG_FILE_PATH) else ".", exist_ok=True)

    with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
        f.write(f"=== 任务启动 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        f.write(f"目标: {target}\n")
        f.write(f"最大轮次数: {max_rounds}\n")
        f.write("=" * 80 + "\n\n")

    with open(UI_LOG_FILE_PATH, "w", encoding="utf-8") as f:
        f.write(f"=== UI 日志启动 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        f.write(f"目标: {target}\n")
        f.write("=" * 80 + "\n\n")

    log_manager.push_message(f"🚀 任务启动 - 目标: {target}")
    asyncio.create_task(log_manager.start_polling())

    try:
        if not os.environ.get("OPENAI_API_KEY") and not any(
            os.environ.get(f"{role}_API_KEY")
            for role in ["STRATEGIST", "DEPUTY", "OPERATOR", "AUDITOR"]
        ):
            raise ValueError("API Keys not found!")

        assault_graph = create_assault_graph()
        reporting_graph = create_reporting_graph()

        # 问题2优化: mission_history 跨轮累积（保留历史战况摘要），
        # 每轮只清空本轮输出字段，strategist 可直接读 state 而非只依赖文件
        mission_history = ""

        for i in range(1, int(max_rounds) + 1):
            # 架构4: 停止检查
            if mission_control.stopped:
                log_manager.push_message("[ERROR] ⏹ 用户请求停止，终止任务")
                break

            log_manager.push_message(f"🔥 Round {i}/{max_rounds} 开始")

            state = {
                "target": target,
                "mission_history": mission_history,
                "strategy": "",
                "deputy_requirement": "",
                "operator_command": "",
                "execution_result": "",
                "final_report": "",
                "final_html": "",
            }

            round_state = await asyncio.to_thread(assault_graph.invoke, state)

            # 跨轮累积: 把本轮执行结果摘要追加进 mission_history
            if round_state.get("execution_result"):
                result_summary = str(round_state["execution_result"]).strip()
                if result_summary and result_summary != "无输出":
                    mission_history += (
                        f"\n[Round {i} 执行结果]\n{result_summary[:2000]}"
                    )
                    mission_history = mission_history[-10000:]  # 限制历史长度防上下文膨胀

            # 模块5(修复): 每轮保存完整上下文到历史（策略/命令/执行结果），
            # 供历史查看与 RAG 收录（不再只存截断的执行结果）
            try:
                if task_id:
                    round_parts = []
                    if round_state.get("strategy"):
                        round_parts.append(f"策略: {round_state['strategy'][:800]}")
                    if round_state.get("deputy_requirement"):
                        round_parts.append(f"需求: {round_state['deputy_requirement'][:500]}")
                    if round_state.get("operator_command"):
                        round_parts.append(f"命令: {round_state['operator_command'][:800]}")
                    if round_state.get("execution_result"):
                        round_parts.append(f"结果: {str(round_state['execution_result'])[:2000]}")
                    if round_parts:
                        db.add_message(task_id, "assistant", "\n".join(round_parts), agent_name="任务轮次", round=i)
                        db.update_task(task_id, current_round=i)
                    # 模块4: 提取资产（从完整结果而非截断）
                    _extract_assets_to_project(project_id, task_id, str(round_state.get("execution_result", "")))
            except Exception:
                pass

            log_manager.push_message(f"[RESULT] ✅ Round {i} 完成")

        if mission_control.stopped:
            log_manager.push_message("[ERROR] ⏹ 任务已停止，跳过报告生成")
        else:
            log_manager.push_message("[AGENT] 📊 生成报告...")

            # 问题3: 生成带时间戳的报告文件名，避免多轮任务覆盖同名报告
            report_md_path, report_html_path = config.get_timestamped_report_paths(target)
            log_manager.push_message(f"[INFO] 📄 报告将保存为: {os.path.basename(report_md_path)}")

            report_state = {
                "target": target,
                "mission_history": "",
                "strategy": "",
                "deputy_requirement": "",
                "operator_command": "",
                "execution_result": "",
                "final_report": "",
                "final_html": "",
                "report_md_path": report_md_path,
                "report_html_path": report_html_path,
            }

            await asyncio.to_thread(reporting_graph.invoke, report_state)

            log_manager.push_message("🎉 任务完成！")

            if os.path.exists(report_md_path):
                md_btn.enable()
                current_report["md"] = report_md_path
                log_manager.push_message(f"[RESULT] ✅ MD 报告已生成: {os.path.basename(report_md_path)}")

            if os.path.exists(report_html_path):
                html_btn.enable()
                current_report["html"] = report_html_path
                log_manager.push_message(f"[RESULT] ✅ HTML 报告已生成: {os.path.basename(report_html_path)}")

            artifacts_card.visible = True
            ui.notify("Mission Complete!", type="positive")

    except Exception as e:
        log_manager.push_message(f"[ERROR] ❌ 任务失败: {str(e)}")
        ui.notify(f"Failed: {str(e)}", type="negative")

    finally:
        log_manager.stop_polling()
        spinner.visible = False
        start_btn.enable()
        stop_btn.disable()
        status_label.set_text("● IDLE")
        status_label.style("color: #6B7280; font-size: 12px; font-weight: 700; letter-spacing: 0.08em;")
        # 模块5: 更新任务状态（历史会话用）
        try:
            if task_id:
                status = "stopped" if mission_control.stopped else "completed"
                db.update_task(task_id, status=status)
        except Exception:
            pass


@ui.page("/")

def main_page():
    ui.add_head_html("""
<style>
@keyframes logFadeInUp {
    from {
        opacity: 0;
        transform: translateY(8px);
    }
    to {
        opacity: 1;
        transform: translateY(0);
    }
}
.log-row-animate {
    animation: logFadeInUp 0.28s ease-out;
}

.thinking-placeholder .q-label {
    color: #7C3AED !important;
    font-style: italic;
}

@keyframes thinkingPulse {
    0% { opacity: 0.35; }
    50% { opacity: 1; }
    100% { opacity: 0.35; }
}
.thinking-placeholder .q-label:last-child::after {
    content: '.....';
    display: inline-block;
    margin-left: 2px;
    animation: thinkingPulse 1.1s infinite ease-in-out;
}

@keyframes caretBlink {
    0%, 45% { opacity: 1; }
    50%, 100% { opacity: 0; }
}
.typewriter-caret::after {
    content: '|';
    margin-left: 2px;
    animation: caretBlink 0.9s infinite;
    color: #7C3AED;
}

/* 打字机光标 */
.typewriter-caret::after {
    content: '|';
    margin-left: 2px;
    animation: caretBlink 0.9s infinite;
    color: #7C3AED;
}

/* Markdown 渲染样式（REPLY 内容） */
.markdown-body {
    color: var(--md-body-color, #111827);  /* 角色色优先，默认深灰 */
    line-height: 1.6;
    font-size: 0.9rem;
}
.markdown-body p { margin: 4px 0; }
.markdown-body code {
    background: #F3F4F6;
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 0.85em;
    color: #B91C1C;  /* 行内代码深红，浅底深字 */
}
.markdown-body pre {
    background: #F8FAFC;  /* 浅色底，深色字，保证可读 */
    color: #1F2937;
    border: 1px solid #E5E7EB;
    padding: 8px 12px;
    border-radius: 6px;
    overflow-x: auto;
    margin: 6px 0;
}
.markdown-body pre code {
    background: transparent;
    padding: 0;
    color: inherit;  /* 继承 pre 的深色文字 */
    font-size: 0.85em;
}
.markdown-body table { border-collapse: collapse; margin: 6px 0; }
.markdown-body th, .markdown-body td {
    border: 1px solid #D1D5DB;
    padding: 4px 8px;
}
.markdown-body ul, .markdown-body ol { margin: 4px 0; padding-left: 20px; }
.markdown-body blockquote {
    border-left: 3px solid #D1D5DB;
    margin: 4px 0;
    padding-left: 8px;
    color: #4B5563;
}
</style>
""")



    ui.colors(primary="#111827", secondary="#4B5563", accent="#10B981")

    # 导航（复用 gui_pages）
    from gui_pages import _nav_header
    _nav_header("")

    with ui.row().classes(
        "w-full no-wrap p-4 gap-6 items-stretch h-[calc(100vh-80px)] bg-[#F8FAFC]"
    ):

        with ui.card().classes(
            "w-1/4 flex flex-col p-6 shadow-sm border border-gray-200 bg-white gap-6"
        ):
            ui.label("Mission Control").classes(
                "text-lg font-bold text-gray-800 border-b border-gray-200 pb-2"
            )

            # 模块3: 项目选择（问题1修复: 默认选中"临时项目"，所有临时数据归入同一项目）
            import database as _db
            # 确保存在固定的临时项目（所有不指定项目的任务都归入这里）
            TEMP_PROJECT_ID = _db.get_or_create_project("临时项目")
            projects = _db.list_projects()
            project_options = {TEMP_PROJECT_ID: "🕐 临时项目（默认）"}
            for p in projects:
                if p["id"] != TEMP_PROJECT_ID:
                    project_options[p["id"]] = p["name"]
            # UI问题2: 下拉框末尾加"创建新项目"虚拟选项
            project_options["__new__"] = "➕ 创建新项目..."
            project_select = ui.select(
                project_options, label="所属项目", value=TEMP_PROJECT_ID
            ).props("outlined dense color=black").classes("w-full")

            def on_project_change():
                # 选中"创建新项目"时弹出输入框
                if project_select.value == "__new__":
                    with ui.dialog() as dialog, ui.card():
                        ui.label("创建新项目").classes("font-bold")
                        new_name = ui.input("项目名称").classes("w-64")

                        def do_create():
                            name = new_name.value.strip()
                            if name:
                                pid = _db.get_or_create_project(name)
                                project_options[pid] = name
                                project_select.options = project_options
                                project_select.value = pid
                                dialog.close()
                                ui.notify(f"项目 '{name}' 已创建并选中", type="positive")
                            else:
                                ui.notify("项目名称不能为空", type="warning")

                        with ui.row().classes("gap-2"):
                            ui.button("创建", on_click=do_create).props("color=primary")
                            ui.button("取消", on_click=lambda: (dialog.close(), setattr(project_select, "value", TEMP_PROJECT_ID))).props("flat")
                    dialog.open()

            project_select.on_value_change(on_project_change)

            target_input = ui.input(
                label="Target IP / URL",
                value="",
                placeholder="例如: http://192.168.1.1 或 10.0.0.5",
            ).props("outlined dense color=black").classes("w-full")

            rounds_input = ui.number(
                label="Attack Rounds",
                value=3,
                min=1,
                max=1000,
            ).props("outlined dense color=black").classes("w-full")

            poll_interval_input = ui.number(
                label="Log Poll Interval (s)",
                value=2.0,
                min=0.5,
                max=10.0,
                step=0.5,
            ).props("outlined dense color=black").classes("w-full")

            start_btn = ui.button(
                "START OPERATION",
                icon="rocket_launch",
            ).classes(
                "w-full h-12 text-lg shadow-sm bg-gray-900 text-white font-bold tracking-wide"
            )

            # 架构4: 停止按钮（初始禁用）
            stop_btn = ui.button(
                "STOP",
                icon="stop",
            ).classes(
                "w-full h-12 text-lg shadow-sm bg-red-600 text-white font-bold tracking-wide"
            )
            stop_btn.disable()

            with ui.column().classes(
                "w-full gap-3 mt-auto pt-6 border-t border-gray-200"
            ) as artifacts_card:
                artifacts_card.visible = False
                ui.label("Mission Artifacts").classes("font-bold text-gray-700 text-sm")

                with ui.row().classes("w-full gap-2"):
                    # 问题3: 下载按钮动态指向最新生成的报告（时间戳命名）
                    current_report = {"md": "", "html": ""}

                    md_btn = ui.button(
                        "Report.md",
                        icon="description",
                        on_click=lambda: download_local_file(current_report["md"] or "final_report.md"),
                    ).props("outline color=grey-8").classes("flex-grow")

                    html_btn = ui.button(
                        "Report.html",
                        icon="html",
                        on_click=lambda: download_local_file(current_report["html"] or "final_report.html"),
                    ).props("outline color=grey-8").classes("flex-grow")

                md_btn.disable()
                html_btn.disable()

        with ui.card().classes(
            "w-3/4 flex flex-col p-0 shadow-sm border border-gray-200 bg-white overflow-hidden"
        ):
            with ui.row().classes(
                "w-full bg-gray-50 p-2 px-4 items-center justify-between border-b border-gray-200"
            ):
                with ui.row().classes("gap-2 items-center"):
                    ui.icon("terminal", color="black", size="sm")
                    ui.label("Execution Log").classes(
                        "text-gray-800 font-mono text-sm font-bold"
                    )

                with ui.row().classes("gap-3 items-center"):
                    status_label = ui.label("● IDLE")
                    status_label.style(
                        "color: #6B7280; font-size: 12px; font-weight: 700; letter-spacing: 0.08em;"
                    )

                    # 问题1修复: 运行中 spinner，证明程序仍在工作（API 等待期可见）
                    spinner = ui.spinner(size="lg")
                    spinner.visible = False

                    log_pause_btn = ui.button(
                        "暂停日志",
                        icon="pause_circle",
                    ).props("flat dense")
                    log_pause_btn.style("color: #92400E; font-weight: 700;")

            log_display = ColoredLogDisplay(max_lines=1000)

            log_manager = LogPollingManager(
                log_element=log_display,
                log_file_path=UI_LOG_FILE_PATH if UI_LOG_FILE_PATH else LOG_FILE_PATH,
                poll_interval=2.0,
            )

            def toggle_log_pause():
                log_manager.is_paused = not log_manager.is_paused

                if log_manager.is_paused:
                    log_pause_btn.text = "恢复日志"
                    log_pause_btn.props("icon=play_circle")
                    log_pause_btn.style("color: #065F46; font-weight: 700;")
                    status_label.set_text("⏸ LOG PAUSED")
                    status_label.style(
                        "color: #B45309; font-size: 12px; font-weight: 700; letter-spacing: 0.08em;"
                    )
                else:
                    # 恢复后不需要手工补读，轮询线程会从 last_position 继续读取暂停期间累积的日志
                    log_pause_btn.text = "暂停日志"
                    log_pause_btn.props("icon=pause_circle")
                    log_pause_btn.style("color: #92400E; font-weight: 700;")
                    status_label.set_text("● LIVE")
                    status_label.style(
                        "color: #059669; font-size: 12px; font-weight: 700; letter-spacing: 0.08em;"
                    )

            log_pause_btn.on_click(toggle_log_pause)

            async def handle_start():
                # 模块3/5: 任务归属项目（默认归入"临时项目"）
                try:
                    val = project_select.value
                    if val == "__new__" or not val:
                        proj_id = TEMP_PROJECT_ID
                    else:
                        proj_id = int(val)
                except (TypeError, ValueError):
                    proj_id = TEMP_PROJECT_ID
                title = f"{target_input.value} ({datetime.now().strftime('%H:%M:%S')})"
                await run_mission(
                    target=target_input.value,
                    max_rounds=int(rounds_input.value),
                    poll_interval=float(poll_interval_input.value or 2.0),
                    log_manager=log_manager,
                    artifacts_card=artifacts_card,
                    md_btn=md_btn,
                    html_btn=html_btn,
                    start_btn=start_btn,
                    stop_btn=stop_btn,
                    status_label=status_label,
                    log_pause_btn=log_pause_btn,
                    spinner=spinner,
                    current_report=current_report,
                    project_id=proj_id,
                    task_title=title,
                )

            def handle_stop():
                # 架构4: 请求停止（终止所有运行中的子进程 + 置位停止标记）
                mission_control.request_stop()
                stop_btn.disable()
                ui.notify("正在停止...", type="warning")

            start_btn.on_click(handle_start)
            stop_btn.on_click(handle_stop)

    # UI问题3: 插话悬浮面板（优化: 与终端框同宽、水平居中，气泡在右下角）
    # 面板固定居中显示在页面底部区域（宽度与终端 w-3/4 一致）
    with ui.row().classes(
        "fixed bottom-6 left-1/2 -translate-x-1/2 items-center gap-3 z-50 w-3/4"
    ) as interrupt_wrap:
        interrupt_wrap.visible = False
        with ui.column().classes(
            "flex-grow bg-white border border-gray-200 rounded-xl shadow-lg p-4 gap-2"
        ) as interrupt_panel:
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("插入需求").classes("text-sm font-bold text-gray-700")
                ui.button("✕", on_click=lambda: toggle_interrupt_panel(False)).props("flat dense size=sm")

            interrupt_input = ui.textarea(
                placeholder="例如: 额外扫描 8080 端口\n任务运行中随时插入，下个 Agent 执行时生效",
            ).props("outlined rows=3").classes("w-full")
            with ui.row().classes("w-full items-center justify-end gap-2"):
                send_btn = ui.button(
                    "发送插话", icon="send"
                ).props("color=primary dense")

                def do_send():
                    text = interrupt_input.value
                    if text and text.strip():
                        import agents as _agents
                        _agents.add_interrupt_request(text.strip())
                        interrupt_input.value = ""
                        ui.notify("已插入需求，将在下一个 Agent 执行时生效", type="positive")
                        toggle_interrupt_panel(False)
                    else:
                        ui.notify("请输入要插入的需求", type="warning")

                send_btn.on_click(do_send)

    # 右下角悬浮气泡按钮（独立于面板，始终可见）
    with ui.row().classes("fixed bottom-4 right-4 z-50") as bubble_area:
        bubble_btn = ui.button(
            "", icon="chat_bubble"
        ).props("round color=primary size=lg").classes("shadow-lg")
        bubble_btn.style("width: 56px; height: 56px;")

        def toggle_interrupt_panel(show: bool | None = None):
            target = not interrupt_wrap.visible if show is None else show
            interrupt_wrap.visible = target
            bubble_btn.set_visibility(not target)

        bubble_btn.on_click(lambda: toggle_interrupt_panel())

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title="AutoPT", port=8080, reload=False)
