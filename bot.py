import asyncio
import html
import logging
import time
import uuid
from dataclasses import dataclass, field
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import load_settings
from gemini_client import GeminiClient
from models import EstadoTransaccion, TipoCuenta, Transaccion
from r2_client import R2Client
from sheets_client import SheetsClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("contability-bot")

BATCH_DEBOUNCE_SEC = 2.0
# Timeout per-foto. Si una foto se cuelga (ej. Gemini en retry infinito), no debe
# bloquear el reply del batch entero. Acomoda los 5 reintentos de Gemini (backoff
# exponencial acumulado ~31s + tiempo de cada call).
PER_PHOTO_TIMEOUT_SEC = 90.0

# Magic bytes de formatos de imagen aceptados. Defensa contra polyglot/MIME spoof:
# si los primeros bytes no coinciden con un formato real, rechazamos antes de pasarlo a Gemini.
_IMAGE_MAGIC = (
    b"\xff\xd8\xff",                  # JPEG
    b"\x89PNG\r\n\x1a\n",             # PNG
    b"GIF87a", b"GIF89a",             # GIF (raro en comprobantes pero válido)
    b"BM",                            # BMP
)

# Campos editables vía botón. (id_interno, etiqueta visible).
# `estado` y `es_comprobante` no están aquí: tienen sus propios botones de acción rápida.
EDITABLE_FIELDS: list[tuple[str, str]] = [
    ("banco", "Banco"),
    ("codigo_transaccion", "Código"),
    ("destino_nombre", "Destino: Nombre"),
    ("destino_numero", "Destino: Número"),
    ("destino_tipo", "Destino: Tipo"),
    ("valor", "Valor"),
    ("moneda", "Moneda"),
    ("fecha_comprobante", "Fecha comprobante"),
    ("notas_ocr", "Notas OCR"),
]

# Orden de ciclado del botón "🔁 Estado".
_STATE_CYCLE = [
    EstadoTransaccion.EXITOSA,
    EstadoTransaccion.PENDIENTE,
    EstadoTransaccion.FALLIDA,
]


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


@dataclass
class PendingReview:
    """Revisión pendiente de aprobación por un admin.

    Vive en memoria (`BotApp._reviews`) — se pierde si el bot se reinicia.
    `tx` se muta vía botones/edición; al aceptar se escribe a Sheets.
    Conservamos `image_bytes` para no re-descargar de Telegram al subir a R2.
    """

    review_id: str
    tx: Transaccion
    chat_id: int
    photo_message_id: int
    photo_file_id: str
    sender_full_name: str
    sender_username: str | None
    sender_user_id: int
    photo_date: datetime
    image_ref: str
    image_bytes: bytes | None = None
    review_message_id: int | None = None
    awaiting_edit_field: str | None = None
    awaiting_edit_admin_id: int | None = None
    created_at: float = field(default_factory=time.monotonic)


class BotApp:
    def __init__(self):
        self.settings = load_settings()
        self.tz = ZoneInfo(self.settings.timezone)
        self.gemini = GeminiClient(self.settings.gemini_api_key, self.settings.gemini_model, self.settings.gemini_rpm)
        self.sheets = SheetsClient(self.settings.service_account_file, self.settings.sheet_id)
        # R2 opcional — si las creds no están, image_link queda como link de Telegram.
        if R2Client.is_configured(self.settings):
            self.r2: R2Client | None = R2Client(
                self.settings.r2_account_id,
                self.settings.r2_access_key_id,
                self.settings.r2_secret_access_key,
                self.settings.r2_bucket_name,
                self.settings.r2_public_url,
            )
        else:
            self.r2 = None
            logger.info("R2 no configurado — usando link de Telegram como image_link")
        self._pending: dict[str, list[Message]] = {}
        self._pending_tasks: dict[str, asyncio.Task] = {}
        self._pending_lock = asyncio.Lock()
        # Rate limit: sliding window 60s por user_id
        self._user_hits: dict[int, deque[float]] = {}
        # Bypass del clasificador es_comprobante, SCOPED POR chat_id.
        # Cuando está activo se escribe directo a Sheets, salteando el flujo de review.
        self._bypass_until: dict[int, float] = {}
        # Reviews pendientes — key = review_id (UUID4 trunc 8 chars).
        self._reviews: dict[str, PendingReview] = {}
        self._reviews_lock = asyncio.Lock()

    # ------------------------------- helpers básicos -------------------------------

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

    # ------------------------------- comandos -------------------------------

    async def cmd_id(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        await update.message.reply_text(
            f"chat_id: {chat.id}\nchat_title: {chat.title or chat.username or 'privado'}\n"
            f"tu user_id: {user.id}"
        )

    async def cmd_start(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "Hola! Envíame fotos de comprobantes. Cada una abrirá una tarjeta de revisión "
            "que un admin debe aceptar, rechazar o editar antes de registrarse."
        )

    async def cmd_bypass(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Toggle del modo auto-registrar (admin-only). SCOPED al chat donde se invoca.
        Con bypass ACTIVO las fotos se registran directamente sin tarjeta de revisión —
        útil para subir lotes en local sin tener que aprobarlos uno por uno.
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
                f"Las fotos enviadas AQUÍ se registrarán DIRECTAMENTE sin pasar por revisión.\n"
                f"Otros chats NO se ven afectados.\n"
                f"Se desactiva solo a los {ttl_min} min o con /bypass off."
            )
        elif action in ("off", "desactivar", "0", "false"):
            was_active = self._bypass_active(chat.id)
            self._bypass_until.pop(chat.id, None)
            logger.info("BYPASS desactivado en chat=%s por user=%s (estaba_activo=%s)",
                        chat.id, user.id, was_active)
            await update.message.reply_text(
                f"✅ Bypass desactivado en {scope}. Volvió el flujo de revisión normal."
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
                    f"Bypass INACTIVO en {scope} (flujo de revisión normal).\n"
                    f"Uso: /bypass on para activar por {ttl_min} min SOLO aquí."
                )

    # ------------------------------- recepción de fotos -------------------------------

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
        # Cada foto con su propio timeout — una colgada NO debe bloquear el reply del batch.
        async def run_one(m: Message):
            try:
                return await asyncio.wait_for(self._process_one(m), timeout=PER_PHOTO_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                logger.error("Timeout (%.0fs) procesando msg=%s — abortada",
                             PER_PHOTO_TIMEOUT_SEC, m.message_id)
                raise

        results = await asyncio.gather(*[run_one(m) for m in messages], return_exceptions=True)
        auto = sum(1 for r in results if r == "auto_written")
        review = sum(1 for r in results if r == "review_created")
        ignored = sum(1 for r in results if isinstance(r, PhotoRejected))
        err = sum(
            1 for r in results
            if isinstance(r, BaseException) and not isinstance(r, PhotoRejected)
        )

        if err:
            first_err = next(
                r for r in results
                if isinstance(r, BaseException) and not isinstance(r, PhotoRejected)
            )
            logger.error("Errores en batch (%d/%d): %s", err, len(results), first_err)

        # Reply consolidado SOLO si:
        #  - hubo registros auto (bypass) → confirmar la cantidad
        #  - hubo errores → reportar
        # Si todas fueron a review, cada tarjeta ya es su propio reply → no decimos nada.
        reply = self._summary_reply(auto, review, ignored, err, results)
        if reply:
            await messages[0].reply_text(reply)

    async def _process_one(self, msg: Message) -> str:
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

        sender = msg.from_user.full_name if msg.from_user else "Desconocido"
        username = msg.from_user.username if msg.from_user else None
        when = (msg.date or datetime.utcnow()).astimezone(self.tz)
        image_ref = self._telegram_image_ref(msg.chat.id, msg.message_id, photo.file_id)

        # BYPASS: escribir directo a Sheets, sin pasar por review.
        if self._bypass_active(msg.chat.id):
            logger.warning("BYPASS activo en chat=%s — registrando sin revisión (msg=%s)",
                           msg.chat.id, msg.message_id)
            sender_display = f"{sender} (@{username})" if username else sender
            link = await self._upload_or_fallback(image_bytes, when, tx,
                                                  msg.message_id, image_ref)
            await asyncio.to_thread(
                self.sheets.append_transaction,
                tx,
                message_id=msg.message_id,
                sender=sender_display,
                when=when,
                image_link=link,
            )
            return "auto_written"

        # Flujo normal: crear tarjeta de revisión.
        await self._create_review_card(
            msg, tx, sender, username, when, image_ref, image_bytes,
        )
        return "review_created"

    async def _upload_or_fallback(
        self,
        image_bytes: bytes | None,
        when: datetime,
        tx: Transaccion,
        message_id: int,
        fallback: str,
    ) -> str:
        """Sube a R2 si está configurado. Si falla o no está, devuelve `fallback`."""
        if not self.r2 or not image_bytes:
            return fallback
        try:
            return await asyncio.to_thread(
                self.r2.upload_receipt, image_bytes, when, tx,
                telegram_msg_id=message_id,
            )
        except Exception as e:
            logger.error("R2 upload falló (msg=%s): %s — uso fallback %s",
                         message_id, e, fallback)
            return fallback

    async def _create_review_card(
        self,
        photo_msg: Message,
        tx: Transaccion,
        sender_full_name: str,
        sender_username: str | None,
        when: datetime,
        image_ref: str,
        image_bytes: bytes,
    ) -> None:
        rid = uuid.uuid4().hex[:8]
        review = PendingReview(
            review_id=rid,
            tx=tx,
            chat_id=photo_msg.chat.id,
            photo_message_id=photo_msg.message_id,
            photo_file_id=photo_msg.photo[-1].file_id,
            sender_full_name=sender_full_name,
            sender_username=sender_username,
            sender_user_id=photo_msg.from_user.id if photo_msg.from_user else 0,
            photo_date=when,
            image_ref=image_ref,
            image_bytes=image_bytes,
        )
        text, kb = self._render_card(review)
        sent = await photo_msg.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        review.review_message_id = sent.message_id
        async with self._reviews_lock:
            self._reviews[rid] = review
        logger.info("Review %s creada para msg=%s en chat=%s", rid, photo_msg.message_id, photo_msg.chat.id)

    @staticmethod
    def _summary_reply(auto: int, review: int, ignored: int, err: int, results: list) -> str:
        parts = []
        if auto:
            parts.append(f"✅ {auto} registrada{'s' if auto != 1 else ''} (bypass)")
        if err:
            first = next(
                (r for r in results
                 if isinstance(r, BaseException) and not isinstance(r, PhotoRejected)),
                None,
            )
            reason = BotApp._friendly_error(first) if first else "error"
            parts.append(f"❌ {err} con error: {reason}")
        # Si todo fue review (sin auto/err) → no reply, las tarjetas hablan por sí mismas.
        # Si todo fue ignorado → no reply (foto random rechazada en filtro).
        if not auto and not err:
            return ""
        return " · ".join(parts)

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

    # ------------------------------- rendering de tarjeta -------------------------------

    def _render_card(self, r: PendingReview) -> tuple[str, InlineKeyboardMarkup]:
        """Texto HTML + teclado inline. Si está en awaiting_edit, muestra prompt y solo Cancelar."""
        tx = r.tx
        h = html.escape

        es_comp_icon = "✅" if tx.es_comprobante else "⚠️"
        es_comp_label = "Sí" if tx.es_comprobante else "<b>NO</b>"

        sender_line = h(r.sender_full_name)
        if r.sender_username:
            sender_line += f" (@{h(r.sender_username)})"

        try:
            valor_fmt = f"{tx.valor:,.0f}".replace(",", ".")
        except Exception:
            valor_fmt = str(tx.valor)

        lines = [
            f"{es_comp_icon} <b>Revisión pendiente</b>  <code>#{r.review_id}</code>",
            f"<i>De:</i> {sender_line}",
            f"<i>Hora:</i> {h(r.photo_date.strftime('%Y-%m-%d %H:%M'))}",
            "",
            f"<b>Banco:</b> {h(tx.banco)}",
            f"<b>Estado:</b> {h(tx.estado.value)}",
            f"<b>Código:</b> {h(tx.codigo_transaccion)}",
            f"<b>Destino:</b> {h(tx.destino_nombre)}",
            f"   ↳ N°: <code>{h(tx.destino_numero)}</code>",
            f"   ↳ Tipo: {h(tx.destino_tipo.value)}",
            f"<b>Valor:</b> {valor_fmt} {h(tx.moneda)}",
            f"<b>Fecha comp.:</b> {h(tx.fecha_comprobante or '—')}",
            f"<b>¿Es comprobante?:</b> {es_comp_label}",
        ]
        if tx.notas_ocr:
            lines.append("")
            lines.append(f"<i>OCR: {h(tx.notas_ocr)}</i>")

        if r.awaiting_edit_field:
            label = self._field_label(r.awaiting_edit_field)
            lines.append("")
            lines.append(
                f"✏️ <b>Editando «{h(label)}»</b> — el admin que pulsó debe enviar el "
                f"nuevo valor como mensaje en este chat. Pulsa Cancelar para abortar."
            )
            kb = [[InlineKeyboardButton("❌ Cancelar edición", callback_data=f"cnc:{r.review_id}")]]
            return "\n".join(lines), InlineKeyboardMarkup(kb)

        rid = r.review_id
        kb = [
            [
                InlineKeyboardButton("✅ Aceptar", callback_data=f"acc:{rid}"),
                InlineKeyboardButton("❌ Rechazar", callback_data=f"rej:{rid}"),
            ],
            [
                InlineKeyboardButton("🔁 Estado", callback_data=f"cyc:{rid}"),
                InlineKeyboardButton(
                    "📌 Es comp: " + ("Sí→No" if tx.es_comprobante else "No→Sí"),
                    callback_data=f"tog:{rid}",
                ),
            ],
            [InlineKeyboardButton("✏️ Editar campo", callback_data=f"edt:{rid}")],
        ]
        return "\n".join(lines), InlineKeyboardMarkup(kb)

    def _render_edit_menu(self, r: PendingReview) -> InlineKeyboardMarkup:
        """Teclado del submenú de edición: un botón por campo + volver."""
        rid = r.review_id
        rows: list[list[InlineKeyboardButton]] = []
        # 2 botones por fila
        buf: list[InlineKeyboardButton] = []
        for fid, label in EDITABLE_FIELDS:
            buf.append(InlineKeyboardButton(label, callback_data=f"efld:{rid}:{fid}"))
            if len(buf) == 2:
                rows.append(buf)
                buf = []
        if buf:
            rows.append(buf)
        rows.append([InlineKeyboardButton("⬅️ Volver", callback_data=f"bck:{rid}")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _field_label(field_id: str) -> str:
        for fid, label in EDITABLE_FIELDS:
            if fid == field_id:
                return label
        return field_id

    # ------------------------------- callback handlers -------------------------------

    async def handle_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.data is None:
            return
        user = q.from_user
        if not self._is_admin(user.id if user else None):
            await q.answer("Solo administradores pueden revisar.", show_alert=True)
            return

        parts = q.data.split(":", 2)
        action = parts[0]
        rid = parts[1] if len(parts) > 1 else None
        extra = parts[2] if len(parts) > 2 else None

        if not rid:
            await q.answer()
            return

        async with self._reviews_lock:
            r = self._reviews.get(rid)
        if r is None:
            await q.answer("Revisión expirada o ya resuelta.", show_alert=True)
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        if action == "acc":
            await self._accept_review(q, r, user)
        elif action == "rej":
            await self._reject_review(q, r, user)
        elif action == "cyc":
            await self._cycle_state(q, r)
        elif action == "tog":
            await self._toggle_comprobante(q, r)
        elif action == "edt":
            await self._show_edit_menu(q, r)
        elif action == "bck":
            await self._show_main_card(q, r)
        elif action == "efld":
            if not extra:
                await q.answer()
                return
            await self._start_field_edit(q, r, extra, user)
        elif action == "cnc":
            await self._cancel_field_edit(q, r)
        else:
            await q.answer("Acción desconocida")

    async def _accept_review(self, q, r: PendingReview, admin_user) -> None:
        sender_display = (
            f"{r.sender_full_name} (@{r.sender_username})"
            if r.sender_username else r.sender_full_name
        )
        # Subir a R2 con los datos actuales (posiblemente editados) del tx.
        link = await self._upload_or_fallback(
            r.image_bytes, r.photo_date, r.tx, r.photo_message_id, r.image_ref,
        )
        try:
            await asyncio.to_thread(
                self.sheets.append_transaction,
                r.tx,
                message_id=r.photo_message_id,
                sender=sender_display,
                when=r.photo_date,
                image_link=link,
            )
        except Exception as e:
            logger.error("Error escribiendo a Sheets para review %s: %s", r.review_id, e)
            await q.answer(f"Error guardando: {self._friendly_error(e)}", show_alert=True)
            return

        async with self._reviews_lock:
            self._reviews.pop(r.review_id, None)

        admin_name = self._admin_display(admin_user)
        await q.answer("Registrada.")
        try:
            await q.edit_message_text(
                self._closing_text(r, "✅", f"Registrada por {admin_name}"),
                reply_markup=None,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning("No pude editar mensaje de review %s: %s", r.review_id, e)
        logger.info("Review %s ACEPTADA por user=%s", r.review_id, admin_user.id)

    async def _reject_review(self, q, r: PendingReview, admin_user) -> None:
        async with self._reviews_lock:
            self._reviews.pop(r.review_id, None)
        admin_name = self._admin_display(admin_user)
        await q.answer("Rechazada.")
        try:
            await q.edit_message_text(
                self._closing_text(r, "🚫", f"Rechazada por {admin_name}"),
                reply_markup=None,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning("No pude editar mensaje de review %s: %s", r.review_id, e)
        logger.info("Review %s RECHAZADA por user=%s", r.review_id, admin_user.id)

    async def _cycle_state(self, q, r: PendingReview) -> None:
        try:
            idx = _STATE_CYCLE.index(r.tx.estado)
        except ValueError:
            idx = -1  # estado fuera del ciclo → arrancar desde el primero
        r.tx.estado = _STATE_CYCLE[(idx + 1) % len(_STATE_CYCLE)]
        await q.answer(f"Estado → {r.tx.estado.value}")
        await self._refresh_card(q, r)

    async def _toggle_comprobante(self, q, r: PendingReview) -> None:
        r.tx.es_comprobante = not r.tx.es_comprobante
        await q.answer(f"es_comprobante → {r.tx.es_comprobante}")
        await self._refresh_card(q, r)

    async def _show_edit_menu(self, q, r: PendingReview) -> None:
        if r.awaiting_edit_field:
            await q.answer("Hay una edición en curso. Cancela primero.", show_alert=True)
            return
        await q.answer()
        kb = self._render_edit_menu(r)
        try:
            await q.edit_message_reply_markup(reply_markup=kb)
        except Exception as e:
            logger.warning("No pude mostrar edit menu: %s", e)

    async def _show_main_card(self, q, r: PendingReview) -> None:
        await q.answer()
        await self._refresh_card(q, r)

    async def _start_field_edit(self, q, r: PendingReview, field_id: str, admin_user) -> None:
        if field_id not in {fid for fid, _ in EDITABLE_FIELDS}:
            await q.answer("Campo desconocido", show_alert=True)
            return
        if r.awaiting_edit_field:
            await q.answer("Ya hay una edición en curso.", show_alert=True)
            return
        r.awaiting_edit_field = field_id
        r.awaiting_edit_admin_id = admin_user.id

        hint = self._field_input_hint(field_id, r.tx)
        await q.answer(hint, show_alert=True)
        await self._refresh_card(q, r)
        logger.info("Review %s: admin=%s edita campo '%s'",
                    r.review_id, admin_user.id, field_id)

    async def _cancel_field_edit(self, q, r: PendingReview) -> None:
        if not r.awaiting_edit_field:
            await q.answer()
            await self._refresh_card(q, r)
            return
        r.awaiting_edit_field = None
        r.awaiting_edit_admin_id = None
        await q.answer("Edición cancelada.")
        await self._refresh_card(q, r)

    async def _refresh_card(self, q, r: PendingReview) -> None:
        text, kb = self._render_card(r)
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception as e:
            # Telegram lanza BadRequest "message is not modified" si nada cambió — ignorable.
            if "not modified" not in str(e).lower():
                logger.warning("No pude refrescar tarjeta %s: %s", r.review_id, e)

    @staticmethod
    def _admin_display(user) -> str:
        if user is None:
            return "admin"
        if user.username:
            return f"@{user.username}"
        return user.full_name or f"id:{user.id}"

    @staticmethod
    def _closing_text(r: PendingReview, icon: str, action_line: str) -> str:
        """Mensaje final cuando la review se cierra — solo línea de status + admin + ID."""
        return f"{icon} <b>{html.escape(action_line)}</b>  <code>#{r.review_id}</code>"

    # ------------------------------- edición de campo (texto) -------------------------------

    @staticmethod
    def _field_input_hint(field_id: str, tx: Transaccion) -> str:
        """Hint corto que se muestra en answer_callback (max ~200 chars)."""
        if field_id == "valor":
            return f"Envía el nuevo VALOR (número, ej. 250000). Actual: {tx.valor}"
        if field_id == "destino_tipo":
            opts = " / ".join(t.value for t in TipoCuenta)
            return f"Envía el TIPO de cuenta. Opciones: {opts}. Actual: {tx.destino_tipo.value}"
        if field_id == "moneda":
            return f"Envía la MONEDA (ej. COP, USD). Actual: {tx.moneda}"
        label = BotApp._field_label(field_id)
        current = getattr(tx, field_id, "") or "—"
        return f"Envía el nuevo valor para «{label}». Actual: {current}"

    @staticmethod
    def _apply_field_edit(tx: Transaccion, field_id: str, raw: str) -> None:
        """Muta tx in-place. Lanza ValueError con mensaje user-facing si el input es inválido."""
        value = raw.strip()
        if not value:
            raise ValueError("Vacío")

        if field_id == "valor":
            # quitar símbolos comunes ($, espacios, separadores de miles . o ,)
            cleaned = (
                value.replace("$", "").replace(" ", "")
                .replace(".", "").replace(",", "")
            )
            try:
                tx.valor = float(cleaned)
            except ValueError:
                raise ValueError(f"'{value}' no es un número")
            return

        if field_id == "destino_tipo":
            # case-insensitive match contra enum
            for t in TipoCuenta:
                if t.value.lower() == value.lower():
                    tx.destino_tipo = t
                    return
            opts = ", ".join(t.value for t in TipoCuenta)
            raise ValueError(f"Tipo inválido. Opciones: {opts}")

        if field_id in {
            "banco", "codigo_transaccion", "destino_nombre", "destino_numero",
            "moneda", "fecha_comprobante", "notas_ocr",
        }:
            setattr(tx, field_id, value)
            return

        raise ValueError(f"Campo no editable: {field_id}")

    async def handle_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Recibe el mensaje de texto con el nuevo valor de un campo en edición.
        Solo aplica si el sender es admin y tiene una review con awaiting_edit_admin_id=su id
        en el mismo chat. Si no, no-op (no rompe otros usos del chat).
        """
        msg = update.effective_message
        if msg is None or not msg.text:
            return
        user = msg.from_user
        chat = msg.chat
        if user is None or not self._is_admin(user.id):
            return
        if not self._chat_allowed(chat.id):
            return

        async with self._reviews_lock:
            target = next(
                (r for r in self._reviews.values()
                 if r.chat_id == chat.id
                 and r.awaiting_edit_admin_id == user.id
                 and r.awaiting_edit_field),
                None,
            )
        if target is None:
            return  # nada que hacer — texto normal del admin

        field_id = target.awaiting_edit_field
        try:
            self._apply_field_edit(target.tx, field_id, msg.text)
        except ValueError as e:
            await msg.reply_text(f"❌ Valor inválido: {e}\nReenvía el mensaje o pulsa Cancelar.")
            return

        target.awaiting_edit_field = None
        target.awaiting_edit_admin_id = None

        # Re-render
        text, kb = self._render_card(target)
        try:
            await ctx.bot.edit_message_text(
                chat_id=target.chat_id,
                message_id=target.review_message_id,
                text=text,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning("No pude refrescar tarjeta tras edit %s: %s", target.review_id, e)
        try:
            await msg.reply_text(
                f"✏️ <b>{html.escape(self._field_label(field_id))}</b> actualizado.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        logger.info("Review %s: campo '%s' editado por user=%s",
                    target.review_id, field_id, user.id)

    # ------------------------------- build / main -------------------------------

    def build(self) -> Application:
        app = Application.builder().token(self.settings.telegram_bot_token).build()
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("id", self.cmd_id))
        app.add_handler(CommandHandler("bypass", self.cmd_bypass))
        app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        app.add_handler(CallbackQueryHandler(self.handle_callback))
        # Texto plano (NO comando) en chats permitidos — sólo se aplica si el sender
        # es admin con una edición pendiente. Cualquier otro texto se ignora.
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))
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
