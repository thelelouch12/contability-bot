from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class EstadoTransaccion(str, Enum):
    EXITOSA = "Exitosa"
    PENDIENTE = "Pendiente"
    FALLIDA = "Fallida"
    REVISION = "Revisión"
    DESCONOCIDA = "Desconocida"


class TipoCuenta(str, Enum):
    AHORROS = "Ahorros"
    CORRIENTE = "Corriente"
    NEQUI = "Nequi"
    DAVIPLATA = "Daviplata"
    BOLSILLO = "Bolsillo"
    OTRO = "Otro"
    DESCONOCIDO = "Desconocido"


class Transaccion(BaseModel):
    """Datos extraídos del comprobante de transferencia."""

    es_comprobante: bool = Field(description="True SOLO si la imagen es un comprobante de una transferencia/pago bancario efectivamente realizado. False si es saldo, consulta de cuenta, alerta de seguridad, token, transacción fallida, captura random, meme, etc.")
    banco: str = Field(description="Banco emisor o que aparece en el comprobante (ej. Bancolombia, Nequi, Daviplata, BBVA)")
    estado: EstadoTransaccion = Field(description="Estado de la transacción")
    codigo_transaccion: str = Field(description="Código/número/referencia de la transacción. Si no aparece, usar 'N/A'")
    destino_nombre: str = Field(description="Nombre del titular de la cuenta destino. Si no aparece, 'N/A'")
    destino_numero: str = Field(description="Número de cuenta destino (puede estar enmascarado con asteriscos). Si no aparece, 'N/A'")
    destino_tipo: TipoCuenta = Field(description="Tipo de cuenta destino")
    valor: float = Field(description="Valor numérico de la transacción, solo el número sin símbolos ni separadores de miles")
    moneda: str = Field(default="COP", description="Código ISO de la moneda (COP, USD, etc). Por defecto COP si no se detecta")
    fecha_comprobante: Optional[str] = Field(default=None, description="Fecha/hora que aparece dentro del comprobante en formato ISO 8601 si es posible, sino el texto tal cual aparece")
    notas_ocr: Optional[str] = Field(default=None, description="Cualquier observación relevante (ej. 'comprobante borroso', 'monto parcialmente legible')")
