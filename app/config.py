import os
import json
from typing import List, Dict, Any, Optional
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    HOST: str = "0.0.0.0"
    PORT: int = 7860
    
    # 单账号降级兼容配置（若未配置 accounts.json 则使用此项）
    TENCENT_COOKIE: str = ""
    DEFAULT_CHAT_ID: str = "dap8gfk2c3m4o8kl0ld0"
    
    # 多账号配置文件路径
    ACCOUNTS_FILE: str = "configs/accounts.json"
    
    # 代理访问密钥（留空则不开启验证，支持逗号分隔多个 key）
    API_KEYS: str = ""
    
    # 生图超时时间（秒）
    REQUEST_TIMEOUT: float = 120.0
    
    # 默认模型名称
    DEFAULT_MODEL: str = "HY-Image-3.5-Preview-4090-Tob-v1.1"

    @property
    def valid_api_keys(self) -> List[str]:
        if not self.API_KEYS.strip():
            return []
        return [k.strip() for k in self.API_KEYS.split(",") if k.strip()]

    def load_accounts(self) -> List[Dict[str, Any]]:
        """
        从 configs/accounts.json 加载多账号，如果不存在则使用单账号降级配置
        """
        accounts = []
        if os.path.exists(self.ACCOUNTS_FILE):
            try:
                with open(self.ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        for idx, item in enumerate(data):
                            if item.get("enabled", True) and item.get("cookie"):
                                accounts.append({
                                    "name": item.get("name", f"account_{idx + 1}"),
                                    "cookie": item["cookie"],
                                    "chat_id": item.get("chat_id", self.DEFAULT_CHAT_ID)
                                })
            except Exception as e:
                print(f"[WARN] 读取多账号文件 {self.ACCOUNTS_FILE} 失败: {e}")

        # 如果没有多账号文件或内容为空，但配置了环境变量 TENCENT_COOKIE，则作为单账号载入
        if not accounts and self.TENCENT_COOKIE:
            accounts.append({
                "name": "default_account",
                "cookie": self.TENCENT_COOKIE,
                "chat_id": self.DEFAULT_CHAT_ID
            })

        return accounts

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

settings = Settings()
