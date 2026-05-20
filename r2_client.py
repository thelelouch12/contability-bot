import logging
import re
import unicodedata
import uuid
from datetime import datetime

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from models import Transaccion

logger = logging.getLogger(__name__)

# Slug seguro: solo a-z 0-9, separadores con guion. Defensa contra path-injection
# si Gemini devuelve nombres con caracteres raros desde una imagen no confiable.
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MAX_SLUG_LEN = 32


def _slugify(s: str) -> str:
    # NFKD descompone caracteres acentuados (á → a + combining accent),
    # luego encode/decode ASCII descarta los diacríticos. Bogotá → Bogota.
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = _SLUG_RE.sub("-", s).strip("-")
    return s[:_MAX_SLUG_LEN] or "unknown"


def _detect_image_ext(image_bytes: bytes) -> str:
    """Devuelve extensión según magic bytes. Default .jpg."""
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "webp"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    return "jpg"


def _content_type_for(ext: str) -> str:
    return {
        "jpg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "gif": "image/gif",
    }.get(ext, "application/octet-stream")


class R2Client:
    """Sube fotos a Cloudflare R2 vía API S3-compatible.

    Estructura del bucket:
        receipts/YYYY-MM/YYYYMMDD-HHMM_<banco-slug>_<valor>_<uuid8>.<ext>

    El UUID8 al final hace la URL no-iterable (importante porque el bucket
    es público vía R2.dev). El resto del path es descriptivo para
    navegar desde el dashboard de R2.
    """

    def __init__(
        self,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        public_url: str = "",
    ):
        endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
        self._bucket = bucket
        self._public_url = public_url.rstrip("/")
        # virtual=False fuerza path-style (R2 lo prefiere). signature_version v4
        # es lo que R2 espera.
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        logger.info("R2Client iniciado: bucket=%s public=%s",
                    bucket, bool(self._public_url))

    def upload_receipt(
        self,
        image_bytes: bytes,
        when: datetime,
        tx: Transaccion,
        *,
        telegram_msg_id: int | None = None,
    ) -> str:
        """Sube el comprobante. Devuelve URL pública (o signed URL si no hay public_url).
        Lanza la excepción de boto en caso de error — el caller decide fallback.
        """
        ext = _detect_image_ext(image_bytes)
        month = when.strftime("%Y-%m")
        ts = when.strftime("%Y%m%d-%H%M")
        banco = _slugify(tx.banco)
        # valor entero sin separadores (más corto en path); ceros si valor=0
        try:
            valor_int = int(round(tx.valor))
        except (TypeError, ValueError):
            valor_int = 0
        unique = uuid.uuid4().hex[:8]
        key = f"receipts/{month}/{ts}_{banco}_{valor_int}_{unique}.{ext}"

        # S3 metadata SOLO acepta ASCII — normalizamos diacríticos (á → a) y
        # descartamos lo que no sobreviva. Sin esto, "Banco de Bogotá" rompe el upload.
        def _ascii(v: str) -> str:
            return unicodedata.normalize("NFKD", v or "").encode("ascii", "ignore").decode("ascii")[:200]

        metadata = {
            "banco": _ascii(tx.banco),
            "estado": _ascii(tx.estado.value),
            "valor": str(valor_int),
        }
        if telegram_msg_id is not None:
            metadata["telegram-msg-id"] = str(telegram_msg_id)

        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=image_bytes,
            ContentType=_content_type_for(ext),
            CacheControl="public, max-age=31536000, immutable",
            Metadata=metadata,
        )
        logger.info("R2 upload OK: %s (%d bytes)", key, len(image_bytes))

        if self._public_url:
            return f"{self._public_url}/{key}"
        # Sin public_url → signed URL válida 7 días
        return self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=7 * 24 * 3600,
        )

    @staticmethod
    def is_configured(settings) -> bool:
        return bool(
            settings.r2_account_id
            and settings.r2_access_key_id
            and settings.r2_secret_access_key
            and settings.r2_bucket_name
        )
