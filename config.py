# config.py (支持 base_url)
import os
from pathlib import Path
from dotenv import load_dotenv

class Config:
    """配置管理类"""
    
    def __init__(self):
        # 加载 .env
        env_path = Path(__file__).parent / ".env"
        load_dotenv(dotenv_path=env_path)
        
        # 路径配置
        self.TOOLS_ROOT_DIR = os.getenv("TOOLS_ROOT_DIR", "./aptools/")
        self.LOG_FILE_PATH = os.getenv("LOG_FILE_PATH", "./mission.log")
        self.REPORT_FILE_PATH = os.getenv("REPORT_FILE_PATH", "./final_report.md")
        
        # 全局API配置
        self.OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
        self.OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        
        # 验证必要配置
        if not self.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY not found in .env file!")
    
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
