import sys
import os
import re
import asyncio
from pathlib import Path
from nicegui import ui, app
from dotenv import load_dotenv


log_path = r"D:/project/autopt/mission.log"
# --- 1. 环境配置与遥测禁用 (关键修复点) ---
current_file = Path(__file__).resolve()
project_root = current_file.parent.parent.parent

# 1.1 强制加载 .env
env_path = project_root / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)



# 1.2 【关键】禁用 CrewAI 的遥测和分析功能，防止 EventBus JSON 解析报错
os.environ["CREWAI_TELEMETRY_OPT_OUT"] = "true"
os.environ["OTEL_SDK_DISABLED"] = "true"

# 1.3 将项目根目录加入 Python 路径
sys.path.append(str(project_root))





# 确保目录存在
os.makedirs(os.path.dirname(log_path), exist_ok=True)

# 核心修复：如果文件不存在或为空，写入初始化头

with open(log_path, "w", encoding="utf-8") as f:
    f.write("=== 渗透任务日志初始化 ===\n[SYSTEM] 任务启动。\n当前为初期阶段，请开始渗透流程\n================\n\n")
    print("日志文件已初始化，Agent 不会读到空内容了。")


from src.team.crew import RedTeamCrew





# --- 2. 自定义日志清洗器 (保持不变) ---
class CleanLogger:
    def __init__(self, log_element):
        self.terminal = sys.__stdout__
        self.log_element = log_element
        self.ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        self.box_chars = ["│", "┌", "┐", "└", "┘", "─", "┤", "├", "┴", "┬"]

    def write(self, message):
        self.terminal.write(message)
        if message.strip():
            clean_msg = self.ansi_escape.sub('', message)
            for char in self.box_chars:
                clean_msg = clean_msg.replace(char, '')
            clean_msg = clean_msg.strip()
            
            if clean_msg:
                if "CMD:" in clean_msg:
                    clean_msg = f"💻 EXEC: {clean_msg.replace('CMD:', '').strip()}"
                elif "Thought:" in clean_msg:
                    clean_msg = f"🤔 THINK: {clean_msg.replace('Thought:', '').strip()}"
                elif "[SYSTEM TOOL]" in clean_msg:
                    clean_msg = f"⚙️ SYS: {clean_msg.replace('[SYSTEM TOOL]', '').strip()}"
                elif "Error" in clean_msg or "ERROR" in clean_msg:
                    clean_msg = f"❌ {clean_msg}"
                
                self.log_element.push(clean_msg)

    def flush(self):
        self.terminal.flush()

# --- 3. 核心任务逻辑 ---
async def run_mission(target_ip, max_rounds, log_view, report_view, download_btn, start_btn):
    """执行渗透任务"""
    start_btn.disable()
    log_view.clear()
    report_view.content = "**Mission Initializing...**"
    download_btn.disable()
    
    # 清理旧日志
    log_file = project_root / "mission.log"
    if log_file.exists():
        os.remove(log_file)

    clean_logger = CleanLogger(log_view)
    sys.stdout = clean_logger

    try:
        if not os.environ.get("STRATEGIST_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("API Keys not found! Please check your .env file.")

        mission_history = f"Mission Start. Target: {target_ip}"
        red_team = RedTeamCrew()
        inputs = {'target': target_ip, 'mission_history': mission_history}

        # --- 循环执行 ---
        for i in range(1, int(max_rounds) + 1):
            log_view.push(f"\n{'='*10} ROUND {i} STARTED {'='*10}")
            
            assault = red_team.assault_crew()
            inputs['mission_history'] = mission_history
            
            result = await asyncio.to_thread(assault.kickoff, inputs=inputs)
            
            mission_history += f"\n[Round {i}]: {result}"
            log_view.push(f"✅ Round {i} Finished.")

        # --- 生成报告 ---
        log_view.push(f"\n{'='*10} GENERATING REPORT {'='*10}")
        
        reporter = red_team.reporting_crew()
        
        # 这里的 kickoff 可能会因为 EventBus 报错，但我们禁用了遥测后应该就没事了
        final_report = await asyncio.to_thread(reporter.kickoff, inputs={})
        
        # 【防御性编程】强制转换为字符串，防止 CrewAI 返回对象导致 UI 渲染失败
        if hasattr(final_report, 'raw'):
            final_report_str = str(final_report.raw)
        else:
            final_report_str = str(final_report)

        # UI 更新
        report_view.set_content(final_report_str)
        download_btn.enable()
        
        sys.stdout = sys.__stdout__
        ui.notify('Mission Complete!', type='positive')

    except Exception as e:
        sys.stdout = sys.__stdout__
        error_msg = f"CRITICAL ERROR: {str(e)}"
        print(error_msg)
        log_view.push(f"❌ {error_msg}")
        ui.notify(f'Failed: {str(e)}', type='negative')
    finally:
        start_btn.enable()

# --- 4. 下载功能 (保持不变) ---
def download_report(report_content):
    if not report_content:
        ui.notify("No report to download", type='warning')
        return
    ui.download(
        content=report_content.encode('utf-8'), 
        filename='penetration_report.md', 
        media_type='text/markdown'
    )

# --- 5. 界面布局 (保持不变) ---
@ui.page('/')
def main_page():
    ui.add_head_html('''
        <style>
            .log-box { font-family: 'Consolas', 'Monaco', monospace; font-size: 0.85rem; line-height: 1.2; }
            .report-box { font-family: 'Segoe UI', sans-serif; }
            body { background-color: #f3f4f6; } 
        </style>
    ''')

    with ui.header().classes('bg-slate-900 text-white p-4 shadow-md'):
        with ui.row().classes('items-center gap-2'):
            ui.icon('shield', size='lg', color='red-500')
            ui.label('AutoRedTeam Pro').classes('text-xl font-bold tracking-wider')

    with ui.row().classes('w-full no-wrap p-4 gap-4 items-stretch h-[calc(100vh-80px)]'):
        with ui.card().classes('w-5/12 flex flex-col p-0 gap-0 shadow-lg h-full'):
            with ui.column().classes('p-4 bg-white border-b gap-3'):
                ui.label('🎮 Operation Control').classes('font-bold text-gray-700')
                target_input = ui.input(label='Target IP/URL', value='192.168.154.128').classes('w-full')
                rounds_input = ui.number(label='Max Rounds', value=3, min=1, max=10).classes('w-full')
                start_btn = ui.button('🚀 IGNITE MISSION', on_click=lambda: run_mission(
                    target_input.value, rounds_input.value, log_display, report_display, download_btn, start_btn
                )).classes('w-full bg-red-600 text-white font-bold')

            ui.label('📡 Live Terminal Log').classes('px-4 py-2 bg-gray-800 text-xs text-gray-400 font-mono border-t')
            log_display = ui.log(max_lines=1000).classes('w-full flex-grow bg-black text-green-400 p-4 log-box overflow-y-auto')

        with ui.card().classes('w-7/12 flex flex-col p-0 shadow-lg border border-gray-200 h-full'):
            with ui.row().classes('p-4 bg-gray-50 border-b justify-between items-center w-full'):
                ui.label('📄 Intelligence Report').classes('font-bold text-gray-700')
                download_btn = ui.button('💾 Download Report', on_click=lambda: download_report(report_display.content))\
                    .classes('bg-blue-600 text-white text-sm')
                download_btn.disable()
            with ui.scroll_area().classes('w-full flex-grow bg-white p-8'):
                report_display = ui.markdown('**Waiting for mission data...**\n\nSet target and click start.')\
                    .classes('w-full report-box prose max-w-none')

ui.run(title="AutoRedTeam", port=8080, reload=False)