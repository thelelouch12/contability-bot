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
REPORTE_SHEET = "Reporte"

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

        if REPORTE_SHEET not in existing:
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

    def _format_transactions_sheet(self, ws: gspread.Worksheet) -> None:
        """Aplica todo el formato de una hoja de transacciones en 1 batch_update."""
        gid = ws.id
        # Reglas condicionales: colorear celda Estado (col E = idx 4) según valor
        state_rules = [
            self._state_color_rule(gid, "Exitosa", {"red": 0.72, "green": 0.88, "blue": 0.72}),    # verde
            self._state_color_rule(gid, "Pendiente", {"red": 1.0, "green": 0.92, "blue": 0.6}),    # amarillo
            self._state_color_rule(gid, "Fallida", {"red": 0.96, "green": 0.78, "blue": 0.78}),    # rojo
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

    def _create_reporte_sheet(self) -> None:
        ws = self._sh.add_worksheet(title=REPORTE_SHEET, rows=40, cols=8)
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
            # Comisión
            repeat(19, 21, 0, 1, {"textFormat": {"bold": True}, "horizontalAlignment": "RIGHT"}),
            repeat(19, 20, 1, 2, {"backgroundColor": INPUT_BG, "numberFormat": {"type": "NUMBER", "pattern": "0.##\"%\""}, "textFormat": {"bold": True}, "horizontalAlignment": "CENTER"}),
            repeat(20, 21, 1, 2, {"backgroundColor": GREEN_BG, "numberFormat": CURRENCY, "textFormat": {"bold": True, "fontSize": 12}}),
            # Anchos cols A B C D
            *[{"updateDimensionProperties": {"range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i+1}, "properties": {"pixelSize": w}, "fields": "pixelSize"}}
              for i, w in enumerate([220, 200, 160, 20])],
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

        # Si hay duplicados de código, escribir cross-reference en Notas OCR
        if tx.codigo_transaccion and tx.codigo_transaccion != "N/A":
            self._update_duplicate_notes(todas, tx.codigo_transaccion)
            self._update_duplicate_notes(monthly, tx.codigo_transaccion)

    def _update_duplicate_notes(self, ws: gspread.Worksheet, codigo: str) -> None:
        """Si hay 2+ filas con el mismo código, escribe en cada una la lista de las otras."""
        all_rows = ws.get_all_values()
        # Encontrar filas (1-indexed) con ese código en col F (idx 5)
        matches = [
            i for i, r in enumerate(all_rows, 1)
            if i > 1 and len(r) > 5 and r[5] == codigo
        ]
        if len(matches) < 2:
            return

        updates = []
        for row_num in matches:
            others = [str(r) for r in matches if r != row_num]
            note = f"Duplicado con fila(s): {', '.join(others)}"
            updates.append({"range": f"N{row_num}", "values": [[note]]})
        ws.batch_update(updates, value_input_option="USER_ENTERED")
        logger.info("Actualizadas notas de duplicados en '%s' para código %s (filas %s)",
                    ws.title, codigo, matches)

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
