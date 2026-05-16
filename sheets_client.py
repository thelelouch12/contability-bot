import logging
import threading
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

HEADERS = [
    "ID Mensaje", "Fecha/Hora", "Remitente", "Banco", "Estado", "Código",
    "Destino - Nombre", "Destino - Número", "Destino - Tipo", "Valor",
    "Moneda", "Fecha Comprobante", "Imagen", "Notas OCR", "Verificado",
    "Notas Manuales",
]

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
COL_WIDTHS = [70, 130, 200, 140, 95, 95, 160, 170, 105, 115, 75, 130, 95, 180, 95, 200]


class SheetsClient:
    def __init__(self, service_account_file: str, sheet_id: str):
        creds = Credentials.from_service_account_file(service_account_file, scopes=SCOPES)
        self._gc = gspread.authorize(creds)
        self._sh = self._gc.open_by_key(sheet_id)
        self._monthly_lock = threading.Lock()
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

        # Crear Resumen solo si no existe (las fórmulas son estables).
        # Si quieres recrearlo, bórralo manualmente del Sheet y reinicia.
        if RESUMEN_SHEET not in existing:
            self._create_resumen_sheet()

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

    def _format_transactions_sheet(self, ws: gspread.Worksheet) -> None:
        """Aplica todo el formato de una hoja de transacciones en 1 batch_update."""
        gid = ws.id
        sp = self._sep  # ',' en en_US, ';' en es/fr/de/it/pt/nl
        # Regla condicional: pintar fila amarilla si el código se repite (duplicado)
        dup_rule = {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": gid,
                        "startRowIndex": 1,
                        "endRowIndex": 1000,
                        "startColumnIndex": 0,
                        "endColumnIndex": len(HEADERS),
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": f'=AND($F2<>""{sp}$F2<>"N/A"{sp}COUNTIF($F:$F{sp}$F2)>1)'}],
                        },
                        "format": {
                            "backgroundColor": {"red": 1.0, "green": 0.92, "blue": 0.6},
                        },
                    },
                },
                "index": 0,
            }
        }
        requests = [
            dup_rule,
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
        ws = self._sh.add_worksheet(title=RESUMEN_SHEET, rows=200, cols=12)
        gid = ws.id
        sep = self._sep
        T = TODAS_SHEET

        # Valores + fórmulas → 1 batch_update con USER_ENTERED
        ws.batch_update(
            [
                {"range": "A1", "values": [["RESUMEN CONTABLE"]]},
                {"range": "A3:C4", "values": [
                    ["Total General:", "", f"=SUM({T}!J2:J)"],
                    ["Transacciones:", "", f"=COUNTA({T}!A2:A)"],
                ]},
                {"range": "A6", "values": [["Por Banco"]]},
                {"range": "A7:C7", "values": [["Banco", "Cantidad", "Total"]]},
                {"range": "A8", "values": [[
                    f'=IFERROR(QUERY({T}!D2:J{sep}"select D, count(D), sum(J) where D is not null group by D order by sum(J) desc label count(D) \'\', sum(J) \'\'"{sep}0){sep}"")'
                ]]},
                {"range": "E6", "values": [["Por Remitente"]]},
                {"range": "E7:G7", "values": [["Remitente", "Cantidad", "Total"]]},
                {"range": "E8", "values": [[
                    f'=IFERROR(QUERY({T}!C2:J{sep}"select C, count(C), sum(J) where C is not null group by C order by sum(J) desc label count(C) \'\', sum(J) \'\'"{sep}0){sep}"")'
                ]]},
                {"range": "I6", "values": [["Por Estado"]]},
                {"range": "I7:K7", "values": [["Estado", "Cantidad", "Total"]]},
                {"range": "I8", "values": [[
                    f'=IFERROR(QUERY({T}!E2:J{sep}"select E, count(E), sum(J) where E is not null group by E order by sum(J) desc label count(E) \'\', sum(J) \'\'"{sep}0){sep}"")'
                ]]},
                {"range": "A50", "values": [["Por Mes"]]},
                {"range": "A51:C51", "values": [["Mes", "Cantidad", "Total"]]},
                {"range": "A52", "values": [[
                    f'=IFERROR(QUERY({{ARRAYFORMULA(IF({T}!B2:B=""{sep}""{sep}TEXT({T}!B2:B{sep}"yyyy-mm"))){self._arr_col_sep}{T}!J2:J}}{sep}"select Col1, count(Col1), sum(Col2) where Col1 is not null and Col1 <> \'\' group by Col1 order by Col1 desc label count(Col1) \'\', sum(Col2) \'\'"{sep}0){sep}"")'
                ]]},
            ],
            value_input_option="USER_ENTERED",
        )

        # Formato + merges + anchos + freeze → 1 batch_update
        requests = [
            {"updateSheetProperties": {
                "properties": {"sheetId": gid, "gridProperties": {"frozenRowCount": 4}},
                "fields": "gridProperties.frozenRowCount",
            }},
            # Título A1:L1 merge + format
            {"mergeCells": {
                "range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 12},
                "mergeType": "MERGE_ALL",
            }},
            self._repeat_cell(gid, 0, 1, 0, 12, TITLE_FMT, "userEnteredFormat"),
            # Total general label
            self._repeat_cell(gid, 2, 4, 0, 2, {"textFormat": {"bold": True}, "horizontalAlignment": "RIGHT"}, "userEnteredFormat"),
            self._repeat_cell(gid, 2, 3, 2, 3, {**CURRENCY_FMT, "textFormat": {"bold": True, "fontSize": 12}}, "userEnteredFormat"),
            # Subheaders por sección
            {"mergeCells": {"range": {"sheetId": gid, "startRowIndex": 5, "endRowIndex": 6, "startColumnIndex": 0, "endColumnIndex": 3}, "mergeType": "MERGE_ALL"}},
            self._repeat_cell(gid, 5, 6, 0, 3, SUBHEADER_FMT, "userEnteredFormat"),
            self._repeat_cell(gid, 6, 7, 0, 3, {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER", "backgroundColor": LIGHT_BLUE}, "userEnteredFormat"),
            self._repeat_cell(gid, 7, None, 2, 3, CURRENCY_FMT, "userEnteredFormat.numberFormat"),

            {"mergeCells": {"range": {"sheetId": gid, "startRowIndex": 5, "endRowIndex": 6, "startColumnIndex": 4, "endColumnIndex": 7}, "mergeType": "MERGE_ALL"}},
            self._repeat_cell(gid, 5, 6, 4, 7, SUBHEADER_FMT, "userEnteredFormat"),
            self._repeat_cell(gid, 6, 7, 4, 7, {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER", "backgroundColor": LIGHT_BLUE}, "userEnteredFormat"),
            self._repeat_cell(gid, 7, None, 6, 7, CURRENCY_FMT, "userEnteredFormat.numberFormat"),

            {"mergeCells": {"range": {"sheetId": gid, "startRowIndex": 5, "endRowIndex": 6, "startColumnIndex": 8, "endColumnIndex": 11}, "mergeType": "MERGE_ALL"}},
            self._repeat_cell(gid, 5, 6, 8, 11, SUBHEADER_FMT, "userEnteredFormat"),
            self._repeat_cell(gid, 6, 7, 8, 11, {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER", "backgroundColor": LIGHT_BLUE}, "userEnteredFormat"),
            self._repeat_cell(gid, 7, None, 10, 11, CURRENCY_FMT, "userEnteredFormat.numberFormat"),

            # Por Mes (fila 50/51 → idx 49/50)
            {"mergeCells": {"range": {"sheetId": gid, "startRowIndex": 49, "endRowIndex": 50, "startColumnIndex": 0, "endColumnIndex": 3}, "mergeType": "MERGE_ALL"}},
            self._repeat_cell(gid, 49, 50, 0, 3, SUBHEADER_FMT, "userEnteredFormat"),
            self._repeat_cell(gid, 50, 51, 0, 3, {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER", "backgroundColor": LIGHT_BLUE}, "userEnteredFormat"),
            self._repeat_cell(gid, 51, None, 2, 3, CURRENCY_FMT, "userEnteredFormat.numberFormat"),

            # Anchos
            *[
                {"updateDimensionProperties": {
                    "range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                    "properties": {"pixelSize": w},
                    "fields": "pixelSize",
                }}
                for i, w in enumerate([160, 80, 130, 20, 200, 80, 130, 20, 130, 80, 130, 20])
            ],
        ]
        self._sh.batch_update({"requests": requests})
        logger.info("Hoja '%s' recreada con formato", RESUMEN_SHEET)

    # ------------------------------- hojas mensuales -------------------------------

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
    ) -> None:
        destino_num = f"'{tx.destino_numero}" if tx.destino_numero else ""

        row = [
            str(message_id),
            when.strftime("%Y-%m-%d %H:%M:%S"),
            sender,
            tx.banco,
            tx.estado.value,
            tx.codigo_transaccion,
            tx.destino_nombre,
            destino_num,
            tx.destino_tipo.value,
            tx.valor,
            tx.moneda,
            tx.fecha_comprobante or "",
            image_link,
            tx.notas_ocr or "",
            False,
            "",
        ]

        todas = self._sh.worksheet(TODAS_SHEET)
        resp_t = todas.append_row(row, value_input_option="USER_ENTERED", include_values_in_response=True)
        self._add_checkbox_for_appended(todas.id, resp_t)

        monthly = self._get_or_create_monthly(when)
        resp_m = monthly.append_row(row, value_input_option="USER_ENTERED", include_values_in_response=True)
        self._add_checkbox_for_appended(monthly.id, resp_m)
        logger.info("Fila escrita en '%s' y '%s'", TODAS_SHEET, monthly.title)

    def _add_checkbox_for_appended(self, sheet_gid: int, resp: dict) -> None:
        rng = resp.get("updates", {}).get("updatedRange", "")
        if "!" not in rng:
            return
        cell = rng.split("!")[1].split(":")[0]  # "A7"
        row_num = int("".join(c for c in cell if c.isdigit()))
        self._sh.batch_update({"requests": [{
            "setDataValidation": {
                "range": {
                    "sheetId": sheet_gid,
                    "startRowIndex": row_num - 1,
                    "endRowIndex": row_num,
                    "startColumnIndex": 14,
                    "endColumnIndex": 15,
                },
                "rule": {"condition": {"type": "BOOLEAN"}, "strict": True, "showCustomUi": True},
            }
        }]})
