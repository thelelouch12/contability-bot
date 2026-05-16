import asyncio
import logging
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


class NotAComprobante(Exception):
    """La imagen no es un comprobante de transferencia. Se ignora."""


class BotApp:
    def __init__(self):
        self.settings = load_settings()
        self.tz = ZoneInfo(self.settings.timezone)
        self.gemini = GeminiClient(self.settings.gemini_api_key, self.settings.gemini_model, self.settings.gemini_rpm)
        self.sheets = SheetsClient(self.settings.service_account_file, self.settings.sheet_id)
        self._pending: dict[str, list[Message]] = {}
        self._pending_tasks: dict[str, asyncio.Task] = {}
        self._pending_lock = asyncio.Lock()

    def _chat_allowed(self, chat_id: int) -> bool:
        if not self.settings.allowed_chat_ids:
            return True
        return chat_id in self.settings.allowed_chat_ids

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

    async def handle_photo(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        chat = update.effective_chat

        if not self._chat_allowed(chat.id):
            logger.warning("Chat no permitido: %s (%s)", chat.id, chat.title)
            return
        if not msg.photo:
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
        ignored = sum(1 for r in results if isinstance(r, NotAComprobante))
        err = len(results) - ok - ignored

        if err:
            first_err = next(r for r in results if isinstance(r, Exception) and not isinstance(r, NotAComprobante))
            logger.error("Errores en batch (%d/%d): %s", err, len(results), first_err)

        reply = self._summary_reply(ok, ignored, err, results)
        if reply:
            await messages[0].reply_text(reply)

    async def _process_one(self, msg: Message) -> Transaccion:
        photo = msg.photo[-1]
        tg_file = await photo.get_file()
        image_bytes = bytes(await tg_file.download_as_bytearray())

        tx = await self.gemini.extract(image_bytes)
        logger.info("Gemini extrajo: %s", tx.model_dump())

        # Skip si Gemini marca que no es un comprobante real (transferencia efectiva)
        if not tx.es_comprobante or tx.valor == 0:
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
            first = next((r for r in results if isinstance(r, BaseException) and not isinstance(r, NotAComprobante)), None)
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
        app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        return app


def main() -> None:
    bot = BotApp()
    app = bot.build()
    logger.info("Bot iniciado, esperando mensajes...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
