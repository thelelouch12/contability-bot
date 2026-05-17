import asyncio
import logging
import re
import time
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from models import Transaccion

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
MAX_BACKOFF_SEC = 65.0
_RETRY_DELAY_RE = re.compile(r"retry in (\d+(?:\.\d+)?)s", re.IGNORECASE)


def _parse_retry_delay(err: Exception) -> float | None:
    m = _RETRY_DELAY_RE.search(str(err))
    if m:
        return min(float(m.group(1)) + 1.0, MAX_BACKOFF_SEC)
    return None


class _RateLimiter:
    """Limita a max_calls por window_sec. Bloquea antes de llamar para evitar 429."""

    def __init__(self, max_calls: int, window_sec: float):
        self._max = max_calls
        self._window = window_sec
        self._times: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._times = [t for t in self._times if now - t < self._window]
            if len(self._times) >= self._max:
                wait = self._window - (now - self._times[0]) + 0.2
                logger.info("Rate limit: esperando %.1fs (cola=%d)", wait, len(self._times))
                await asyncio.sleep(wait)
                now = time.monotonic()
                self._times = [t for t in self._times if now - t < self._window]
            self._times.append(now)

EXTRACTION_PROMPT = """Eres un asistente contable. Analiza la imagen y extrae los datos en JSON estricto siguiendo el esquema.

PASO 1 — Determina `es_comprobante`:
- True si la imagen es un comprobante/recibo/notificación bancaria de una **transferencia o pago** entre cuentas. INCLUYE transferencias EXITOSAS, PENDIENTES y **FALLIDAS** — siempre que tenga datos de la transacción (código/referencia, destinatario, monto). Una transferencia fallida con código y destinatario SÍ es comprobante (con estado=Fallida).
- False SOLO en estos casos:
  • Pantalla de saldo / consulta de cuenta (muestra saldo, no una transferencia específica).
  • Alerta de seguridad, login, token, OTP, mensaje de "clave casi lista".
  • Mensaje de error genérico sin datos de transacción (sin código, sin destinatario, sin monto claro).
  • Captura random, meme, foto personal, screenshot de cualquier otra app no bancaria.
  • Recibo de servicio público (luz, agua) sin transferencia bancaria.

Regla clave: si hay código de transacción + destinatario + monto, es comprobante (es_comprobante=true) — SIEMPRE, sin importar el estado. Mapea el estado así:
  - "Exitosa", "Aprobada", "Confirmada", "Realizada", "Completada" → Exitosa
  - "Pendiente", "En proceso", "Procesando", "Validando", "En validación", "En trámite" → Pendiente
  - "Fallida", "Rechazada", "No exitosa", "Error", "No procesada" → Fallida
  - Si NO se puede determinar el estado claramente, usa Pendiente (NO descartes la imagen).

PASO 2 — Si `es_comprobante=false`, devuelve: banco='N/A', estado='Desconocida', codigo='N/A', destino_*='N/A'/'Desconocido', valor=0, moneda='COP', notas_ocr=<motivo breve>.

PASO 3 — Si `es_comprobante=true`, extrae:
- 'valor': número decimal sin separador de miles ni símbolo. Es el monto TRANSFERIDO (no el saldo).
- 'banco': banco/billetera emisora (Bancolombia, Nequi, Daviplata, BBVA, Davivienda, etc).
- 'codigo_transaccion': referencia/comprobante/número de aprobación. 'N/A' si no aparece.
- 'destino_*': info de la cuenta a la que se transfirió. Para Nequi/Daviplata usar ese enum en destino_tipo.
- 'fecha_comprobante': ISO 8601 (YYYY-MM-DDTHH:MM:SS) si se puede; sino el texto tal cual.
- Si un campo no es legible, usa 'N/A' o el enum 'Desconocido'/'Desconocida' que corresponda.
"""


class GeminiClient:
    def __init__(self, api_key: str, model: str, rpm: int = 14):
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._limiter = _RateLimiter(max_calls=rpm, window_sec=61.0)

    async def extract(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> Transaccion:
        last_err: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                await self._limiter.acquire()
                response = await self._client.aio.models.generate_content(
                    model=self._model,
                    contents=[
                        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                        EXTRACTION_PROMPT,
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=Transaccion,
                        temperature=0.0,
                    ),
                )
                parsed = response.parsed
                if not isinstance(parsed, Transaccion):
                    logger.warning("Gemini no devolvió Transaccion parseado; intentando JSON crudo")
                    parsed = Transaccion.model_validate_json(response.text)
                return parsed
            except (genai_errors.ServerError, genai_errors.ClientError) as e:
                status = getattr(e, "status_code", None) or getattr(e, "code", None)
                if status not in RETRYABLE_STATUSES or attempt == MAX_ATTEMPTS:
                    raise
                # Para 429 respetar el retryDelay que sugiere Google (ej. "retry in 57s")
                suggested = _parse_retry_delay(e)
                wait = suggested if suggested is not None else min(2 ** (attempt - 1), MAX_BACKOFF_SEC)
                logger.warning("Gemini %s (intento %d/%d). Reintentando en %.1fs%s",
                               status, attempt, MAX_ATTEMPTS, wait,
                               " (sugerido por Google)" if suggested else "")
                last_err = e
                await asyncio.sleep(wait)
        assert last_err is not None
        raise last_err
