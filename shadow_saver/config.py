import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    wechat_token: str
    public_base_url: str
    database_path: str = "shadow_saver.db"
    wechat_app_id: str = ""
    wechat_app_secret: str = ""
    deepseek_api_key: str = ""
    jd_app_key: str = ""
    jd_app_secret: str = ""
    jd_site_id: str = ""
    wechat_encoding_aes_key: str = ""
    taobao_app_key: str = ""
    taobao_app_secret: str = ""
    taobao_pid: str = ""
    taobao_activity_id: str = ""
    taobao_activity_title: str = ""
    taobao_activity_url: str = ""
    taobao_activity_token: str = ""
    taobao_activity_expires: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("WECHAT_TOKEN", "")
        base_url = os.environ.get("PUBLIC_BASE_URL", "")
        if not token or not base_url.startswith("https://"):
            raise RuntimeError("WECHAT_TOKEN and HTTPS PUBLIC_BASE_URL are required")
        return cls(
            token,
            base_url.rstrip("/"),
            os.environ.get("DATABASE_PATH", "shadow_saver.db"),
            os.environ.get("WECHAT_APP_ID", ""),
            os.environ.get("WECHAT_APP_SECRET", ""),
            os.environ.get("DEEPSEEK_API_KEY", ""),
            os.environ.get("JD_APP_KEY", ""),
            os.environ.get("JD_APP_SECRET", ""),
            os.environ.get("JD_SITE_ID", ""),
            os.environ.get("WECHAT_ENCODING_AES_KEY", ""),
            os.environ.get("TAOBAO_APP_KEY", ""),
            os.environ.get("TAOBAO_APP_SECRET", ""),
            os.environ.get("TAOBAO_PID", ""),
            os.environ.get("TAOBAO_ACTIVITY_ID", ""),
            os.environ.get("TAOBAO_ACTIVITY_TITLE", ""),
            os.environ.get("TAOBAO_ACTIVITY_URL", ""),
            os.environ.get("TAOBAO_ACTIVITY_TOKEN", ""),
            os.environ.get("TAOBAO_ACTIVITY_EXPIRES", ""),
        )

