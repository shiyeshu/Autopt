# team/gui_app.py
import sys
import os
import re
import asyncio
from pathlib import Path
import base64
import time
from datetime import datetime
from nicegui import ui
from dotenv import load_dotenv

# ========== 1) 路径与环境变量 ==========
THIS_FILE = Path(__file__).resolve()
TEAM_DIR = THIS_FILE.parent
PROJECT_ROOT = TEAM_DIR.parent

# 确保能 import project_root 下的模块（如 config.py），以及 team 下的 graph.py/tools/
sys.path.insert(0, str(TEAM_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

# 加载 .env（位于项目根目录）
env_path = PROJECT_ROOT / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)

# 兼容：如果你还在用某些遥测/追踪相关包，可选择禁用（不强制）
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

LOG_FILE_PATH = os.getenv("LOG_FILE_PATH", r"./mission.log")

# ========== 2) 导入 LangGraph ==========
# 注意：graph.py 在 team/ 目录内，所以可直接 import graph
from graph import create_assault_graph, create_reporting_graph


# ========== 3) 日志轮询管理器 ==========
class LogPollingManager:
    """轮询式日志管理器，替代实时推送"""

    def __init__(self, log_element, log_file_path: str, poll_interval: float = 2.0):
        self.log_element = log_element
        self.log_file_path = log_file_path
        self.poll_interval = poll_interval
        self.last_position = 0
        self.last_size = 0
        self.is_polling = False
        self.polling_task = None

    def _clean_ansi(self, text: str) -> str:
        """移除 ANSI 转义码和边框字符"""
        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        box_chars = ["│", "┌", "┐", "└", "┘", "─", "┤", "├", "┴", "┬"]

        clean_msg = ansi_escape.sub("", text)
        for ch in box_chars:
            clean_msg = clean_msg.replace(ch, "")
        return clean_msg.strip()

    def _format_line(self, line: str) -> str:
        """格式化单行日志"""
        if not line or not line.strip():
            return None

        clean_msg = self._clean_ansi(line)
        if not clean_msg:
            return None

        # 标签化处理
        if "CMD:" in clean_msg:
            return f"EXEC: {clean_msg.replace('CMD:', '').strip()}"
        elif "Thought:" in clean_msg:
            return f"THINK: {clean_msg.replace('Thought:', '').strip()}"
        elif "[SYSTEM TOOL]" in clean_msg:
            return f"SYS: {clean_msg.replace('[SYSTEM TOOL]', '').strip()}"
        elif "Error" in clean_msg or "ERROR" in clean_msg:
            return f"ERROR: {clean_msg}"
        else:
            return clean_msg

    def push_message(self, message: str):
        """直接推送消息到日志显示"""
        formatted = self._format_line(message)
        if formatted:
            self.log_element.push(formatted)

    def read_new_logs(self) -> str:
        """读取日志文件的新增内容（增量读取）"""
        try:
            if not os.path.exists(self.log_file_path):
                return ""

            current_size = os.path.getsize(self.log_file_path)

            # 文件被截断（重新开始），重置位置
            if current_size < self.last_size:
                self.last_position = 0
                self.last_size = 0

            # 没有新内容
            if current_size == self.last_size:
                return ""

            # 读取新增内容
            with open(self.log_file_path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self.last_position)
                new_content = f.read()

                # 更新位置和大小
                self.last_position = f.tell()
                self.last_size = current_size

            return new_content

        except Exception as e:
            return f"[轮询错误: {str(e)}]"

    async def start_polling(self):
        """开始轮询日志文件"""
        if self.is_polling:
            return

        self.is_polling = True
        self.last_position = 0
        self.last_size = 0

        while self.is_polling:
            try:
                new_logs = self.read_new_logs()
                if new_logs:
                    # 按行处理并推送
                    for line in new_logs.split('\n'):
                        formatted = self._format_line(line)
                        if formatted:
                            self.log_element.push(formatted)

                await asyncio.sleep(self.poll_interval)

            except Exception as e:
                self.log_element.push(f"[轮询异常: {str(e)}]")
                await asyncio.sleep(self.poll_interval * 2)  # 错误时加倍等待

    def stop_polling(self):
        """停止轮询"""
        self.is_polling = False

    def reset(self):
        """重置状态"""
        self.last_position = 0
        self.last_size = 0


# ========== 4) 下载报告 ==========
def download_report(content: str, filename: str):
    if not content:
        ui.notify("No report to download", type="warning")
        return

    # 使用 JavaScript Blob + URL.createObjectURL 的方式
    # 这是 NiceGUI 官方推荐的内存文件下载方法
    ui.run_javascript(f"""
        const blob = new Blob([{content}], {{type: 'text/plain'}});
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = '{filename}';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    """)


# ========== 5) 核心任务逻辑 ==========
async def run_mission(target, max_rounds, log_manager, artifacts_card, md_btn, html_btn, start_btn, poll_toggle_btn):
    """执行渗透任务（轮询模式）"""
    # 1. UI 状态重置
    start_btn.disable()
    artifacts_card.visible = False
    md_btn.disable()
    html_btn.disable()
    log_manager.reset()
    log_manager.log_element.clear()

    # 2. 初始化日志文件
    log_path = os.getenv("LOG_FILE_PATH", "mission.log")
    os.makedirs(os.path.dirname(log_path) if os.path.dirname(log_path) else ".", exist_ok=True)

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"=== 任务启动 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        f.write(f"目标: {target}\n")
        f.write(f"最大轮次数: {max_rounds}\n")
        f.write("="*80 + "\n\n")

    log_manager.push_message(f"🚀 任务启动 - 目标: {target}")

    # 3. 启动轮询
    poll_toggle_btn.props('icon=pause')
    poll_toggle_btn.props('color=positive')

    polling_task = asyncio.create_task(log_manager.start_polling())

    try:
        # API Key 检查
        if not os.environ.get("OPENAI_API_KEY") and not any(
            os.environ.get(f"{role}_API_KEY") for role in ["STRATEGIST", "DEPUTY", "OPERATOR", "AUDITOR"]
        ):
            raise ValueError("API Keys not found!")

        # 编译 Graph
        assault_graph = create_assault_graph()
        reporting_graph = create_reporting_graph()

        mission_history = f"目标: {target}, 轮次数: {max_rounds}"

        # 攻击循环
        for i in range(1, int(max_rounds) + 1):
            log_manager.push_message(f"\n🔥 Round {i}/{max_rounds} 开始")

            state = {
                "target": target,
                "mission_history": mission_history,
                "strategy": "", "deputy_requirement": "",
                "operator_command": "", "execution_result": "",
                "log_result": "", "final_report": "", "final_html": ""
            }

            # 静默执行（所有输出写入日志文件）
            result_state = await asyncio.to_thread(assault_graph.invoke, state)
            mission_history += f"\nRound {i} 完成"

            log_manager.push_message(f"✅ Round {i} 完成")

            # 生成报告
            log_manager.push_message("📊 生成报告...")
            report_state = {
                "target": target,
                "mission_history": mission_history,
                "strategy": "", "deputy_requirement": "",
                "operator_command": "", "execution_result": "",
                "log_result": "", "final_report": "", "final_html": ""
            }

            await asyncio.to_thread(reporting_graph.invoke, report_state)

        # 任务完成
        log_manager.push_message("\n🎉 任务完成！")

        # 检查成果文件
        if os.path.exists("final_report.md"):
            md_btn.enable()
            log_manager.push_message("✅ MD 报告已生成")
        if os.path.exists("final_report.html"):
            html_btn.enable()
            log_manager.push_message("✅ HTML 报告已生成")

        artifacts_card.visible = True
        ui.notify("Mission Complete!", type="positive")

    except Exception as e:
        log_manager.push_message(f"\n❌ 任务失败: {str(e)}")
        ui.notify(f"Failed: {str(e)}", type="negative")

    finally:
        # 停止轮询
        log_manager.stop_polling()
        poll_toggle_btn.props('icon=play_arrow')
        poll_toggle_btn.props('color=grey')
        start_btn.enable()


def download_local_file(filename: str):
    if not os.path.exists(filename):
        ui.notify(f"File {filename} not found!", type="negative")
        return

    try:
        # 以 bytes 模式读取，确保编码正确
        with open(filename, "rb") as f:
            content_bytes = f.read()

        # 转换为 Base64（二进制安全）
        b64_content = base64.b64encode(content_bytes).decode("ascii")

        ui.run_javascript(f"""
            // 解码并创建 UTF-8 Blob
            const bytes = Uint8Array.from(atob('{b64_content}'), c => c.charCodeAt(0));
            const blob = new Blob([bytes], {{
                type: 'text/plain;charset=utf-8'
            }});
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


# ========== 6) 界面 ==========
@ui.page("/")
def main_page():
    # 设置页面背景色
    ui.colors(primary='#E11D48', secondary='#1F2937', accent='#22C55E')

    with ui.header().classes("bg-gray-900 text-white p-4 shadow-lg flex items-center gap-3"):
        ui.icon('security', size='md', color='red-500')
        ui.label("AutoRedTeam Pro").classes("text-xl font-bold tracking-wider")

    with ui.row().classes("w-full no-wrap p-4 gap-6 items-stretch h-[calc(100vh-80px)] bg-gray-100"):

        # === 左侧：控制面板 (1/4 宽度) ===
        with ui.card().classes("w-1/4 flex flex-col p-6 shadow-sm gap-6"):
            ui.label("Mission Control").classes("text-lg font-bold text-gray-700 border-b pb-2")

            target_input = ui.input(label="Target IP / URL", value="192.168.1.1").props('outlined dense').classes("w-full")
            rounds_input = ui.number(label="Attack Rounds", value=3, min=1, max=10).props('outlined dense').classes("w-full")

            start_btn = ui.button("START OPERATION", icon="rocket_launch").classes("w-full h-12 text-lg shadow-md")

            # === 新增：轮询控制区 ===
            ui.label("日志轮询控制").classes("text-sm font-bold text-gray-600 mt-4")
            ui.label("轮询间隔 (秒):").classes("text-xs text-gray-500")
            poll_interval_input = ui.number(value=2.0, min=0.5, max=10.0, step=0.5).props('outlined dense').classes("w-full")

            # === 新增：成果物区域 (默认隐藏) ===
            with ui.column().classes("w-full gap-3 mt-auto pt-6 border-t") as artifacts_card:
                artifacts_card.visible = False
                ui.label("Mission Artifacts").classes("font-bold text-gray-600 text-sm")

                with ui.row().classes("w-full gap-2"):
                    # Markdown 下载按钮
                    md_btn = ui.button("Report.md", icon="description",
                        on_click=lambda: download_local_file("final_report.md")
                    ).props('flat outline color=grey').classes("flex-grow")

                    # HTML 下载按钮
                    html_btn = ui.button("Report.html", icon="html",
                        on_click=lambda: download_local_file("final_report.html")
                    ).props('flat outline color=blue').classes("flex-grow")

        # === 右侧：实时终端 (3/4 宽度) ===
        with ui.card().classes("w-3/4 flex flex-col p-0 shadow-sm overflow-hidden bg-black"):
            # 终端标题栏
            with ui.row().classes("w-full bg-gray-800 p-2 px-4 items-center justify-between border-b border-gray-700"):
                with ui.row().classes("gap-2 items-center"):
                    ui.icon('terminal', color='green-400', size='sm')
                    ui.label("Execution Log").classes("text-gray-300 font-mono text-sm")
                    ui.label("[轮询模式]").classes("text-yellow-500 text-xs font-bold")

                # 轮询控制按钮
                poll_toggle_btn = ui.button(icon="play_arrow", on_click=lambda: None).props(
                    'flat round size=sm color=grey'
                ).classes("opacity-70")
                poll_toggle_btn.props('disable')  # 任务启动后才能操作

            # 终端内容
            log_display = ui.log(max_lines=2000).classes(
                "w-full flex-grow bg-black text-green-400 font-mono text-sm p-4 overflow-y-auto leading-relaxed"
            )

            # 创建日志管理器
            log_manager = LogPollingManager(
                log_element=log_display,
                log_file_path=LOG_FILE_PATH,
                poll_interval=2.0
            )

            # 轮询控制逻辑
            def toggle_polling():
                if log_manager.is_polling:
                    log_manager.stop_polling()
                    poll_toggle_btn.props('icon=play_arrow')
                    poll_toggle_btn.props('color=grey')
                else:
                    log_manager.poll_interval = poll_interval_input.value
                    asyncio.create_task(log_manager.start_polling())
                    poll_toggle_btn.props('icon=pause')
                    poll_toggle_btn.props('color=positive')

            poll_toggle_btn.on_click(toggle_polling)

            # 绑定启动按钮
            start_btn.on_click(
                lambda: run_mission(
                    target_input.value,
                    int(rounds_input.value),
                    log_manager,
                    artifacts_card,
                    md_btn,
                    html_btn,
                    start_btn,
                    poll_toggle_btn
                )
            )


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title="AutoRedTeam", port=8080, reload=False)
