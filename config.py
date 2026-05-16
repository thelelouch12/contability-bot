import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Falta variable de entorno: {name}")
    return value


def _resolve_service_account_file() -> str:
    """Si GOOGLE_SERVICE_ACCOUNT_JSON está seteada (deploy en cloud), escribirla a un archivo
    temporal y devolver la ruta. Sino usar GOOGLE_SERVICE_ACCOUNT_FILE como antes (dev local)."""
    json_blob = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if json_blob:
        path = "/tmp/sa.json"
        with open(path, "w") as f:
            f.write(json_blob)
        os.chmod(path, 0o600)
        return path
    return _required("GOOGLE_SERVICE_ACCOUNT_FILE")


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    allowed_chat_ids: set[int]
    gemini_api_key: str
    gemini_model: str
    gemini_rpm: int
    service_account_file: str
    sheet_id: str
    timezone: str
    default_currency: str


def load_settings() -> Settings:
    raw_chats = os.getenv("ALLOWED_CHAT_IDS", "").strip()
    allowed = {int(x) for x in raw_chats.split(",") if x.strip()} if raw_chats else set()

    return Settings(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        allowed_chat_ids=allowed,
        gemini_api_key=_required("GEMINI_API_KEY"),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"),
        gemini_rpm=int(os.getenv("GEMINI_RPM", "14")),
        service_account_file=_resolve_service_account_file(),
        sheet_id=_required("GOOGLE_SHEET_ID"),
        timezone=os.getenv("TIMEZONE", "America/Bogota"),
        default_currency=os.getenv("DEFAULT_CURRENCY", "COP"),
    )


settings = load_settings() if os.getenv("TELEGRAM_BOT_TOKEN") else None
