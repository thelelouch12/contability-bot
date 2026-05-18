import asyncio
import logging
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Message, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config import load_settings
from gemini_client import GeminiClient
from models import Transaccion
from sheets_client import SheetsClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("contability-bot")

BATCH_DEBOUNCE_SEC = 2.0

# Magic bytes de formatos de imagen aceptados. Defensa contra polyglot/MIME spoof:
# si los primeros bytes no coinciden con un formato real, rechazamos antes de pasarlo a Gemini.
_IMAGE_MAGIC = (
    b"\xff\xd8\xff",                  # JPEG
    b"\x89PNG\r\n\x1a\n",             # PNG
    b"GIF87a", b"GIF89a",             # GIF (raro en comprobantes pero válido)
    b"BM",                            # BMP
)


def _looks_like_image(data: bytes) -> bool:
    if len(data) < 12:
        return False
    if data.startswith(_IMAGE_MAGIC):
        return True
    # WebP usa contenedor RIFF: "RIFF....WEBP". Validar el subtipo descarta WAV/AVI.
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


class NotAComprobante(Exception):
    """La imagen no es un comprobante de transferencia. Se ignora."""


class PhotoRejected(Exception):
    """Foto rechazada por validación de seguridad. Se ignora silenciosamente."""


class BotApp:
    def __init__(self):
        self.settings = load_settings()
        self.tz = ZoneInfo(self.settings.timezone)
        self.gemini = GeminiClient(self.settings.gemini_api_key, self.settings.gemini_model, self.settings.gemini_rpm)
        self.sheets = SheetsClient(self.settings.service_account_file, self.settings.sheet_id)
        self._pending: dict[str, list[Message]] = {}
        self._pending_tasks: dict[str, asyncio.Task] = {}
        self._pending_lock = asyncio.Lock()
        # Rate limit: sliding window 60s por user_id
        self._user_hits: dict[int, deque[float]] = {}
        # Bypass del clasificador es_comprobante, SCOPED POR chat_id.
        # Activado con /bypass on en un chat → afecta solo a ese chat.
        # Si activás bypass en tu DM, NO afecta fotos del grupo (y viceversa).
        self._bypass_until: dict[int, float] = {}

    def _chat_allowed(self, chat_id: int) -> bool:
        if not self.settings.allowed_chat_ids:
            return True
        return chat_id in self.settings.allowed_chat_ids

    def _user_allowed(self, user_id: int | None) -> bool:
        if not self.settings.allowed_user_ids:
            return True
        return user_id is not None and user_id in self.settings.allowed_user_ids

    def _rate_limit_ok(self, user_id: int) -> bool:
        now = time.monotonic()
        window = 60.0
        hits = self._user_hits.setdefault(user_id, deque())
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= self.settings.user_rate_per_min:
            return False
        hits.append(now)
        return True

    def _is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.settings.admin_user_ids

    def _bypass_active(self, chat_id: int) -> bool:
        return time.monotonic() < self._bypass_until.get(chat_id, 0.0)

    def _bypass_seconds_left(self, chat_id: int) -> int:
        return max(0, int(self._bypass_until.get(chat_id, 0.0) - time.monotonic()))

    @staticmethod
    def _telegram_image_ref(chat_id: int, message_id: int, file_id: str) -> str:
        if chat_id < -1_000_000_000_000:
            short = str(abs(chat_id))[3:]
            return f"https://t.me/c/{short}/{message_id}"
        return f"tg:{file_id}"

    async def cmd_id(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        await update.message.reply_text(
            f"chat_id: {chat.id}\nchat_title: {chat.title or chat.username or 'privado'}\n"
            f"tu user_id: {user.id}"
        )

    async def cmd_start(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "Hola! Envíame fotos de comprobantes y los registraré en el Excel."
        )

    async def cmd_bypass(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Toggle del modo sin-clasificador (admin-only). SCOPED al chat donde se invoca.
        /bypass on  → activa por BYPASS_TTL_MIN minutos SOLO en este chat
        /bypass off → desactiva en este chat
        /bypass     → muestra estado de este chat
        """
        user = update.effective_user
        chat = update.effective_chat
        if not self._is_admin(user.id if user else None):
            logger.warning("Intento de /bypass por no-admin: user=%s chat=%s",
                           user.id if user else None, chat.id)
            return  # silencio total para no dar pistas

        args = [a.lower() for a in (ctx.args or [])]
        action = args[0] if args else "status"
        ttl_min = self.settings.bypass_ttl_min
        scope = "este DM" if chat.type == "private" else f"el grupo «{chat.title or chat.id}»"

        if action in ("on", "activar", "1", "true"):
            self._bypass_until[chat.id] = time.monotonic() + ttl_min * 60
            logger.warning("BYPASS activado por user=%s en chat=%s (%s) por %d min",
                           user.id, chat.id, chat.title or "DM", ttl_min)
            await update.message.reply_text(
                f"⚠️ BYPASS ACTIVO en {scope} por {ttl_min} min.\n"
                f"Las fotos enviadas AQUÍ se registrarán sin filtrar por es_comprobante.\n"
                f"Otros chats NO se ven afectados.\n"
                f"Se desactiva solo a los {ttl_min} min o con /bypass off."
            )
        elif action in ("off", "desactivar", "0", "false"):
            was_active = self._bypass_active(chat.id)
            self._bypass_until.pop(chat.id, None)
            logger.info("BYPASS desactivado en chat=%s por user=%s (estaba_activo=%s)",
                        chat.id, user.id, was_active)
            await update.message.reply_text(
                f"✅ Bypass desactivado en {scope}. Clasificador vuelve a estar activo."
                if was_active else f"Bypass ya estaba inactivo en {scope}."
            )
        else:  # status
            if self._bypass_active(chat.id):
                left = self._bypass_seconds_left(chat.id)
                await update.message.reply_text(
                    f"⚠️ Bypass ACTIVO en {scope}. Expira en {left//60}m {left%60}s.\n"
                    f"Uso: /bypass off para desactivar."
                )
            else:
                await update.message.reply_text(
                    f"Bypass INACTIVO en {scope} (clasificador normal).\n"
                    f"Uso: /bypass on para activar por {ttl_min} min SOLO aquí."
                )

    async def handle_photo(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        chat = update.effective_chat
        user = msg.from_user

        if not self._chat_allowed(chat.id):
            logger.warning("Chat no permitido: %s (%s)", chat.id, chat.title)
            return
        if not self._user_allowed(user.id if user else None):
            logger.warning("Usuario no permitido en grupo: user_id=%s name=%s chat=%s",
                           user.id if user else None,
                           user.full_name if user else None,
                           chat.id)
            return
        if not msg.photo:
            return
        if not self._rate_limit_ok(user.id):
            logger.warning("Rate limit excedido: user_id=%s (%d/min)",
                           user.id, self.settings.user_rate_per_min)
            return

        # Validar tamaño y dimensiones desde metadata de Telegram (sin descargar todavía).
        photo = msg.photo[-1]  # variante de mayor resolución
        if photo.file_size and photo.file_size > self.settings.max_photo_bytes:
            logger.warning("Foto demasiado grande: %d bytes (max=%d) user=%s",
                           photo.file_size, self.settings.max_photo_bytes, user.id)
            return
        if photo.width and photo.height:
            pixels = photo.width * photo.height
            if pixels > self.settings.max_photo_pixels:
                logger.warning("Foto con demasiados pixeles: %dx%d=%d (max=%d) user=%s",
                               photo.width, photo.height, pixels,
                               self.settings.max_photo_pixels, user.id)
                return

        group_key = msg.media_group_id or f"solo:{msg.message_id}"
        async with self._pending_lock:
            self._pending.setdefault(group_key, []).append(msg)
            old = self._pending_tasks.get(group_key)
            if old and not old.done():
                old.cancel()
            self._pending_tasks[group_key] = asyncio.create_task(
                self._flush_group_after(group_key, BATCH_DEBOUNCE_SEC)
            )

    async def _flush_group_after(self, group_key: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        async with self._pending_lock:
            messages = self._pending.pop(group_key, [])
            self._pending_tasks.pop(group_key, None)
        if not messages:
            return
        await self._process_batch(messages)

    async def _process_batch(self, messages: list[Message]) -> None:
        results = await asyncio.gather(
            *[self._process_one(m) for m in messages],
            return_exceptions=True,
        )
        ok = sum(1 for r in results if not isinstance(r, Exception))
        ignored = sum(1 for r in results if isinstance(r, (NotAComprobante, PhotoRejected)))
        err = len(results) - ok - ignored

        if err:
            first_err = next(
                r for r in results
                if isinstance(r, Exception) and not isinstance(r, (NotAComprobante, PhotoRejected))
            )
            logger.error("Errores en batch (%d/%d): %s", err, len(results), first_err)

        reply = self._summary_reply(ok, ignored, err, results)
        if reply:
            await messages[0].reply_text(reply)

    async def _process_one(self, msg: Message) -> Transaccion:
        photo = msg.photo[-1]
        tg_file = await photo.get_file()
        image_bytes = bytes(await tg_file.download_as_bytearray())

        # Defensa: validar segundo bound de tamaño después de descargar.
        if len(image_bytes) > self.settings.max_photo_bytes:
            logger.warning("Foto descargada excede límite: %d bytes (max=%d) msg=%s",
                           len(image_bytes), self.settings.max_photo_bytes, msg.message_id)
            raise PhotoRejected("imagen muy grande")
        # Defensa: rechazar si los magic bytes no son de un formato de imagen aceptado.
        if not _looks_like_image(image_bytes):
            logger.warning("Bytes no parecen una imagen válida (head=%s) msg=%s user=%s",
                           image_bytes[:16].hex(), msg.message_id,
                           msg.from_user.id if msg.from_user else None)
            raise PhotoRejected("formato no reconocido")

        tx = await self.gemini.extract(image_bytes)
        logger.info("Gemini extrajo: %s", tx.model_dump())

        # BYPASS: si está activo en ESTE chat (admin lo prendió con /bypass on aquí),
        # saltarse el filtro y forzar el registro. Scope por chat_id evita que el
        # bypass de un chat (DM) afecte fotos de otro chat (grupo).
        if self._bypass_active(msg.chat.id):
            logger.warning("BYPASS activo en chat=%s — registrando sin filtrar es_comprobante (msg=%s)",
                           msg.chat.id, msg.message_id)
        elif not tx.es_comprobante or tx.valor == 0:
            logger.info("Skip (no es comprobante): %s", tx.notas_ocr)
            raise NotAComprobante(tx.notas_ocr or "No parece un comprobante")

        sender = msg.from_user.full_name if msg.from_user else "Desconocido"
        if msg.from_user and msg.from_user.username:
            sender = f"{sender} (@{msg.from_user.username})"

        when = (msg.date or datetime.utcnow()).astimezone(self.tz)
        image_ref = self._telegram_image_ref(msg.chat.id, msg.message_id, photo.file_id)

        await asyncio.to_thread(
            self.sheets.append_transaction,
            tx,
            message_id=msg.message_id,
            sender=sender,
            when=when,
            image_link=image_ref,
        )
        return tx

    @staticmethod
    def _summary_reply(ok: int, ignored: int, err: int, results: list) -> str:
        parts = []
        if ok:
            parts.append(f"✅ {ok} registrada{'s' if ok != 1 else ''}")
        if ignored:
            parts.append(f"⏭️ {ignored} ignorada{'s' if ignored != 1 else ''} (no comprobante)")
        if err:
            first = next(
                (r for r in results
                 if isinstance(r, BaseException)
                 and not isinstance(r, (NotAComprobante, PhotoRejected))),
                None,
            )
            reason = BotApp._friendly_error(first) if first else "error"
            parts.append(f"❌ {err} con error: {reason}")
        # Si todo fue ignorado (típicamente 1 foto random en el grupo), mejor no responder nada para no hacer ruido
        if not ok and not err and ignored:
            return ""
        return " · ".join(parts) if parts else ""

    @staticmethod
    def _friendly_error(e: BaseException) -> str:
        text = str(e)
        if "UNAVAILABLE" in text or "503" in text:
            return "Gemini sobrecargado, reenvía"
        if "RESOURCE_EXHAUSTED" in text or "429" in text:
            return "Límite de Gemini alcanzado, espera un momento"
        if "PERMISSION_DENIED" in text or "403" in text:
            return "Permiso denegado en Google"
        return text[:120]

    def build(self) -> Application:
        app = Application.builder().token(self.settings.telegram_bot_token).build()
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("id", self.cmd_id))
        app.add_handler(CommandHandler("bypass", self.cmd_bypass))
        app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        return app


def main() -> None:
    bot = BotApp()
    app = bot.build()
    logger.info("Bot iniciado, esperando mensajes...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import sys
    import time
    import traceback

    try:
        main()
    except Exception:
        # Mantener el container vivo 5min al crashear para poder leer logs
        print("=" * 60, flush=True)
        print("FATAL ERROR — bot.py crasheó en startup:", flush=True)
        traceback.print_exc()
        print("=" * 60, flush=True)
        print("Sleeping 5min antes de exit para que se puedan leer los logs...", flush=True)
        sys.stdout.flush()
        sys.stderr.flush()
        time.sleep(300)
        sys.exit(1)
