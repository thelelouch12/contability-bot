import logging
import threading
import time
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from models import Transaccion

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

TODAS_SHEET = "Todas"
RESUMEN_SHEET = "Resumen"
REPORTE_SHEET = "Reporte"

# Caracteres que Sheets interpreta como inicio de fórmula. Si una celda string
# proveniente del OCR (controlado por el atacante vía contenido de la imagen)
# empieza con uno de estos, le prefijamos comilla simple para forzar texto literal.
# Defensa estándar OWASP contra CSV/Formula Injection.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value):
    """Neutraliza formula injection en celdas string. Pasa-through para tipos no-string."""
    if not isinstance(value, str) or not value:
        return value
    return "'" + value if value.startswith(_FORMULA_TRIGGERS) else value

HEADERS = [
    "ID Mensaje", "Fecha/Hora", "Remitente", "Banco", "Estado", "Código",
    "Destino - Nombre", "Destino - Número", "Destino - Tipo", "Valor",
    "Moneda", "Fecha Comprobante", "Imagen", "Notas OCR", "Verificado",
    "Notas Manuales", "Comisionable", "Aprobado por",
]
# Índices clave (1-based para A1 notation): O=15 Verificado, P=16 Notas Manuales, Q=17 Comisionable, R=18 Aprobado por
COL_VERIFICADO_IDX = 14  # 0-based, col O
COL_COMISIONABLE_IDX = 16  # 0-based, col Q
COL_APROBADO_IDX = 17  # 0-based, col R

# Colores
NAVY = {"red": 0.20, "green": 0.32, "blue": 0.45}
LIGHT_BLUE = {"red": 0.86, "green": 0.89, "blue": 0.94}
WHITE = {"red": 1, "green": 1, "blue": 1}

HEADER_FMT = {
    "backgroundColor": NAVY,
    "textFormat": {"foregroundColor": WHITE, "bold": True, "fontSize": 11},
    "horizontalAlignment": "CENTER",
    "verticalAlignment": "MIDDLE",
}
CURRENCY_FMT = {"numberFormat": {"type": "CURRENCY", "pattern": '"$ "#,##0'}}
DATETIME_FMT = {"numberFormat": {"type": "DATE_TIME", "pattern": "yyyy-mm-dd hh:mm"}}
TITLE_FMT = {
    "backgroundColor": NAVY,
    "textFormat": {"foregroundColor": WHITE, "bold": True, "fontSize": 14},
    "horizontalAlignment": "CENTER",
}
SUBHEADER_FMT = {
    "backgroundColor": LIGHT_BLUE,
    "textFormat": {"bold": True, "fontSize": 11},
    "horizontalAlignment": "CENTER",
}
COL_WIDTHS = [70, 130, 200, 140, 95, 95, 160, 170, 105, 115, 75, 130, 95, 180, 95, 200, 115, 160]


class SheetsClient:
    def __init__(self, service_account_file: str, sheet_id: str):
        creds = Credentials.from_service_account_file(service_account_file, scopes=SCOPES)
        self._gc = gspread.authorize(creds)
        self._sh = self._gc.open_by_key(sheet_id)
        self._monthly_lock = threading.Lock()
        # Serializa writes a Sheets para evitar race condition de "first empty row" en
        # append_row bajo concurrencia (asyncio.gather con N workers vía to_thread).
        # Con throttle de 1s mantenemos <60 tx/min ≈ 300 ops/min, dentro del cupo Sheets API.
        self._write_lock = threading.Lock()
        self._last_write_ts = 0.0
        # 1.5s da margen vs el cupo de 300 writes/min de Sheets API cuando hay
        # batch_updates de duplicados pesados encima del append base.
        self._min_write_interval = 1.5
        locale = self._sh.fetch_sheet_metadata().get("properties", {}).get("locale", "en_US")
        non_us = locale.split("_")[0] in {"es", "fr", "de", "it", "pt", "nl"}
        self._sep = ";" if non_us else ","
        self._arr_col_sep = "\\" if non_us else ","
        logger.info("Sheet locale=%s sep='%s' arr_col_sep='%s'", locale, self._sep, self._arr_col_sep)
        self._ensure_base_sheets()

    # ------------------------------- inicialización -------------------------------

    def _ensure_base_sheets(self) -> None:
        existing = {ws.title: ws for ws in self._sh.worksheets()}

        if TODAS_SHEET not in existing:
            ws = self._sh.add_worksheet(title=TODAS_SHEET, rows=1000, cols=len(HEADERS))
            ws.append_row(HEADERS, value_input_option="USER_ENTERED")
            existing[TODAS_SHEET] = ws
            self._format_transactions_sheet(existing[TODAS_SHEET])
        # No re-formatear si ya existe → ahorra cuota de Sheets API en restarts

        # Migración: añadir columna Comisionable a Todas y monthlies si no existe.
        # Se ejecuta solo una vez (es no-op cuando ya está presente).
        self._migrate_add_comisionable_column(existing)

        # Migración: añadir regla condicional para "Revisión" → púrpura en hojas
        # transactions que ya existían antes de esta feature.
        self._migrate_add_revision_color(existing)

        # Migración: añadir columna "Aprobado por" (R) a Todas y monthlies si no existe.
        self._migrate_add_aprobado_column(existing)

        # Resumen: recrear si quedó en versión vieja (marker en A3).
        if RESUMEN_SHEET in existing:
            resumen_ws = existing[RESUMEN_SHEET]
            try:
                marker = resumen_ws.acell("A3").value
            except Exception:
                marker = None
            if marker != "INDICADORES POR ESTADO":
                logger.info("Resumen desactualizado, recreando con KPIs y Top destinatarios")
                self._sh.del_worksheet(resumen_ws)
                self._create_resumen_sheet()
        else:
            self._create_resumen_sheet()

        # Reporte: recrear si quedó desactualizado (le falta la sección de Comisionables).
        if REPORTE_SHEET in existing:
            reporte_ws = existing[REPORTE_SHEET]
            try:
                marker = reporte_ws.acell("A23").value
            except Exception:
                marker = None
            if marker != "COMISIÓN SOBRE COMISIONABLES":
                logger.info("Reporte desactualizado, recreando con seccion de Comisionables")
                self._sh.del_worksheet(reporte_ws)
                self._create_reporte_sheet()
        else:
            self._create_reporte_sheet()

        # No re-formatear hojas mensuales existentes en cada arranque (ahorra cuota API)

        # Limpiar hoja por defecto si quedó vacía
        for default_name in ("Sheet1", "Hoja 1", "Hoja1"):
            ws = existing.get(default_name)
            if ws is None:
                continue
            values = ws.get_all_values()
            if not values or all(not any(row) for row in values):
                try:
                    self._sh.del_worksheet(ws)
                except Exception:
                    pass

    def _migrate_add_aprobado_column(self, existing: dict) -> None:
        """Agrega la columna 'Aprobado por' (R) a transactions sheets.
        Idempotente: si el header de R ya dice 'Aprobado por', solo limpia
        dataValidation BOOLEAN si quedó en filas (caso del bug de las filas
        que aparecían como checkbox antes del fix)."""
        import re as _re
        _monthly_re = _re.compile(r"^\d{4}-\d{2}$")
        target_col_letter = "R"
        for title, ws in existing.items():
            if title != TODAS_SHEET and not _monthly_re.match(title):
                continue
            try:
                current = ws.acell(f"{target_col_letter}1").value
            except Exception:
                current = None
            gid = ws.id
            needs_header = current != "Aprobado por"
            requests = []
            if needs_header:
                logger.info("Migración: agregando header 'Aprobado por' en '%s'", title)
                if ws.col_count < len(HEADERS):
                    ws.add_cols(len(HEADERS) - ws.col_count)
                ws.update(values=[["Aprobado por"]], range_name=f"{target_col_letter}1",
                          value_input_option="USER_ENTERED")
                requests += [
                    self._repeat_cell(gid, 0, 1, COL_APROBADO_IDX, COL_APROBADO_IDX + 1,
                                      HEADER_FMT, "userEnteredFormat"),
                    {"updateDimensionProperties": {
                        "range": {"sheetId": gid, "dimension": "COLUMNS",
                                  "startIndex": COL_APROBADO_IDX, "endIndex": COL_APROBADO_IDX + 1},
                        "properties": {"pixelSize": 160},
                        "fields": "pixelSize",
                    }},
                ]

            # SIEMPRE limpiar dataValidation y boolValues False en col R rows 2+:
            # arregla filas que quedaron como checkbox porque Sheets extiende dv al
            # appendear (heredan del rango definido en otra migración o auto-fill).
            row_max = max(ws.row_count, 1000)
            requests += [
                {"setDataValidation": {
                    "range": {"sheetId": gid, "startRowIndex": 1, "endRowIndex": row_max,
                              "startColumnIndex": COL_APROBADO_IDX, "endColumnIndex": COL_APROBADO_IDX + 1},
                    # sin "rule" → elimina la data validation existente
                }},
                # Limpiar boolValues residuales en col R: filas con TRUE/FALSE de checkbox
                # heredado se reemplazan por celda vacía (string blank).
                {"repeatCell": {
                    "range": {"sheetId": gid, "startRowIndex": 1, "endRowIndex": row_max,
                              "startColumnIndex": COL_APROBADO_IDX, "endColumnIndex": COL_APROBADO_IDX + 1},
                    "cell": {"userEnteredValue": {"stringValue": ""}},
                    "fields": "userEnteredValue",
                }},
            ]
            if requests:
                self._sh.batch_update({"requests": requests})

    def _migrate_add_revision_color(self, existing: dict) -> None:
        """Idempotente: agrega regla 'Revisión' → púrpura a cada hoja transactions
        que no la tenga. Lee conditionalFormats del metadata del spreadsheet para
        no duplicar la regla en restarts."""
        try:
            meta = self._sh.fetch_sheet_metadata()
        except Exception as e:
            logger.warning("No pude leer metadata para migrar color de Revisión: %s", e)
            return

        sheets_meta = {s["properties"]["sheetId"]: s for s in meta.get("sheets", [])}
        requests = []
        purple_color = {"red": 0.86, "green": 0.78, "blue": 0.96}

        import re as _re
        _monthly_re = _re.compile(r"^\d{4}-\d{2}$")
        for title, ws in existing.items():
            # Detectar hojas transactions: TODAS o YYYY-MM (no Resumen/Reporte/etc).
            if title != TODAS_SHEET and not _monthly_re.match(title):
                continue

            sm = sheets_meta.get(ws.id, {})
            rules = sm.get("conditionalFormats", [])
            # Detectar si ya hay regla con TEXT_EQ value "Revisión"
            has_revision = False
            for rule in rules:
                cond = (rule.get("booleanRule") or {}).get("condition") or {}
                if cond.get("type") != "TEXT_EQ":
                    continue
                values = cond.get("values") or []
                if any(v.get("userEnteredValue") == "Revisión" for v in values):
                    has_revision = True
                    break
            if has_revision:
                continue

            logger.info("Migración: agregando regla de color púrpura 'Revisión' en '%s'", title)
            requests.append(self._state_color_rule(ws.id, "Revisión", purple_color))

        if requests:
            self._sh.batch_update({"requests": requests})

    def _migrate_add_comisionable_column(self, existing: dict) -> None:
        """Agrega la columna Comisionable (Q) a Todas y monthly tabs si no existe.
        Idempotente: si el header de Q ya dice 'Comisionable', no hace nada.
        Aplica checkbox validation a las filas con datos (no en filas vacías para no
        romper la deteccion de tabla de append_row).
        """
        target_col_letter = "Q"  # col 17 = índice 16 (después del cambio HEADERS)
        for title, ws in existing.items():
            if title in (RESUMEN_SHEET, REPORTE_SHEET):
                continue
            # Es transactions sheet (Todas o YYYY-MM) si su header A1 = "ID Mensaje"
            try:
                first_header = ws.acell("A1").value
            except Exception:
                continue
            if first_header != "ID Mensaje":
                continue
            try:
                current = ws.acell(f"{target_col_letter}1").value
            except Exception:
                current = None
            if current == "Comisionable":
                continue
            logger.info("Migrando '%s': agregando columna Comisionable en %s", title, target_col_letter)
            # Contar filas con datos (col A no vacía)
            col_a = ws.col_values(1)
            last_data_row = len(col_a)  # incluye header en fila 1
            # 0) Asegurar que el sheet tenga al menos len(HEADERS)=17 columnas
            current_cols = ws.col_count
            if current_cols < len(HEADERS):
                ws.add_cols(len(HEADERS) - current_cols)
            requests = []
            # 1) escribir header (USER_ENTERED)
            ws.update(values=[["Comisionable"]], range_name=f"{target_col_letter}1",
                      value_input_option="USER_ENTERED")
            # 2) formato de header + validation + ancho col, en un solo batch_update
            gid = ws.id
            requests.append(self._repeat_cell(gid, 0, 1, COL_COMISIONABLE_IDX, COL_COMISIONABLE_IDX + 1,
                                              HEADER_FMT, "userEnteredFormat"))
            requests.append({"updateDimensionProperties": {
                "range": {"sheetId": gid, "dimension": "COLUMNS",
                          "startIndex": COL_COMISIONABLE_IDX, "endIndex": COL_COMISIONABLE_IDX + 1},
                "properties": {"pixelSize": 115},
                "fields": "pixelSize",
            }})
            if last_data_row >= 2:
                requests.append({"setDataValidation": {
                    "range": {"sheetId": gid, "startRowIndex": 1, "endRowIndex": last_data_row,
                              "startColumnIndex": COL_COMISIONABLE_IDX, "endColumnIndex": COL_COMISIONABLE_IDX + 1},
                    "rule": {"condition": {"type": "BOOLEAN"}, "strict": True, "showCustomUi": True},
                }})
            self._sh.batch_update({"requests": requests})

    def _format_transactions_sheet(self, ws: gspread.Worksheet) -> None:
        """Aplica todo el formato de una hoja de transacciones en 1 batch_update."""
        gid = ws.id
        # Reglas condicionales: colorear celda Estado (col E = idx 4) según valor
        state_rules = [
            self._state_color_rule(gid, "Exitosa", {"red": 0.72, "green": 0.88, "blue": 0.72}),    # verde
            self._state_color_rule(gid, "Pendiente", {"red": 1.0, "green": 0.92, "blue": 0.6}),    # amarillo
            self._state_color_rule(gid, "Fallida", {"red": 0.96, "green": 0.78, "blue": 0.78}),    # rojo
            self._state_color_rule(gid, "Revisión", {"red": 0.86, "green": 0.78, "blue": 0.96}),   # púrpura/lavanda
        ]
        requests = [
            *state_rules,
            # Freeze header
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": gid, "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }
            },
            # Header format (fila 1 cols A:P)
            self._repeat_cell(gid, 0, 1, 0, len(HEADERS), HEADER_FMT, "userEnteredFormat"),
            # Valor (col J = idx 9) → moneda
            self._repeat_cell(gid, 0, None, 9, 10, CURRENCY_FMT, "userEnteredFormat.numberFormat"),
            # Fecha/Hora (col B = idx 1)
            self._repeat_cell(gid, 0, None, 1, 2, DATETIME_FMT, "userEnteredFormat.numberFormat"),
            # Fecha Comprobante (col L = idx 11)
            self._repeat_cell(gid, 0, None, 11, 12, DATETIME_FMT, "userEnteredFormat.numberFormat"),
            # Nota: el checkbox de Verificado (col O) se aplica fila-a-fila en append_transaction,
            # NO bulk — porque rellenarlo en celdas vacías hace que append_row crea que la tabla
            # llega hasta la última fila con checkbox y appende fuera de rango.
            # Anchos de columna
            *[
                {
                    "updateDimensionProperties": {
                        "range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                        "properties": {"pixelSize": w},
                        "fields": "pixelSize",
                    }
                }
                for i, w in enumerate(COL_WIDTHS)
            ],
        ]
        self._sh.batch_update({"requests": requests})

    @staticmethod
    def _state_color_rule(sheet_gid: int, value: str, color: dict) -> dict:
        """Regla condicional: pinta celda E si su texto == value."""
        return {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sheet_gid,
                        "startRowIndex": 1, "endRowIndex": 1000,
                        "startColumnIndex": 4, "endColumnIndex": 5,
                    }],
                    "booleanRule": {
                        "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": value}]},
                        "format": {"backgroundColor": color, "textFormat": {"bold": True}},
                    },
                },
                "index": 0,
            }
        }

    @staticmethod
    def _repeat_cell(gid, r0, r1, c0, c1, format_obj, fields):
        rng = {"sheetId": gid, "startColumnIndex": c0, "endColumnIndex": c1, "startRowIndex": r0}
        if r1 is not None:
            rng["endRowIndex"] = r1
        return {
            "repeatCell": {
                "range": rng,
                "cell": {"userEnteredFormat": format_obj},
                "fields": fields,
            }
        }

    # ------------------------------- Resumen -------------------------------

    def _create_resumen_sheet(self) -> None:
        """Resumen con fórmulas (todo referencia 'Todas'):
        - Filas 3-6: KPIs por estado (Exitosa/Pendiente/Fallida/Comisionables)
        - Filas 8-10: Totales globales (Total General, Tx, Promedio, % Éxito)
        - Filas 12-49: Desglose Por Banco | Por Remitente | Por Estado (con TOTAL)
        - Filas 51-68: Por Mes (con TOTAL)
        - Filas 70-83: Top 10 Destinatarios
        """
        ws = self._sh.add_worksheet(title=RESUMEN_SHEET, rows=90, cols=12)
        gid = ws.id
        sep = self._sep
        T = TODAS_SHEET

        # Helpers de fórmula por estado
        def sum_estado(estado):
            return f'=SUMIFS({T}!J:J{sep}{T}!E:E{sep}"{estado}")'
        def count_estado(estado):
            return f'=COUNTIFS({T}!E:E{sep}"{estado}")'

        # QUERYs base — cada una termina con un row TOTAL.
        # Por Banco: D=Banco, J=Valor
        q_banco = (f'=IFERROR(QUERY({T}!D2:J{sep}'
                   f'"select D, count(D), sum(J) where D is not null and D <> \'\' '
                   f'group by D order by sum(J) desc '
                   f'label count(D) \'\', sum(J) \'\'"{sep}0){sep}"")')
        # Por Remitente: C=Remitente
        q_remit = (f'=IFERROR(QUERY({T}!C2:J{sep}'
                   f'"select C, count(C), sum(J) where C is not null and C <> \'\' '
                   f'group by C order by sum(J) desc '
                   f'label count(C) \'\', sum(J) \'\'"{sep}0){sep}"")')
        # Por Estado: E
        q_estado = (f'=IFERROR(QUERY({T}!E2:J{sep}'
                    f'"select E, count(E), sum(J) where E is not null and E <> \'\' '
                    f'group by E order by sum(J) desc '
                    f'label count(E) \'\', sum(J) \'\'"{sep}0){sep}"")')
        # Por Mes: TEXT(B,yyyy-mm), J
        q_mes = (f'=IFERROR(QUERY({{ARRAYFORMULA(IF({T}!B2:B=""{sep}""{sep}'
                 f'TEXT({T}!B2:B{sep}"yyyy-mm"))){self._arr_col_sep}{T}!J2:J}}{sep}'
                 f'"select Col1, count(Col1), sum(Col2) where Col1 is not null and Col1 <> \'\' '
                 f'group by Col1 order by Col1 desc '
                 f'label count(Col1) \'\', sum(Col2) \'\'"{sep}0){sep}"")')
        # Top 10 Destinatarios: G=destino_nombre, H=destino_numero, J=valor
        q_top = (f'=IFERROR(QUERY({T}!G2:J{sep}'
                 f'"select G, H, count(G), sum(J) where G is not null and G <> \'\' '
                 f'group by G, H order by sum(J) desc limit 10 '
                 f'label count(G) \'\', sum(J) \'\'"{sep}0){sep}"")')

        ws.batch_update(
            [
                # Título
                {"range": "A1", "values": [["RESUMEN CONTABLE"]]},

                # KPIs POR ESTADO (fila 3 marker, filas 4-6 cards)
                {"range": "A3", "values": [["INDICADORES POR ESTADO"]]},
                {"range": "A4:K4", "values": [[
                    "✓ Exitosas", "", "",
                    "⏳ Pendientes", "", "",
                    "✗ Fallidas", "", "",
                    "★ Comisionables", "",
                ]]},
                {"range": "A5:K5", "values": [[
                    sum_estado("Exitosa"), "", "",
                    sum_estado("Pendiente"), "", "",
                    sum_estado("Fallida"), "", "",
                    f'=SUMIFS({T}!J:J{sep}{T}!Q:Q{sep}TRUE)', "",
                ]]},
                {"range": "A6:K6", "values": [[
                    count_estado("Exitosa"), "", "",
                    count_estado("Pendiente"), "", "",
                    count_estado("Fallida"), "", "",
                    f'=COUNTIFS({T}!Q:Q{sep}TRUE)', "",
                ]]},

                # TOTALES GLOBALES (filas 8-10)
                {"range": "A8", "values": [["TOTALES GLOBALES"]]},
                {"range": "A9:K9", "values": [[
                    "Total General", "", "",
                    "Transacciones", "", "",
                    "Promedio", "", "",
                    "% Éxito", "",
                ]]},
                {"range": "A10:K10", "values": [[
                    f'=SUM({T}!J2:J)', "", "",
                    f'=COUNTA({T}!A2:A)', "", "",
                    f'=IFERROR(SUM({T}!J2:J)/COUNTA({T}!A2:A){sep}0)', "", "",
                    f'=IFERROR(COUNTIFS({T}!E:E{sep}"Exitosa")/COUNTA({T}!A2:A){sep}0)', "",
                ]]},

                # DESGLOSE (filas 12+)
                {"range": "A12", "values": [["Por Banco"]]},
                {"range": "A13:C13", "values": [["Banco", "Cantidad", "Total"]]},
                {"range": "A14", "values": [[q_banco]]},

                {"range": "E12", "values": [["Por Remitente"]]},
                {"range": "E13:G13", "values": [["Remitente", "Cantidad", "Total"]]},
                {"range": "E14", "values": [[q_remit]]},

                {"range": "I12", "values": [["Por Estado"]]},
                {"range": "I13:K13", "values": [["Estado", "Cantidad", "Total"]]},
                {"range": "I14", "values": [[q_estado]]},

                # POR MES (fila 51)
                {"range": "A51", "values": [["Por Mes"]]},
                {"range": "A52:C52", "values": [["Mes", "Cantidad", "Total"]]},
                {"range": "A53", "values": [[q_mes]]},

                # TOP 10 DESTINATARIOS (fila 70)
                {"range": "A70", "values": [["Top 10 Destinatarios"]]},
                {"range": "A71:D71", "values": [["Destinatario", "Número", "Cantidad", "Total"]]},
                {"range": "A72", "values": [[q_top]]},
            ],
            value_input_option="USER_ENTERED",
        )

        # ─────────── Formato ───────────
        currency_bold_large = {**CURRENCY_FMT, "textFormat": {"bold": True, "fontSize": 12, "foregroundColor": NAVY}}
        count_bold = {"textFormat": {"bold": True, "fontSize": 11}, "horizontalAlignment": "CENTER"}
        pct_fmt = {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"},
                   "textFormat": {"bold": True, "fontSize": 12, "foregroundColor": NAVY},
                   "horizontalAlignment": "CENTER"}
        kpi_label = {"textFormat": {"bold": True, "fontSize": 10}, "horizontalAlignment": "CENTER",
                     "backgroundColor": LIGHT_BLUE}
        SECTION_HDR = {
            "backgroundColor": NAVY,
            "textFormat": {"foregroundColor": WHITE, "bold": True, "fontSize": 12},
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
        }

        def merge(r0, r1, c0, c1):
            return {"mergeCells": {
                "range": {"sheetId": gid, "startRowIndex": r0, "endRowIndex": r1,
                          "startColumnIndex": c0, "endColumnIndex": c1},
                "mergeType": "MERGE_ALL",
            }}

        requests = [
            # Freeze top 2 rows (título)
            {"updateSheetProperties": {
                "properties": {"sheetId": gid, "gridProperties": {"frozenRowCount": 2}},
                "fields": "gridProperties.frozenRowCount",
            }},

            # ── Título ──
            merge(0, 1, 0, 11),
            self._repeat_cell(gid, 0, 1, 0, 11, TITLE_FMT, "userEnteredFormat"),

            # ── KPIs POR ESTADO (filas 3-6, idx 2-5) ──
            merge(2, 3, 0, 11),  # subheader "INDICADORES POR ESTADO"
            self._repeat_cell(gid, 2, 3, 0, 11, SECTION_HDR, "userEnteredFormat"),
            # 4 cards: cada card es 3 cols, la última es 2.
            # Card 1: A4:B4 label, A5:B5 monto, A6:B6 cantidad
            *[merge(r, r+1, c0, c1) for r in (3, 4, 5) for (c0, c1) in [(0,2),(3,5),(6,8),(9,11)]],
            # Labels (fila 4) — 4 estilos por color
            self._repeat_cell(gid, 3, 4, 0, 2, {**kpi_label, "backgroundColor": {"red":0.85,"green":0.95,"blue":0.85}}, "userEnteredFormat"),
            self._repeat_cell(gid, 3, 4, 3, 5, {**kpi_label, "backgroundColor": {"red":1.0,"green":0.95,"blue":0.80}}, "userEnteredFormat"),
            self._repeat_cell(gid, 3, 4, 6, 8, {**kpi_label, "backgroundColor": {"red":0.99,"green":0.85,"blue":0.85}}, "userEnteredFormat"),
            self._repeat_cell(gid, 3, 4, 9, 11, {**kpi_label, "backgroundColor": {"red":0.90,"green":0.87,"blue":0.96}}, "userEnteredFormat"),
            # Montos (fila 5) — currency grande
            self._repeat_cell(gid, 4, 5, 0, 11, currency_bold_large, "userEnteredFormat"),
            # Cantidades (fila 6) — número centrado
            self._repeat_cell(gid, 5, 6, 0, 11, count_bold, "userEnteredFormat"),

            # ── TOTALES GLOBALES (filas 8-10, idx 7-9) ──
            merge(7, 8, 0, 11),
            self._repeat_cell(gid, 7, 8, 0, 11, SECTION_HDR, "userEnteredFormat"),
            *[merge(r, r+1, c0, c1) for r in (8, 9) for (c0, c1) in [(0,2),(3,5),(6,8),(9,11)]],
            self._repeat_cell(gid, 8, 9, 0, 11, kpi_label, "userEnteredFormat"),
            # Total General y Promedio: currency
            self._repeat_cell(gid, 9, 10, 0, 2, currency_bold_large, "userEnteredFormat"),
            self._repeat_cell(gid, 9, 10, 6, 8, currency_bold_large, "userEnteredFormat"),
            # Transacciones: número grande
            self._repeat_cell(gid, 9, 10, 3, 5, {"textFormat": {"bold": True, "fontSize": 14, "foregroundColor": NAVY}, "horizontalAlignment": "CENTER"}, "userEnteredFormat"),
            # % Éxito
            self._repeat_cell(gid, 9, 10, 9, 11, pct_fmt, "userEnteredFormat"),

            # ── DESGLOSE (fila 12 = idx 11) ──
            merge(11, 12, 0, 3),
            self._repeat_cell(gid, 11, 12, 0, 3, SUBHEADER_FMT, "userEnteredFormat"),
            merge(11, 12, 4, 7),
            self._repeat_cell(gid, 11, 12, 4, 7, SUBHEADER_FMT, "userEnteredFormat"),
            merge(11, 12, 8, 11),
            self._repeat_cell(gid, 11, 12, 8, 11, SUBHEADER_FMT, "userEnteredFormat"),
            # Headers fila 13 (idx 12)
            self._repeat_cell(gid, 12, 13, 0, 3, {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER", "backgroundColor": LIGHT_BLUE}, "userEnteredFormat"),
            self._repeat_cell(gid, 12, 13, 4, 7, {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER", "backgroundColor": LIGHT_BLUE}, "userEnteredFormat"),
            self._repeat_cell(gid, 12, 13, 8, 11, {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER", "backgroundColor": LIGHT_BLUE}, "userEnteredFormat"),
            # Currency en col C (Total Banco), G (Total Remitente), K (Total Estado) desde fila 14
            self._repeat_cell(gid, 13, 50, 2, 3, CURRENCY_FMT, "userEnteredFormat.numberFormat"),
            self._repeat_cell(gid, 13, 50, 6, 7, CURRENCY_FMT, "userEnteredFormat.numberFormat"),
            self._repeat_cell(gid, 13, 50, 10, 11, CURRENCY_FMT, "userEnteredFormat.numberFormat"),

            # ── POR MES (fila 51 = idx 50) ──
            merge(50, 51, 0, 3),
            self._repeat_cell(gid, 50, 51, 0, 3, SUBHEADER_FMT, "userEnteredFormat"),
            self._repeat_cell(gid, 51, 52, 0, 3, {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER", "backgroundColor": LIGHT_BLUE}, "userEnteredFormat"),
            self._repeat_cell(gid, 52, 69, 2, 3, CURRENCY_FMT, "userEnteredFormat.numberFormat"),

            # ── TOP 10 DESTINATARIOS (fila 70 = idx 69) ──
            merge(69, 70, 0, 4),
            self._repeat_cell(gid, 69, 70, 0, 4, SUBHEADER_FMT, "userEnteredFormat"),
            self._repeat_cell(gid, 70, 71, 0, 4, {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER", "backgroundColor": LIGHT_BLUE}, "userEnteredFormat"),
            self._repeat_cell(gid, 71, 82, 3, 4, CURRENCY_FMT, "userEnteredFormat.numberFormat"),

            # Anchos
            *[
                {"updateDimensionProperties": {
                    "range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                    "properties": {"pixelSize": w},
                    "fields": "pixelSize",
                }}
                for i, w in enumerate([160, 80, 130, 20, 200, 80, 130, 20, 130, 80, 130, 20])
            ],
            # Altura mínima de filas KPI para que se vean cómodas
            {"updateDimensionProperties": {
                "range": {"sheetId": gid, "dimension": "ROWS", "startIndex": 3, "endIndex": 6},
                "properties": {"pixelSize": 32}, "fields": "pixelSize",
            }},
            {"updateDimensionProperties": {
                "range": {"sheetId": gid, "dimension": "ROWS", "startIndex": 8, "endIndex": 10},
                "properties": {"pixelSize": 32}, "fields": "pixelSize",
            }},
        ]
        self._sh.batch_update({"requests": requests})
        logger.info("Hoja '%s' recreada con formato (v2: KPIs + Top 10)", RESUMEN_SHEET)

    # ------------------------------- hojas mensuales -------------------------------

    def _create_reporte_sheet(self) -> None:
        ws = self._sh.add_worksheet(title=REPORTE_SHEET, rows=50, cols=8)
        gid = ws.id
        T = TODAS_SHEET

        def count_f(estado):
            return (f'=IFERROR(IF(OR($B$5="";$B$5="(Todos)");'
                    f'COUNTIFS(INDIRECT("{T}!E"&$B$3&":E"&$B$4);"{estado}");'
                    f'COUNTIFS(INDIRECT("{T}!E"&$B$3&":E"&$B$4);"{estado}";INDIRECT("{T}!C"&$B$3&":C"&$B$4);$B$5));0)')

        def sum_f(estado):
            return (f'=IFERROR(IF(OR($B$5="";$B$5="(Todos)");'
                    f'SUMIFS(INDIRECT("{T}!J"&$B$3&":J"&$B$4);INDIRECT("{T}!E"&$B$3&":E"&$B$4);"{estado}");'
                    f'SUMIFS(INDIRECT("{T}!J"&$B$3&":J"&$B$4);INDIRECT("{T}!E"&$B$3&":E"&$B$4);"{estado}";INDIRECT("{T}!C"&$B$3&":C"&$B$4);$B$5));0)')

        # Comisionable: col Q (Q=Comisionable=TRUE) y col E="Exitosa". Filtro opcional por remitente.
        count_comis = (f'=IFERROR(IF(OR($B$5="";$B$5="(Todos)");'
                       f'COUNTIFS(INDIRECT("{T}!Q"&$B$3&":Q"&$B$4);TRUE;INDIRECT("{T}!E"&$B$3&":E"&$B$4);"Exitosa");'
                       f'COUNTIFS(INDIRECT("{T}!Q"&$B$3&":Q"&$B$4);TRUE;INDIRECT("{T}!E"&$B$3&":E"&$B$4);"Exitosa";INDIRECT("{T}!C"&$B$3&":C"&$B$4);$B$5));0)')
        sum_comis = (f'=IFERROR(IF(OR($B$5="";$B$5="(Todos)");'
                     f'SUMIFS(INDIRECT("{T}!J"&$B$3&":J"&$B$4);INDIRECT("{T}!Q"&$B$3&":Q"&$B$4);TRUE;INDIRECT("{T}!E"&$B$3&":E"&$B$4);"Exitosa");'
                     f'SUMIFS(INDIRECT("{T}!J"&$B$3&":J"&$B$4);INDIRECT("{T}!Q"&$B$3&":Q"&$B$4);TRUE;INDIRECT("{T}!E"&$B$3&":E"&$B$4);"Exitosa";INDIRECT("{T}!C"&$B$3&":C"&$B$4);$B$5));0)')

        ws.batch_update([
            {"range": "A1", "values": [["REPORTE PERSONALIZADO"]]},
            {"range": "A3:B3", "values": [["Desde fila #:", 2]]},
            {"range": "A4:B4", "values": [["Hasta fila #:", f"=COUNTA({T}!A:A)"]]},
            {"range": "A5:B5", "values": [["Remitente:", "(Todos)"]]},
            {"range": "A7", "values": [["ESTADOS EN EL RANGO SELECCIONADO"]]},
            {"range": "A8:C8", "values": [["Estado", "Cantidad", "Total"]]},
            {"range": "A9:C9", "values": [["Exitosa", count_f("Exitosa"), sum_f("Exitosa")]]},
            {"range": "A10:C10", "values": [["Pendiente", count_f("Pendiente"), sum_f("Pendiente")]]},
            {"range": "A11:C11", "values": [["Fallida", count_f("Fallida"), sum_f("Fallida")]]},
            {"range": "A12:C12", "values": [["TOTAL", "=SUM(B9:B11)", "=SUM(C9:C11)"]]},
            {"range": "A14", "values": [["CÁLCULO INGRESO NETO (sobre Exitosas)"]]},
            {"range": "A15:B15", "values": [["Total Exitosas:", "=C9"]]},
            {"range": "A16:B16", "values": [["Atrapado (a descontar):", 0]]},
            {"range": "A17:B17", "values": [["INGRESO NETO:", "=B15-B16"]]},
            {"range": "A19", "values": [["COMISIÓN A COBRAR"]]},
            {"range": "A20:B20", "values": [["Comisión %:", 0]]},
            {"range": "A21:B21", "values": [["Comisión a cobrar:", "=B15*B20/100"]]},
            # Nueva sección: comisión SOLO sobre las marcadas como Comisionable (col Q)
            {"range": "A23", "values": [["COMISIÓN SOBRE COMISIONABLES"]]},
            {"range": "A24:B24", "values": [["Cantidad Comisionables:", count_comis]]},
            {"range": "A25:B25", "values": [["Total Comisionables:", sum_comis]]},
            {"range": "A26:B26", "values": [["Comisión %:", 0]]},
            {"range": "A27:B27", "values": [["Comisión a cobrar:", "=B25*B26/100"]]},
            # Helper oculto para el dropdown de remitentes
            {"range": "F1", "values": [["(Todos)"]]},
            {"range": "F2", "values": [[f'=IFERROR(UNIQUE(FILTER({T}!C2:C;{T}!C2:C<>""));"")']]},
        ], value_input_option="USER_ENTERED")

        NAVY = {"red": 0.20, "green": 0.32, "blue": 0.45}
        LIGHT_BLUE = {"red": 0.86, "green": 0.89, "blue": 0.94}
        GREEN_BG = {"red": 0.84, "green": 0.93, "blue": 0.84}
        INPUT_BG = {"red": 1.0, "green": 0.97, "blue": 0.86}
        WHITE = {"red": 1, "green": 1, "blue": 1}
        CURRENCY = {"type": "CURRENCY", "pattern": '"$ "#,##0'}

        def repeat(r0, r1, c0, c1, fmt):
            return {"repeatCell": {
                "range": {"sheetId": gid, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": c0, "endColumnIndex": c1},
                "cell": {"userEnteredFormat": fmt},
                "fields": "userEnteredFormat",
            }}

        self._sh.batch_update({"requests": [
            # Título
            {"mergeCells": {"range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 4}, "mergeType": "MERGE_ALL"}},
            repeat(0, 1, 0, 4, {"backgroundColor": NAVY, "textFormat": {"foregroundColor": WHITE, "bold": True, "fontSize": 14}, "horizontalAlignment": "CENTER"}),
            # Selector filas 3-5
            repeat(2, 5, 0, 1, {"textFormat": {"bold": True}, "horizontalAlignment": "RIGHT"}),
            repeat(2, 5, 1, 2, {"backgroundColor": INPUT_BG, "textFormat": {"bold": True}, "horizontalAlignment": "CENTER"}),
            # Subheaders
            {"mergeCells": {"range": {"sheetId": gid, "startRowIndex": 6, "endRowIndex": 7, "startColumnIndex": 0, "endColumnIndex": 3}, "mergeType": "MERGE_ALL"}},
            repeat(6, 7, 0, 3, {"backgroundColor": LIGHT_BLUE, "textFormat": {"bold": True, "fontSize": 11}, "horizontalAlignment": "CENTER"}),
            {"mergeCells": {"range": {"sheetId": gid, "startRowIndex": 13, "endRowIndex": 14, "startColumnIndex": 0, "endColumnIndex": 3}, "mergeType": "MERGE_ALL"}},
            repeat(13, 14, 0, 3, {"backgroundColor": LIGHT_BLUE, "textFormat": {"bold": True, "fontSize": 11}, "horizontalAlignment": "CENTER"}),
            {"mergeCells": {"range": {"sheetId": gid, "startRowIndex": 18, "endRowIndex": 19, "startColumnIndex": 0, "endColumnIndex": 3}, "mergeType": "MERGE_ALL"}},
            repeat(18, 19, 0, 3, {"backgroundColor": LIGHT_BLUE, "textFormat": {"bold": True, "fontSize": 11}, "horizontalAlignment": "CENTER"}),
            # Header tabla
            repeat(7, 8, 0, 3, {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER", "backgroundColor": LIGHT_BLUE}),
            # Currency col C filas 9-12
            repeat(8, 12, 2, 3, {"numberFormat": CURRENCY}),
            # Fila TOTAL
            repeat(11, 12, 0, 3, {"textFormat": {"bold": True}, "backgroundColor": LIGHT_BLUE}),
            # Ingreso neto
            repeat(14, 18, 0, 1, {"textFormat": {"bold": True}, "horizontalAlignment": "RIGHT"}),
            repeat(14, 15, 1, 2, {"numberFormat": CURRENCY, "textFormat": {"bold": True}}),
            repeat(15, 16, 1, 2, {"backgroundColor": INPUT_BG, "numberFormat": CURRENCY, "textFormat": {"bold": True}, "horizontalAlignment": "CENTER"}),
            repeat(16, 17, 1, 2, {"backgroundColor": GREEN_BG, "numberFormat": CURRENCY, "textFormat": {"bold": True, "fontSize": 12}}),
            # Comisión (sobre Exitosas)
            repeat(19, 21, 0, 1, {"textFormat": {"bold": True}, "horizontalAlignment": "RIGHT"}),
            repeat(19, 20, 1, 2, {"backgroundColor": INPUT_BG, "numberFormat": {"type": "NUMBER", "pattern": "0.##\"%\""}, "textFormat": {"bold": True}, "horizontalAlignment": "CENTER"}),
            repeat(20, 21, 1, 2, {"backgroundColor": GREEN_BG, "numberFormat": CURRENCY, "textFormat": {"bold": True, "fontSize": 12}}),
            # Sección Comisión sobre Comisionables (fila 23 = idx 22)
            {"mergeCells": {"range": {"sheetId": gid, "startRowIndex": 22, "endRowIndex": 23, "startColumnIndex": 0, "endColumnIndex": 3}, "mergeType": "MERGE_ALL"}},
            repeat(22, 23, 0, 3, {"backgroundColor": LIGHT_BLUE, "textFormat": {"bold": True, "fontSize": 11}, "horizontalAlignment": "CENTER"}),
            repeat(23, 27, 0, 1, {"textFormat": {"bold": True}, "horizontalAlignment": "RIGHT"}),
            # Cantidad Comisionables (B24): número entero
            repeat(23, 24, 1, 2, {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER"}),
            # Total Comisionables (B25): moneda
            repeat(24, 25, 1, 2, {"numberFormat": CURRENCY, "textFormat": {"bold": True}}),
            # Comisión % (B26): input
            repeat(25, 26, 1, 2, {"backgroundColor": INPUT_BG, "numberFormat": {"type": "NUMBER", "pattern": "0.##\"%\""}, "textFormat": {"bold": True}, "horizontalAlignment": "CENTER"}),
            # Comisión a cobrar (B27): destacado verde
            repeat(26, 27, 1, 2, {"backgroundColor": GREEN_BG, "numberFormat": CURRENCY, "textFormat": {"bold": True, "fontSize": 12}}),
            # Anchos cols A B C D
            *[{"updateDimensionProperties": {"range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i+1}, "properties": {"pixelSize": w}, "fields": "pixelSize"}}
              for i, w in enumerate([240, 200, 160, 20])],
            # Ocultar columna F (helper)
            {"updateDimensionProperties": {"range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": 5, "endIndex": 6}, "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
            # Dropdown en B5
            {"setDataValidation": {
                "range": {"sheetId": gid, "startRowIndex": 4, "endRowIndex": 5, "startColumnIndex": 1, "endColumnIndex": 2},
                "rule": {"condition": {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": f"={REPORTE_SHEET}!$F$1:$F$1000"}]}, "strict": True, "showCustomUi": True},
            }},
        ]})
        logger.info("Hoja '%s' creada", REPORTE_SHEET)

    def _get_or_create_monthly(self, when: datetime) -> gspread.Worksheet:
        title = when.strftime("%Y-%m")
        try:
            return self._sh.worksheet(title)
        except gspread.WorksheetNotFound:
            pass
        with self._monthly_lock:
            try:
                return self._sh.worksheet(title)
            except gspread.WorksheetNotFound:
                ws = self._sh.add_worksheet(title=title, rows=500, cols=len(HEADERS))
                ws.append_row(HEADERS, value_input_option="USER_ENTERED")
                self._format_transactions_sheet(ws)
                logger.info("Hoja mensual '%s' creada", title)
                return ws

    # ------------------------------- append -------------------------------

    def append_transaction(
        self,
        tx: Transaccion,
        *,
        message_id: int,
        sender: str,
        when: datetime,
        image_link: str,
        approved_by: str = "",
    ) -> None:
        # Lock + throttle: en lotes grandes (10–100 fotos paralelas) sin esto, dos
        # append_row concurrentes pueden detectar la misma "primera fila vacía" y la
        # segunda sobreescribe a la primera, perdiendo transacciones silenciosamente.
        with self._write_lock:
            elapsed = time.monotonic() - self._last_write_ts
            if elapsed < self._min_write_interval:
                time.sleep(self._min_write_interval - elapsed)
            try:
                self._do_append_transaction(
                    tx, message_id=message_id, sender=sender, when=when,
                    image_link=image_link, approved_by=approved_by,
                )
            finally:
                self._last_write_ts = time.monotonic()

    def _do_append_transaction(
        self,
        tx: Transaccion,
        *,
        message_id: int,
        sender: str,
        when: datetime,
        image_link: str,
        approved_by: str,
    ) -> None:
        destino_num = f"'{tx.destino_numero}" if tx.destino_numero else ""

        row = [
            str(message_id),
            when.strftime("%Y-%m-%d %H:%M:%S"),
            _safe_cell(sender),
            _safe_cell(tx.banco),
            tx.estado.value,
            _safe_cell(tx.codigo_transaccion),
            _safe_cell(tx.destino_nombre),
            destino_num,  # ya tiene prefijo '
            tx.destino_tipo.value,
            tx.valor,
            tx.moneda,
            _safe_cell(tx.fecha_comprobante or ""),
            image_link,
            _safe_cell(tx.notas_ocr or ""),
            False,  # Verificado
            "",     # Notas Manuales
            False,  # Comisionable
            _safe_cell(approved_by),  # Aprobado por
        ]

        todas = self._sh.worksheet(TODAS_SHEET)
        resp_t = todas.append_row(row, value_input_option="USER_ENTERED", include_values_in_response=True)
        self._add_checkbox_for_appended(todas.id, resp_t)

        monthly = self._get_or_create_monthly(when)
        resp_m = monthly.append_row(row, value_input_option="USER_ENTERED", include_values_in_response=True)
        self._add_checkbox_for_appended(monthly.id, resp_m)
        logger.info("Fila escrita en '%s' y '%s'", TODAS_SHEET, monthly.title)

        # Cross-reference de duplicados REALES (mismo código + destinatario + valor).
        # Esto evita el spam de batch_updates por pseudo-códigos como "TS3177" (que el
        # banco usa como genérico de "Pendiente" sobre muchas tx distintas).
        # Try/except: si falla por quota, NO debe tumbar la transacción que ya se escribió.
        if tx.codigo_transaccion and tx.codigo_transaccion != "N/A":
            try:
                self._update_duplicate_notes(
                    todas, tx.codigo_transaccion, tx.destino_numero, tx.valor,
                )
                self._update_duplicate_notes(
                    monthly, tx.codigo_transaccion, tx.destino_numero, tx.valor,
                )
            except Exception as e:
                logger.warning("Skip cross-ref de duplicados por error: %s", e)

    def _update_duplicate_notes(
        self, ws: gspread.Worksheet, codigo: str, destino_numero: str, valor: float,
    ) -> None:
        """Marca como duplicado solo si código+destinatario+valor coinciden (duplicado real)."""
        all_rows = ws.get_all_values()
        # Cols: F=código(5), H=destino_numero(7), J=valor(9)
        # Normalizamos destino_numero quitando el prefijo "'" que usamos para forzar texto.
        norm_dest = (destino_numero or "").lstrip("'")
        valor_str = str(int(valor)) if valor == int(valor) else str(valor)
        matches = []
        for i, r in enumerate(all_rows, 1):
            if i == 1 or len(r) < 10:
                continue
            if r[5] != codigo:
                continue
            r_dest = (r[7] or "").lstrip("'")
            if r_dest != norm_dest:
                continue
            # valor en sheet viene formateado como "$ 1.234.567" o similar; comparamos por digitos
            r_valor_digits = "".join(c for c in r[9] if c.isdigit())
            if r_valor_digits != valor_str.replace(".", "").replace(",", ""):
                continue
            matches.append(i)
        if len(matches) < 2:
            return

        updates = []
        for row_num in matches:
            others = [str(r) for r in matches if r != row_num]
            note = f"Duplicado con fila(s): {', '.join(others)}"
            updates.append({"range": f"N{row_num}", "values": [[note]]})
        ws.batch_update(updates, value_input_option="USER_ENTERED")
        logger.info("Cross-ref de duplicados en '%s' codigo=%s destino=%s valor=%s filas=%s",
                    ws.title, codigo, norm_dest, valor_str, matches)

    def _add_checkbox_for_appended(self, sheet_gid: int, resp: dict) -> None:
        rng = resp.get("updates", {}).get("updatedRange", "")
        if "!" not in rng:
            return
        cell = rng.split("!")[1].split(":")[0]  # "A7"
        row_num = int("".join(c for c in cell if c.isdigit()))
        bool_rule = {"condition": {"type": "BOOLEAN"}, "strict": True, "showCustomUi": True}
        # Validation para Verificado (col O = idx 14) y Comisionable (col Q = idx 16) en la fila recién appendeada
        self._sh.batch_update({"requests": [
            {"setDataValidation": {
                "range": {"sheetId": sheet_gid, "startRowIndex": row_num - 1, "endRowIndex": row_num,
                          "startColumnIndex": COL_VERIFICADO_IDX, "endColumnIndex": COL_VERIFICADO_IDX + 1},
                "rule": bool_rule,
            }},
            {"setDataValidation": {
                "range": {"sheetId": sheet_gid, "startRowIndex": row_num - 1, "endRowIndex": row_num,
                          "startColumnIndex": COL_COMISIONABLE_IDX, "endColumnIndex": COL_COMISIONABLE_IDX + 1},
                "rule": bool_rule,
            }},
        ]})
