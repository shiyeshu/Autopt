# config.py (支持 base_url)
# 架构3：配置集中化 —— 所有路径基于 PROJECT_ROOT 计算，.env 只保留密钥与覆盖项
import os
from pathlib import Path
from dotenv import load_dotenv


class Config:
    """配置管理类"""

    def __init__(self):
        # 项目根目录：本文件所在目录的上一级（config.py 在项目根）
        self.PROJECT_ROOT = Path(__file__).resolve().parent
        self.TEAM_DIR = self.PROJECT_ROOT

        # 加载 .env（固定从项目根加载，不依赖 CWD）
        env_path = self.PROJECT_ROOT / ".env"
        load_dotenv(dotenv_path=env_path, override=False)

        # 路径配置：全部解析为基于 PROJECT_ROOT 的绝对路径
        self.TOOLS_ROOT_DIR = self._resolve_path_env("TOOLS_ROOT_DIR", "aptools")
        self.LOG_FILE_PATH = self._resolve_path_env("LOG_FILE_PATH", "mission.log")
        self.REPORT_FILE_PATH = self._resolve_path_env("REPORT_FILE_PATH", "final_report.md")
        self.UI_LOG_FILE_PATH = self._resolve_path_env("UI_LOG_FILE_PATH", "ui_mission.log")

        # 全局API配置
        self.OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
        self.OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

        # 验证必要配置
        if not self.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY not found in .env file!")

    def _resolve_path_env(self, key: str, default_rel: str) -> str:
        """将环境变量中的路径解析为绝对路径；空值/相对值均以 PROJECT_ROOT 为基准。"""
        raw = os.getenv(key, "").strip()
        if not raw:
            return str((self.PROJECT_ROOT / default_rel).resolve())
        p = Path(raw)
        if p.is_absolute():
            return str(p.resolve())
        # 相对路径：兼容 ".\aptools\"、"./mission.log"、纯相对 "aptools"
        return str((self.PROJECT_ROOT / p).resolve())

    def get_timestamped_report_paths(self, target: str) -> tuple[str, str]:
        """生成带时间戳的报告文件名（问题3: 避免多轮任务覆盖同名报告）。

        返回: (markdown路径, html路径)，如:
        D:/project/autopt/reports/autopt_20260818_011723_www.baidu.com.md
        """
        from datetime import datetime
        import re as _re

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 目标清洗为安全文件名片段（保留可读性）
        safe_target = _re.sub(r"[^A-Za-z0-9_.-]+", "_", str(target)).strip("_")
        safe_target = (safe_target or "target")[:60]

        reports_dir = self.PROJECT_ROOT / "reports"
        md_path = reports_dir / f"autopt_{ts}_{safe_target}.md"
        html_path = reports_dir / f"autopt_{ts}_{safe_target}.html"
        return str(md_path), str(html_path)

    def get_agent_model(self, role: str) -> str:
        """获取指定角色的模型"""
        return os.getenv(f"{role}_MODEL", "gpt-4o")

    def get_agent_api_key(self, role: str) -> str:
        """获取指定角色的API Key"""
        return os.getenv(f"{role}_API_KEY", self.OPENAI_API_KEY)

    def get_agent_base_url(self, role: str) -> str:
        """获取指定角色的Base URL"""
        return os.getenv(f"{role}_BASE_URL", self.OPENAI_BASE_URL)


# 全局配置实例
config = Config()
