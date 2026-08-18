# gui_pages.py - AutoPT 功能页面模块
# 模块3/4/5/6/7/10/1 的 GUI 页面：项目与资产、历史会话、设置、Agent编辑、Skill、RAG
import json
import os
from datetime import datetime
from pathlib import Path

from nicegui import ui

import database as db
import agent_manager
import skill_manager
import rag_store

PROJECT_ROOT = Path(__file__).resolve().parent


# ==================== 模块3/4: 项目与资产 ====================
@ui.page("/projects")
def projects_page():
    ui.add_head_html("<title>AutoPT - 项目与资产</title>")
    _nav_header("projects")

    def refresh_projects():
        projects_container.clear()
        with projects_container:
            for p in db.list_projects():
                with ui.card().classes("w-full"):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label(p["name"]).classes("text-lg font-bold")
                        ui.label(f"更新: {p['updated_at']}").classes("text-xs text-gray-500")
                        ui.button("查看资产", on_click=lambda pid=p["id"]: _show_assets(pid)).props("outline size=sm")
                        ui.button("删除", on_click=lambda pid=p["id"]: _delete_project(pid)).props("outline size=sm color=red")

    def _show_assets(pid):
        assets_card.clear()
        with assets_card:
            ui.label(f"资产 - {db.get_project(pid)['name']}").classes("text-lg font-bold")
            types = ["domain", "ip", "port", "url", "vuln"]
            type_names = {"domain": "域名", "ip": "IP", "port": "端口", "url": "URL", "vuln": "漏洞"}
            for t in types:
                assets = db.list_assets(pid, t)
                with ui.expansion(f"{type_names.get(t, t)} ({len(assets)})").classes("w-full"):
                    if not assets:
                        ui.label("无").classes("text-gray-400")
                    else:
                        with ui.column().classes("w-full gap-1"):
                            for a in assets[:100]:
                                ui.label(f"{a['value']}  {a.get('detail', '')}").classes(
                                    "text-sm font-mono " + ("text-red-600" if t == "vuln" else "")
                                )

    def _delete_project(pid):
        db.delete_project(pid)
        refresh_projects()
        assets_card.clear()
        ui.notify("项目已删除", type="warning")

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
        ui.label("项目管理与资产统计").classes("text-2xl font-bold")

        with ui.row().classes("w-full gap-2 items-center"):
            name_input = ui.input("项目名称").classes("flex-grow")
            desc_input = ui.input("描述（可选）").classes("flex-grow")

            def create_project():
                if name_input.value.strip():
                    db.get_or_create_project(name_input.value.strip())
                    db.update_project(
                        db.get_or_create_project(name_input.value.strip()),
                        description=desc_input.value.strip(),
                    )
                    refresh_projects()
                    name_input.value = ""
                    ui.notify("项目已创建", type="positive")

            ui.button("新建项目", on_click=create_project)

        projects_container = ui.column().classes("w-full gap-2")
        refresh_projects()

        assets_card = ui.column().classes("w-full mt-4 gap-2")


# ==================== 模块5: 历史会话 ====================
@ui.page("/history")
def history_page():
    ui.add_head_html("<title>AutoPT - 历史会话</title>")
    _nav_header("history")

    def refresh():
        list_container.clear()
        with list_container:
            for t in db.list_tasks():
                with ui.card().classes("w-full"):
                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.column().classes("gap-0"):
                            ui.label(t["title"]).classes("font-bold")
                            ui.label(f"目标: {t['target']} | 状态: {t['status']} | 轮次: {t['current_round']}/{t['max_rounds']} | {t['created_at']}").classes("text-xs text-gray-500")
                        with ui.row().classes("gap-2"):
                            ui.button("查看", on_click=lambda tid=t["id"]: _view(tid)).props("outline size=sm")
                            ui.button("恢复会话", on_click=lambda tid=t["id"]: _resume(tid)).props("outline size=sm color=primary")
                            ui.button("删除", on_click=lambda tid=t["id"]: _delete(tid)).props("outline size=sm color=red")

    def _view(tid):
        detail_card.clear()
        with detail_card:
            msgs = db.list_messages(tid)
            ui.label(f"会话记录 ({len(msgs)} 条)").classes("text-lg font-bold")
            if not msgs:
                ui.label("（该任务暂无历史消息记录）").classes("text-gray-400")
            with ui.scroll_area().classes("w-full h-96 border rounded p-2"):
                with ui.column().classes("w-full gap-1"):
                    for m in msgs:
                        role_label = {"user": "用户", "assistant": "AI", "system": "系统", "tool": "工具"}.get(m["role"], m["role"])
                        color = "text-blue-600" if m["role"] == "user" else ("text-green-600" if m["role"] == "assistant" else "text-gray-500")
                        with ui.row().classes("w-full gap-1"):
                            ui.label(f"[{role_label}]").classes(f"text-xs font-bold {color}")
                            ui.label(m["content"][:200]).classes(f"text-sm {color}").props("wrap")

    def _resume(tid):
        # 问题2修复: 恢复会话时加载历史消息展示
        resume_card.clear()
        with resume_card:
            t = db.get_task(tid)
            msgs = db.list_messages(tid)
            ui.label(f"恢复会话: {t['title']}").classes("text-lg font-bold")
            ui.label(f"目标: {t['target']} | 状态: {t['status']}").classes("text-sm text-gray-500")

            # 展示历史上下文（供用户确认恢复哪个会话）
            with ui.expansion(f"历史上下文 ({len(msgs)} 条消息)", value=False).classes("w-full"):
                with ui.column().classes("w-full gap-1"):
                    for m in msgs[-20:]:  # 最近 20 条
                        role_label = {"user": "用户", "assistant": "AI", "system": "系统", "tool": "工具"}.get(m["role"], m["role"])
                        color = "text-blue-600" if m["role"] == "user" else "text-gray-600"
                        ui.label(f"[{role_label}] {m['content'][:150]}").classes(f"text-xs {color}").props("wrap")

            ui.label("请输入新的需求，将以恢复的上下文继续任务：").classes("text-sm text-gray-600 mt-2")
            new_req = ui.textarea("新的需求（可选）").classes("w-full")
            target_input = ui.input("目标（可修改）", value=t["target"]).classes("w-full")

            def do_resume():
                from gui_app import mission_control
                import agents
                if new_req.value.strip():
                    agents.add_interrupt_request(new_req.value.strip())
                ui.navigate.to("/")
                ui.notify("已恢复会话，请在主页面重新启动任务", type="positive")

            ui.button("确认恢复", on_click=do_resume).props("color=primary")

    def _delete(tid):
        # 问题2修复: 先通知再刷新，避免访问已删除的 client 上下文
        db.delete_task(tid)
        try:
            ui.notify("会话已删除", type="warning")
        except Exception:
            pass
        refresh()
        detail_card.clear()

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
        ui.label("历史会话").classes("text-2xl font-bold")
        list_container = ui.column().classes("w-full gap-2")
        refresh()
        detail_card = ui.column().classes("w-full mt-4 gap-2")
        resume_card = ui.column().classes("w-full mt-4 gap-2")


# ==================== 模块6: 设置 ====================
@ui.page("/settings")
def settings_page():
    ui.add_head_html("<title>AutoPT - 设置</title>")
    _nav_header("settings")

    # 加载 .env 现有配置
    env_data = {}
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env_data[k.strip()] = v.strip()

    def save():
        updates = {}
        for key, inp in inputs.items():
            val = inp.value
            if key in env_data and env_data[key] != val:
                updates[key] = val
            elif key not in env_data and val:
                updates[key] = val
        # 写回 .env
        lines = []
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines()
        for key, val in updates.items():
            found = False
            for i, line in enumerate(lines):
                if line.strip().startswith(f"{key}="):
                    lines[i] = f"{key}={val}"
                    found = True
                    break
            if not found:
                lines.append(f"{key}={val}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ui.notify("设置已保存，重启生效", type="positive")

    with ui.column().classes("w-full max-w-3xl mx-auto p-6 gap-4"):
        ui.label("系统设置").classes("text-2xl font-bold")
        ui.label("修改保存到 .env 文件，重启应用后生效").classes("text-sm text-gray-500")

        inputs = {}
        with ui.expansion("API 配置", value=True).classes("w-full"):
            with ui.column().classes("w-full gap-2"):
                for key, label in [
                    ("OPENAI_API_KEY", "全局 API Key"),
                    ("OPENAI_BASE_URL", "全局 Base URL"),
                    ("STRATEGIST_MODEL", "渗透指挥官模型"),
                    ("DEPUTY_MODEL", "副指挥官模型"),
                    ("OPERATOR_MODEL", "战术执行专家模型"),
                    ("AUDITOR_MODEL", "执行引擎模型"),
                    ("REPORTER_MODEL", "日志摘要专家模型"),
                    ("HTML_REPORTER_MODEL", "报告分析师模型"),
                ]:
                    inputs[key] = ui.input(label, value=env_data.get(key, "")).classes("w-full")

        with ui.expansion("工具与路径配置").classes("w-full"):
            with ui.column().classes("w-full gap-2"):
                for key, label in [
                    ("TOOLS_ROOT_DIR", "工具根目录 (aptools)"),
                    ("LOG_FILE_PATH", "日志文件路径"),
                    ("REPORT_FILE_PATH", "报告路径"),
                ]:
                    inputs[key] = ui.input(label, value=env_data.get(key, "")).classes("w-full")

        with ui.expansion("RAG Embedding 配置（远程语义检索）").classes("w-full"):
            ui.label("配置后 RAG 使用远程 embedding 做语义检索（效果远好于关键词）；留空则回退本地关键词检索。推荐硅基流动 SiliconFlow（OpenAI 兼容）").classes("text-xs text-gray-500")
            with ui.column().classes("w-full gap-2"):
                for key, label, ph in [
                    ("EMBEDDING_BASE_URL", "Embedding Base URL", "https://api.siliconflow.cn/v1"),
                    ("EMBEDDING_API_KEY", "Embedding API Key", "sk-..."),
                    ("EMBEDDING_MODEL", "Embedding 模型", "BAAI/bge-m3"),
                    ("RAG_EMBEDDING_ENABLED", "启用开关 (true/false)", "true"),
                ]:
                    inputs[key] = ui.input(
                        label, value=env_data.get(key, ""),
                        placeholder=ph,
                    ).classes("w-full")

        ui.button("保存设置", on_click=save).props("color=primary")


# ==================== 模块7: Agent 编辑 ====================
@ui.page("/agents")
def agents_page():
    ui.add_head_html("<title>AutoPT - Agent 管理</title>")
    _nav_header("agents")
    mgr = agent_manager.agent_manager

    def refresh():
        list_container.clear()
        with list_container:
            for a in mgr.list_agents():
                with ui.card().classes("w-full"):
                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.column().classes("gap-0 flex-grow"):
                            ui.label(f"{a['role']} ({a['key']})").classes("font-bold")
                            status = "激活" if a["enabled"] else "停用"
                            builtin = "内置" if a["is_builtin"] else "自定义"
                            tools_str = _tools_to_str(a["tools"])
                            ui.label(f"工具: {tools_str or '无'} | {status} | {builtin}").classes("text-xs text-gray-500")
                        with ui.row().classes("gap-2"):
                            ui.button("编辑", on_click=lambda ag=a: _edit(ag)).props("outline size=sm")
                            if not a["is_builtin"]:
                                ui.button("删除", on_click=lambda ag=a: _delete(ag)).props("outline size=sm color=red")
                            ui.button("停用" if a["enabled"] else "激活", on_click=lambda ag=a: _toggle(ag)).props("outline size=sm")

    # 可用工具名提示（内置+自定义）
    AVAILABLE_TOOLS = [
        "file_read_tool", "file_write_tool", "execution_tool", "list_custom_tool",
        "http_get_tool", "http_post_tool", "dns_lookup_tool", "port_probe_tool",
        "whois_lookup_tool",
    ]

    def _tools_to_str(tools) -> str:
        """tools 可能是 list 或 JSON 字符串，统一转逗号分隔字符串。"""
        if not tools:
            return ""
        if isinstance(tools, list):
            return ", ".join(str(x) for x in tools)
        if isinstance(tools, str):
            try:
                parsed = json.loads(tools)
                if isinstance(parsed, list):
                    return ", ".join(str(x) for x in parsed)
            except Exception:
                pass
            return tools
        return str(tools)

    def _tools_to_list(tools_str: str) -> list:
        return [t.strip() for t in tools_str.split(",") if t.strip()]

    def _edit(ag):
        edit_card.clear()
        with edit_card:
            ui.label(f"编辑: {ag['role']} ({ag['key']})").classes("text-lg font-bold")
            role_in = ui.input("角色名", value=ag["role"]).classes("w-full")
            goal_in = ui.textarea("目标 (Goal)", value=ag["goal"]).classes("w-full")
            back_in = ui.textarea("背景/行为准则 (Backstory)", value=ag["backstory"]).classes("w-full").props("rows=6")
            tools_in = ui.input("工具列表（逗号分隔）", value=_tools_to_str(ag["tools"])).classes("w-full")
            with ui.expansion("可用工具参考").classes("w-full"):
                ui.label("、".join(AVAILABLE_TOOLS)).classes("text-xs text-gray-500")

            def save():
                tools = _tools_to_list(tools_in.value)
                mgr.update_agent(ag["key"], role=role_in.value, goal=goal_in.value, backstory=back_in.value, tools=tools)
                edit_card.clear()
                refresh()
                ui.notify("已保存", type="positive")

            ui.button("保存", on_click=save).props("color=primary")

    def _delete(ag):
        try:
            mgr.delete_agent(ag["key"])
            refresh()
            ui.notify("已删除", type="warning")
        except ValueError as e:
            ui.notify(str(e), type="negative")

    def _toggle(ag):
        mgr.set_enabled(ag["key"], not ag["enabled"])
        refresh()

    # 模板导入导出（JSON）
    def export_template():
        data = {"agents": mgr.list_agents()}
        json_str = json.dumps(data, ensure_ascii=False, indent=2)
        ui.download(
            json_str.encode("utf-8"),
            filename="autopt_agents_template.json",
        )
        ui.notify("已导出模板", type="positive")

    def import_template(file_content: str):
        try:
            data = json.loads(file_content)
            agents_list = data.get("agents", [])
            imported = 0
            for a in agents_list:
                try:
                    mgr.create_agent(
                        a["key"], a.get("role", ""), a.get("goal", ""), a.get("backstory", ""),
                        tools=a.get("tools", []), enabled=a.get("enabled", True),
                    )
                    imported += 1
                except ValueError:
                    pass  # 已存在则跳过
            refresh()
            ui.notify(f"已导入 {imported} 个角色", type="positive")
        except Exception as e:
            ui.notify(f"导入失败: {e}", type="negative")

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
        ui.label("Agent 角色管理").classes("text-2xl font-bold")

        with ui.row().classes("w-full gap-2 items-center"):
            ui.button("导出模板", on_click=export_template).props("outline")
            ui.upload(
                label="导入模板 (JSON)",
                auto_upload=True,
                on_upload=lambda e: import_template(e.content.read().decode("utf-8")),
            ).props("accept=.json").classes("w-64")

        with ui.expansion("新建角色").classes("w-full"):
            with ui.column().classes("w-full gap-2"):
                key_in = ui.input("角色 Key（英文，如 scanner）").classes("w-full")
                role_in = ui.input("角色名（中文）").classes("w-full")
                goal_in = ui.textarea("目标").classes("w-full")
                back_in = ui.textarea("背景/行为准则").classes("w-full").props("rows=4")
                tools_in = ui.input("工具列表（逗号分隔，可选）").classes("w-full")
                with ui.expansion("可用工具参考").classes("w-full"):
                    ui.label("、".join(AVAILABLE_TOOLS)).classes("text-xs text-gray-500")

                def create():
                    try:
                        tools = _tools_to_list(tools_in.value)
                        mgr.create_agent(key_in.value.strip(), role_in.value, goal_in.value, back_in.value, tools=tools)
                        refresh()
                        ui.notify("角色已创建", type="positive")
                    except ValueError as e:
                        ui.notify(str(e), type="negative")

                ui.button("创建", on_click=create).props("color=primary")

        list_container = ui.column().classes("w-full gap-2")
        refresh()
        edit_card = ui.column().classes("w-full mt-4 gap-2")


# ==================== 模块10: Skill 管理 ====================
@ui.page("/skills")
def skills_page():
    ui.add_head_html("<title>AutoPT - Skill 管理</title>")
    _nav_header("skills")
    mgr = skill_manager.skill_manager

    def refresh():
        list_container.clear()
        with list_container:
            for s in mgr.list_skills():
                with ui.card().classes("w-full"):
                    # 问题4修复: 激活状态用彩色标签明显区分；查看内容用 expansion 可收起
                    with ui.expansion(
                        f"{s['title']} ({s['name']})"
                    ).classes("w-full") as exp:
                        with ui.row().classes("w-full items-center justify-between"):
                            with ui.row().classes("gap-2 items-center"):
                                if s["enabled"]:
                                    ui.label("● 激活").classes(
                                        "text-xs font-bold text-green-600 bg-green-50 px-2 py-0.5 rounded"
                                    )
                                else:
                                    ui.label("● 关闭").classes(
                                        "text-xs font-bold text-gray-400 bg-gray-100 px-2 py-0.5 rounded"
                                    )
                                ui.label(s["description"] or "").classes("text-xs text-gray-500")
                            with ui.row().classes("gap-2"):
                                ui.button("关闭" if s["enabled"] else "激活",
                                          on_click=lambda sk=s: _toggle(sk)).props("outline size=sm")
                                ui.button("删除", on_click=lambda sk=s: _delete(sk)).props("outline size=sm color=red")
                        # expansion 内容区：正文
                        ui.label(s["content"]).classes("text-sm whitespace-pre-wrap")

    def _toggle(s):
        mgr.set_enabled(s["name"], not s["enabled"])
        refresh()

    def _delete(s):
        # 问题2同类修复: 先通知再刷新
        mgr.delete_skill(s["name"])
        try:
            ui.notify("已删除", type="warning")
        except Exception:
            pass
        refresh()

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
        ui.label("Skill 技能管理").classes("text-2xl font-bold")

        with ui.expansion("新建 Skill").classes("w-full"):
            with ui.column().classes("w-full gap-2"):
                name_in = ui.input("名称（英文+下划线，如 recon_workflow）").classes("w-full")
                title_in = ui.input("标题（中文）").classes("w-full")
                desc_in = ui.input("描述").classes("w-full")
                content_in = ui.textarea("内容").classes("w-full").props("rows=6")

                def create():
                    try:
                        mgr.create_skill(name_in.value.strip(), title_in.value, desc_in.value, content_in.value)
                        refresh()
                        ui.notify("Skill 已创建", type="positive")
                    except ValueError as e:
                        ui.notify(str(e), type="negative")

                ui.button("创建", on_click=create).props("color=primary")

        list_container = ui.column().classes("w-full gap-2")
        refresh()


# ==================== 模块1: RAG 知识库 ====================
@ui.page("/rag")
def rag_page():
    ui.add_head_html("<title>AutoPT - RAG 知识库</title>")
    _nav_header("rag")
    store = rag_store.rag_store

    def refresh_entries():
        entries_container.clear()
        with entries_container:
            for e in store.list_entries():
                with ui.card().classes("w-full"):
                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.column().classes("gap-0 flex-grow"):
                            ui.label(f"[{e['category']}] {e['title']}").classes("font-bold")
                            ui.label(e["content"][:100] + ("..." if len(e["content"]) > 100 else "")).classes("text-xs text-gray-500")
                        ui.button("删除", on_click=lambda eid=e["id"]: _delete_entry(eid)).props("outline size=sm color=red")

    def _delete_entry(eid):
        store.delete_entry(eid)
        refresh_entries()

    def refresh_tasks():
        """刷新可选的历史任务列表（供收录到 RAG）。"""
        task_list.clear()
        with task_list:
            tasks = db.list_tasks()
            if not tasks:
                ui.label("暂无历史任务，请先执行任务").classes("text-gray-400")
                return
            for t in tasks:
                with ui.card().classes("w-full"):
                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.column().classes("gap-0 flex-grow"):
                            ui.label(t["title"]).classes("font-bold")
                            ui.label(f"目标: {t['target']} | 状态: {t['status']} | {t['created_at']}").classes("text-xs text-gray-500")
                        with ui.row().classes("gap-2"):
                            ui.button("查看详情", on_click=lambda tid=t["id"]: _show_task_msgs(tid)).props("outline size=sm")
                            ui.button("收录到RAG", on_click=lambda tid=t["id"], tt=t["title"]: _collect(tid, tt)).props("outline size=sm color=positive")

    def _show_task_msgs(tid):
        msg_card.clear()
        with msg_card:
            ui.label("任务消息：").classes("font-bold")
            msgs = db.list_messages(tid)
            if not msgs:
                ui.label("（无消息记录）").classes("text-gray-400")
            with ui.scroll_area().classes("w-full h-40 border rounded p-2"):
                with ui.column().classes("w-full gap-1"):
                    for m in msgs:
                        role_label = {"user": "用户", "assistant": "AI", "system": "系统", "tool": "工具"}.get(m["role"], m["role"])
                        ui.label(f"[{role_label}] {m['content'][:120]}").classes("text-xs").props("wrap")

    def _collect(tid, task_title):
        """从历史会话选择任务，自动收录完整上下文到 RAG。

        优化: 除历史消息外，合并 mission.log 完整证据链（命令+输出），
        确保收录的是整个任务上下文而非零碎片段。
        """
        # 1. 历史消息
        msgs = db.list_messages(tid)
        content_parts = []
        for m in msgs:
            if m["role"] in ("assistant", "tool") and m["content"].strip():
                content_parts.append(m["content"].strip())

        # 2. 合并 mission.log 完整证据链（该任务期间的所有命令与输出）
        log_parts = []
        try:
            from config import config as _cfg
            log_path = _cfg.LOG_FILE_PATH
            if os.path.exists(log_path):
                log_text = open(log_path, encoding="utf-8", errors="ignore").read()
                if log_text.strip():
                    log_parts.append(log_text[-8000:])  # 最近 8000 字符证据链
        except Exception:
            pass

        if not content_parts and not log_parts:
            ui.notify("该任务没有可收录的内容", type="warning")
            return

        content = "\n\n=== 任务轮次 ===\n".join(content_parts)
        if log_parts:
            content += "\n\n=== 命令执行证据链 ===\n" + log_parts[0]
        content = content[:6000]  # 收录限长（超过截断）

        # 自动分类：根据内容关键词
        cat = "general"
        joined = content.lower()
        if "cve-" in joined or "漏洞" in joined or "vuln" in joined:
            cat = "vuln"
        elif "nmap" in joined or "dirsearch" in joined or "curl" in joined or "扫描" in joined or "sqlmap" in joined:
            cat = "tool"
        elif "策略" in joined or "战术" in joined or "下一步" in joined:
            cat = "strategy"
        store.add_entry_with_embedding(
            title=f"任务收录: {task_title}",
            content=content,
            category=cat,
            source_task_id=tid,
            source_task_title=task_title,
        )
        refresh_entries()
        ui.notify(f"已收录任务完整上下文（{len(content)} 字符，分类: {cat}）", type="positive")

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
        ui.label("RAG 知识库").classes("text-2xl font-bold")
        ui.label("从历史会话选择任务收录经验，后续任务自动检索注入，提升 Agent 能力").classes("text-sm text-gray-500")

        # Embedding 状态展示
        with ui.row().classes("w-full gap-2 items-center") as status_row:
            st = store.embedding_status()
            if st["enabled"] and st["ok"]:
                ui.label("● 语义检索已启用").classes("text-xs font-bold text-green-600")
                ui.label(f"模型: {st['config'].get('EMBEDDING_MODEL', '')}").classes("text-xs text-gray-500")
            elif st["enabled"]:
                ui.label("● 语义检索已配置但调用失败").classes("text-xs font-bold text-amber-600")
                ui.label(f"错误: {st['error'][:80]}").classes("text-xs text-gray-500")
            else:
                ui.label("○ 未配置远程 Embedding（使用本地关键词检索）").classes("text-xs text-gray-500")
                ui.label("可在 设置 → RAG Embedding 配置 中启用").classes("text-xs text-gray-400")
            ui.button("重建全部向量", on_click=lambda: _rebuild_vectors()).props("outline size=sm")

            # 导出/导入（知识库分享迁移）
            def export_rag():
                data = store.export_to_json()
                ui.download(
                    data.encode("utf-8"),
                    filename=f"autopt_rag_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                )
                ui.notify("已导出知识库文件，可分享给他人导入", type="positive")

            def import_rag(e):
                try:
                    content = e.content.read().decode("utf-8")
                    n, msg = store.import_from_json(content, recompute_embedding=True)
                    refresh_entries()
                    ui.notify(msg, type="positive" if n else "warning")
                except Exception as ex:
                    ui.notify(f"导入失败: {ex}", type="negative")

            ui.button("导出知识库", on_click=export_rag).props("outline size=sm")
            ui.upload(
                label="导入知识库",
                auto_upload=True,
                on_upload=import_rag,
            ).props('accept=".json" size=sm flat color=primary').classes("w-32").style("min-height: 0;")
            ui.label("(JSON)").classes("text-xs text-gray-400")

        def _rebuild_vectors():
            n, msg = store.rebuild_all_vectors()
            if n:
                ui.notify(f"已重建 {n} 条向量", type="positive")
            else:
                ui.notify(msg, type="warning")

        with ui.expansion("从历史会话收录（推荐）", value=True).classes("w-full"):
            with ui.column().classes("w-full gap-2"):
                task_list = ui.column().classes("w-full gap-2")
                refresh_tasks()
                msg_card = ui.column().classes("w-full gap-1")

        with ui.expansion("手动收录知识（补充）").classes("w-full"):
            with ui.column().classes("w-full gap-2"):
                title_in = ui.input("标题").classes("w-full")
                cat_in = ui.select(["strategy", "tool", "vuln", "target", "lesson", "general"], label="分类", value="strategy").classes("w-40")
                content_in = ui.textarea("内容").classes("w-full").props("rows=5")

                def add():
                    if title_in.value.strip() and content_in.value.strip():
                        store.add_entry_with_embedding(title_in.value.strip(), content_in.value.strip(), category=cat_in.value)
                        refresh_entries()
                        ui.notify("已收录", type="positive")

                ui.button("收录", on_click=add).props("color=primary")

        with ui.expansion("测试检索").classes("w-full"):
            with ui.column().classes("w-full gap-2"):
                query_in = ui.input("查询关键词").classes("w-full")

                def search():
                    results_card.clear()
                    with results_card:
                        for r in store.search(query_in.value, limit=5):
                            ui.label(f"[{r['category']}] {r['title']}").classes("font-bold")
                            ui.label(r["content"][:200]).classes("text-sm text-gray-600")

                ui.button("检索", on_click=search).props("outline")
                results_card = ui.column().classes("w-full gap-1")

        entries_container = ui.column().classes("w-full gap-2")
        refresh_entries()


# ==================== 模块11: 报告中心 ====================
@ui.page("/reports")
def reports_page():
    ui.add_head_html("<title>AutoPT - 报告中心</title>")
    _nav_header("reports")

    reports_dir = PROJECT_ROOT / "reports"

    def refresh():
        list_container.clear()
        if not reports_dir.exists():
            ui.label("暂无报告").classes("text-gray-400")
            return
        files = sorted(
            [f for f in reports_dir.iterdir() if f.is_file() and f.suffix.lower() in (".md", ".html")],
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        if not files:
            ui.label("暂无报告").classes("text-gray-400")
            return

        # 优化2: 按任务分组——时间戳前缀相同的 md/html 视为同一任务的报告
        # 文件名格式: autopt_YYYYMMDD_HHMMSS_目标.md/.html
        groups: dict[str, list] = {}
        for f in files:
            # 去掉扩展名后按 "_" 分段，任务组 = 时间戳(前2段) + 目标(剩余)
            stem = f.stem  # autopt_20260818_011723_target
            parts = stem.split("_")
            # 取 autopt_时间戳 作为组key（时间戳含日期+时间两部分）
            group_key = "_".join(parts[:3]) if len(parts) >= 3 else stem
            groups.setdefault(group_key, []).append(f)

        for gkey, gfiles in groups.items():
            # 时间戳解析 + 目标名
            mtime = datetime.fromtimestamp(gfiles[0].stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            parts = gkey.split("_")
            target = "_".join(parts[3:]) if len(parts) > 3 else ""
            md_file = next((f for f in gfiles if f.suffix == ".md"), None)
            html_file = next((f for f in gfiles if f.suffix == ".html"), None)

            with ui.card().classes("w-full"):
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.column().classes("gap-0 flex-grow"):
                        ui.label(f"📄 任务报告: {target or gkey}").classes("font-bold")
                        ui.label(f"{mtime} | {len(gfiles)} 个格式").classes("text-xs text-gray-500")
                    with ui.row().classes("gap-2"):
                        # 同一任务提供两种格式下载
                        if md_file:
                            ui.button("下载 Markdown", on_click=lambda fp=str(md_file): _download(fp)).props("outline size=sm color=primary")
                        if html_file:
                            ui.button("下载 HTML", on_click=lambda fp=str(html_file): _download(fp)).props("outline size=sm color=primary")
                        # 删除整组
                        ui.button("删除", on_click=lambda gs=gfiles: _delete_group(gs)).props("outline size=sm color=red")

    def _delete_group(gs):
        for f in gs:
            try:
                os.remove(f)
            except Exception:
                pass
        refresh()
        ui.notify("报告已删除", type="warning")

    def _download(fp):
        import base64
        try:
            with open(fp, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode("ascii")
            filename = os.path.basename(fp)
            ui.run_javascript(f"""
                const bytes = Uint8Array.from(atob('{b64}'), c => c.charCodeAt(0));
                const blob = new Blob([bytes], {{type: 'text/plain;charset=utf-8'}});
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url; a.download = '{filename}';
                document.body.appendChild(a); a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
            """)
        except Exception as e:
            ui.notify(f"下载失败: {e}", type="negative")

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
        ui.label("报告中心").classes("text-2xl font-bold")
        ui.label("所有生成的测试报告统一存放于此").classes("text-sm text-gray-500")
        list_container = ui.column().classes("w-full gap-2")
        refresh()


# ==================== 导航 ====================
def _nav_header(active: str):
    """顶栏导航。"""
    items = [
        ("/", "任务", "terminal"),
        ("/projects", "项目/资产", "folder"),
        ("/history", "历史会话", "history"),
        ("/reports", "报告中心", "description"),
        ("/settings", "设置", "settings"),
        ("/agents", "Agent管理", "groups"),
        ("/skills", "Skills", "extension"),
        ("/rag", "RAG知识库", "library_books"),
    ]
    with ui.header().classes("bg-white text-gray-800 p-2 shadow-sm border-b border-gray-200 items-center"):
        with ui.row().classes("items-center gap-4"):
            ui.icon("security", size="md", color="black")
            ui.label("AutoPT").classes("text-lg font-bold tracking-wider text-gray-900")
            for path, label, icon in items:
                cls = "bg-gray-900 text-white" if path == active else ""
                ui.link(label, path).classes(
                    f"px-3 py-1 rounded text-sm font-medium {cls} no-underline"
                )
