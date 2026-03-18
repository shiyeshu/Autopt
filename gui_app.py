import sys
import os
import re
import asyncio
from pathlib import Path
import base64
from datetime import datetime
from nicegui import ui
from dotenv import load_dotenv

THIS_FILE = Path(__file__).resolve()
TEAM_DIR = THIS_FILE.parent
PROJECT_ROOT = TEAM_DIR.parent

sys.path.insert(0, str(TEAM_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

env_path = PROJECT_ROOT / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)

os.environ.setdefault("OTEL_SDK_DISABLED", "true")

LOG_FILE_PATH = os.getenv("LOG_FILE_PATH", "./mission.log")
UI_LOG_FILE_PATH = os.getenv("UI_LOG_FILE_PATH", "./ui_mission.log")

from graph import create_assault_graph, create_reporting_graph


class ColoredLogDisplay:
    """支持整行着色、自动滚到底部、统一插入动画、thinking占位、稳定打字机效果"""

    def __init__(self, max_lines=1000):
        self.max_lines = max_lines
        self.log_rows = []
        self.pending_thought = None
        self.active_typing_tasks = set()

        with ui.scroll_area().classes(
            "w-full flex-grow bg-white overflow-y-auto"
        ) as self.scroll_area:
            self.container = ui.column().classes("w-full px-4 py-4 gap-3")

    async def _scroll_to_bottom(self):
        try:
            await asyncio.sleep(0.04)
            self.scroll_area.scroll_to(percent=1.0)
        except Exception:
            pass

    def _trim_rows_if_needed(self):
        while len(self.log_rows) > self.max_lines:
            oldest = self.log_rows.pop(0)
            try:
                oldest["row"].delete()
            except Exception:
                pass

    def _style_prefix(self, prefix_label, role: str):
        if role == "agent":
            prefix_label.style("color: #059669;")
        elif role == "thought":
            prefix_label.style("color: #7C3AED;")
        elif role == "tool":
            prefix_label.style("color: #D97706;")
        elif role == "result":
            prefix_label.style("color: #0D9488;")
        elif role == "error":
            prefix_label.style("color: #DC2626;")
        elif role == "exec":
            prefix_label.style("color: #111827;")
        else:
            prefix_label.style("color: #6B7280;")

    def _style_content(self, content_label, role: str):
        if role == "agent":
            content_label.style("color: #059669; font-weight: 500;")
        elif role == "thought":
            content_label.style("color: #7C3AED; font-style: italic;")
        elif role == "tool":
            content_label.style("color: #D97706; font-weight: 500;")
        elif role == "result":
            content_label.style("color: #0D9488; font-weight: 500;")
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
        with self.container:
            with ui.row().classes(f"w-full items-start gap-2 py-1 log-row-animate {extra_classes}".strip()) as row:
                prefix_label = None
                if prefix:
                    prefix_label = ui.label(prefix).classes(
                        "text-xs font-mono font-bold shrink-0 mt-[2px]"
                    )
                    self._style_prefix(prefix_label, role)

                content_label = ui.label(content).classes(
                    "text-sm font-mono whitespace-pre-wrap break-words flex-grow"
                )
                self._style_content(content_label, role)

        row_info = {
            "row": row,
            "prefix_label": prefix_label,
            "content_label": content_label,
            "role": role,
        }
        self.log_rows.append(row_info)
        self._trim_rows_if_needed()
        asyncio.create_task(self._scroll_to_bottom())
        return row_info

    def push(self, prefix: str, content: str, role: str):
        self._create_row(prefix, content, role)

    def show_thinking_placeholder(self):
        if self.pending_thought is not None:
            return

        with self.container:
            with ui.row().classes("w-full items-start gap-2 py-1 log-row-animate") as row:
                prefix_label = ui.label("[THOUGHT]").classes(
                    "text-xs font-mono font-bold shrink-0 mt-[2px]"
                )
                self._style_prefix(prefix_label, "thought")

                with ui.row().classes("items-center gap-0 flex-grow") as content_wrap:
                    text_label = ui.label("thinking").classes(
                        "text-sm font-mono italic"
                    )
                    text_label.style("color: #7C3AED;")

                    dots_label = ui.label(".").classes(
                        "text-sm font-mono italic"
                    )
                    dots_label.style("color: #7C3AED;")

        row_info = {
            "row": row,
            "prefix_label": prefix_label,
            "content_label": text_label,
            "dots_label": dots_label,
            "role": "thought",
            "dots_task": None,
        }

        self.log_rows.append(row_info)
        self._trim_rows_if_needed()
        self.pending_thought = row_info
        row_info["dots_task"] = asyncio.create_task(self._animate_thinking_dots(row_info))
        asyncio.create_task(self._scroll_to_bottom())


    async def _typewriter_update(self, row_info, content: str, chunk_size: int = 2, delay: float = 0.015):
        label = row_info["content_label"]
        label.classes(add="typewriter-caret")
        shown = ""

        try:
            for i in range(0, len(content), chunk_size):
                shown += content[i:i + chunk_size]
                label.set_text(shown)
                await asyncio.sleep(delay)
                await self._scroll_to_bottom()
        finally:
            try:
                label.classes(remove="typewriter-caret")
            except Exception:
                pass

    def replace_thinking_with_content(self, content: str):
        if self.pending_thought is not None:
            row_info = self.pending_thought
            self.pending_thought = None

            dots_task = row_info.get("dots_task")
            if dots_task and not dots_task.done():
                dots_task.cancel()

            dots_label = row_info.get("dots_label")
            if dots_label is not None:
                try:
                    dots_label.delete()
                except Exception:
                    pass
                row_info["dots_label"] = None

            label = row_info["content_label"]
            label.set_text("")
            self._style_content(label, "thought")
        else:
            row_info = self._create_row("[THOUGHT]", "", "thought")

        task = asyncio.create_task(self._safe_typewriter(row_info, content))
        self.active_typing_tasks.add(task)
        task.add_done_callback(lambda t: self.active_typing_tasks.discard(t))

    async def _animate_thinking_dots(self, row_info):
        dots = [".", "..", "...", "....", "....."]
        i = 0
        try:
            while self.pending_thought is row_info:
                label = row_info.get("dots_label")
                if label is not None:
                    label.set_text(dots[i % len(dots)])
                i += 1
                await asyncio.sleep(0.35)
        except asyncio.CancelledError:
            return
        except Exception:
            return

    async def _safe_typewriter(self, row_info, content: str):
        try:
            await self._typewriter_update(row_info, content)
        except asyncio.CancelledError:
            return
        except Exception:
            try:
                row_info["content_label"].set_text(content)
            except Exception:
                pass

    def clear(self):
        for task in list(self.active_typing_tasks):
            task.cancel()
        self.active_typing_tasks.clear()

        if self.pending_thought is not None:
            dots_task = self.pending_thought.get("dots_task")
            if dots_task and not dots_task.done():
                dots_task.cancel()

        self.pending_thought = None

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

        if role == "agent":
            self.log_element.push(prefix, content, role)

            trigger_keywords = [
                "已接管任务",
                "开始分析",
                "开始思考",
            ]
            if any(k in content for k in trigger_keywords):
                self.log_element.show_thinking_placeholder()
            return

        if role == "thought":
            self.log_element.replace_thinking_with_content(content)
            return

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


async def run_mission(
    target,
    max_rounds,
    poll_interval,
    log_manager,
    artifacts_card,
    md_btn,
    html_btn,
    start_btn,
    status_label,
    log_pause_btn,
):
    start_btn.disable()
    artifacts_card.visible = False
    md_btn.disable()
    html_btn.disable()

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

        for i in range(1, int(max_rounds) + 1):
            log_manager.push_message(f"🔥 Round {i}/{max_rounds} 开始")

            state = {
                "target": target,
                "mission_history": "",
                "strategy": "",
                "deputy_requirement": "",
                "operator_command": "",
                "execution_result": "",
                "log_result": "",
                "final_report": "",
                "final_html": "",
            }

            await asyncio.to_thread(assault_graph.invoke, state)
            log_manager.push_message(f"[RESULT] ✅ Round {i} 完成")

        log_manager.push_message("[AGENT] 📊 生成报告...")

        report_state = {
            "target": target,
            "mission_history": "",
            "strategy": "",
            "deputy_requirement": "",
            "operator_command": "",
            "execution_result": "",
            "log_result": "",
            "final_report": "",
            "final_html": "",
        }

        await asyncio.to_thread(reporting_graph.invoke, report_state)

        log_manager.push_message("🎉 任务完成！")

        if os.path.exists("final_report.md"):
            md_btn.enable()
            log_manager.push_message("[RESULT] ✅ MD 报告已生成")

        if os.path.exists("final_report.html"):
            html_btn.enable()
            log_manager.push_message("[RESULT] ✅ HTML 报告已生成")

        artifacts_card.visible = True
        ui.notify("Mission Complete!", type="positive")

    except Exception as e:
        log_manager.push_message(f"[ERROR] ❌ 任务失败: {str(e)}")
        ui.notify(f"Failed: {str(e)}", type="negative")

    finally:
        log_manager.stop_polling()
        start_btn.enable()
        status_label.set_text("● IDLE")
        status_label.style("color: #6B7280; font-size: 12px; font-weight: 700; letter-spacing: 0.08em;")


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
</style>
""")



    ui.colors(primary="#111827", secondary="#4B5563", accent="#10B981")

    with ui.header().classes(
        "bg-white text-gray-800 p-4 shadow-sm flex items-center gap-3 border-b border-gray-200"
    ):
        ui.icon("security", size="md", color="black")
        ui.label("AutoRedTeam Pro").classes("text-xl font-bold tracking-wider text-gray-900")

    with ui.row().classes(
        "w-full no-wrap p-4 gap-6 items-stretch h-[calc(100vh-80px)] bg-[#F8FAFC]"
    ):

        with ui.card().classes(
            "w-1/4 flex flex-col p-6 shadow-sm border border-gray-200 bg-white gap-6"
        ):
            ui.label("Mission Control").classes(
                "text-lg font-bold text-gray-800 border-b border-gray-200 pb-2"
            )

            target_input = ui.input(
                label="Target IP / URL",
                value="http://10.0.91.43",
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

            with ui.column().classes(
                "w-full gap-3 mt-auto pt-6 border-t border-gray-200"
            ) as artifacts_card:
                artifacts_card.visible = False
                ui.label("Mission Artifacts").classes("font-bold text-gray-700 text-sm")

                with ui.row().classes("w-full gap-2"):
                    md_btn = ui.button(
                        "Report.md",
                        icon="description",
                        on_click=lambda: download_local_file("final_report.md"),
                    ).props("outline color=grey-8").classes("flex-grow")

                    html_btn = ui.button(
                        "Report.html",
                        icon="html",
                        on_click=lambda: download_local_file("final_report.html"),
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
                await run_mission(
                    target=target_input.value,
                    max_rounds=int(rounds_input.value),
                    poll_interval=float(poll_interval_input.value or 2.0),
                    log_manager=log_manager,
                    artifacts_card=artifacts_card,
                    md_btn=md_btn,
                    html_btn=html_btn,
                    start_btn=start_btn,
                    status_label=status_label,
                    log_pause_btn=log_pause_btn,
                )

            start_btn.on_click(handle_start)

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title="AutoRedTeam Pro", port=8080, reload=False)
