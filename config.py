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
    """Prioridad de fuentes de la SA:
       1. GOOGLE_SERVICE_ACCOUNT_JSON_B64  (recomendado para Coolify/cloud — a prueba de escapes)
       2. GOOGLE_SERVICE_ACCOUNT_JSON      (JSON crudo; frágil si pasa por shells/yaml)
       3. GOOGLE_SERVICE_ACCOUNT_FILE      (ruta a archivo, para dev local)
    Las opciones 1 y 2 escriben el JSON a /tmp/sa.json y devuelven esa ruta.
    """
    json_blob = None
    b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
    if b64:
        import base64
        json_blob = base64.b64decode(b64).decode("utf-8")
    else:
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
    allowed_user_ids: set[int]
    admin_user_ids: set[int]
    gemini_api_key: str
    gemini_model: str
    gemini_rpm: int
    service_account_file: str
    sheet_id: str
    timezone: str
    default_currency: str
    max_photo_bytes: int
    max_photo_pixels: int
    user_rate_per_min: int
    bypass_ttl_min: int
    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket_name: str
    r2_public_url: str


def load_settings() -> Settings:
    raw_chats = os.getenv("ALLOWED_CHAT_IDS", "").strip()
    allowed = {int(x) for x in raw_chats.split(",") if x.strip()} if raw_chats else set()
    raw_users = os.getenv("ALLOWED_USER_IDS", "").strip()
    allowed_users = {int(x) for x in raw_users.split(",") if x.strip()} if raw_users else set()
    raw_admins = os.getenv("ADMIN_USER_IDS", "").strip()
    admins = {int(x) for x in raw_admins.split(",") if x.strip()} if raw_admins else set()

    return Settings(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        allowed_chat_ids=allowed,
        allowed_user_ids=allowed_users,
        admin_user_ids=admins,
        gemini_api_key=_required("GEMINI_API_KEY"),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"),
        gemini_rpm=int(os.getenv("GEMINI_RPM", "14")),
        service_account_file=_resolve_service_account_file(),
        sheet_id=_required("GOOGLE_SHEET_ID"),
        timezone=os.getenv("TIMEZONE", "America/Bogota"),
        default_currency=os.getenv("DEFAULT_CURRENCY", "COP"),
        max_photo_bytes=int(os.getenv("MAX_PHOTO_BYTES", str(5 * 1024 * 1024))),
        max_photo_pixels=int(os.getenv("MAX_PHOTO_PIXELS", str(50_000_000))),
        user_rate_per_min=int(os.getenv("USER_RATE_PER_MIN", "60")),
        bypass_ttl_min=int(os.getenv("BYPASS_TTL_MIN", "30")),
        r2_account_id=os.getenv("R2_ACCOUNT_ID", "").strip(),
        r2_access_key_id=os.getenv("R2_ACCESS_KEY_ID", "").strip(),
        r2_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY", "").strip(),
        r2_bucket_name=os.getenv("R2_BUCKET_NAME", "").strip(),
        r2_public_url=os.getenv("R2_PUBLIC_URL", "").strip().rstrip("/"),
    )


settings = load_settings() if os.getenv("TELEGRAM_BOT_TOKEN") else None
