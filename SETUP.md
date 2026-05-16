# Setup — contability-bot

Guía paso a paso para conectar las cuentas y desplegar el bot.

## 1. Crear el bot de Telegram

1. Abre Telegram y busca **@BotFather**.
2. Envía `/newbot`, ponle un nombre (ej. `MiContabilidadBot`) y un username terminado en `bot` (ej. `micontabilidad_bot`).
3. BotFather te devuelve el **HTTP API token** (`123456:ABC-DEF...`). Guárdalo: irá en `TELEGRAM_BOT_TOKEN`.
4. Envía `/setprivacy` → elige tu bot → **Disable** (para que el bot pueda leer todos los mensajes del grupo, no solo los que lo mencionen).
5. Agrega el bot al grupo donde llegan los comprobantes. Hazlo **administrador** si vas a tener restricciones de lectura.

## 2. Crear proyecto en Google Cloud

1. Entra a https://console.cloud.google.com/ y crea un proyecto nuevo (ej. `contability-bot`).
2. Habilita estas APIs (busca cada una en el buscador y click en "Habilitar"):
   - **Google Sheets API**
   - **Google Drive API**
   - **Generative Language API** (Gemini)

## 3. Obtener API key de Gemini

1. Entra a https://aistudio.google.com/apikey
2. Click en **Create API key** → selecciona el proyecto del paso 2.
3. Copia la key → irá en `GEMINI_API_KEY`.

## 4. Crear Service Account (para Sheets + Drive)

1. En Google Cloud Console → **IAM & Admin** → **Service Accounts** → **Create service account**.
2. Nombre: `contability-bot-sa`. Continúa sin asignar roles (no necesarios para Sheets/Drive a nivel de proyecto).
3. Una vez creada, entra a la SA → pestaña **Keys** → **Add key** → **Create new key** → **JSON**.
4. Se descarga un archivo `*.json`. **Renómbralo a `sa.json`** y guárdalo en `./secrets/sa.json` dentro del proyecto.
5. **Copia el email** de la service account (formato `contability-bot-sa@<proyecto>.iam.gserviceaccount.com`). Lo necesitarás abajo.

## 5. Crear el Google Sheet

1. Entra a https://sheets.google.com/ y crea una hoja nueva (ej. `Contabilidad Telegram`).
2. Copia el **ID** de la URL: `https://docs.google.com/spreadsheets/d/<ESTE_ID>/edit` → irá en `GOOGLE_SHEET_ID`.
3. Click en **Compartir** (botón azul arriba a la derecha) → pega el email de la service account → permiso **Editor** → Enviar.

## 6. Crear Shared Drive y carpeta para las imágenes

> **IMPORTANTE:** Las service accounts de Google **NO** tienen cuota de almacenamiento en
> "My Drive". **Es obligatorio usar un Shared Drive** (Drive compartido), de lo contrario
> las subidas fallarán con error `403 storageQuotaExceeded`.

1. Entra a https://drive.google.com/ → menú izquierdo → **Shared drives** → **+ Create a shared drive**.
   - Nombre: por ejemplo `Contabilidad Bot`.
   - Déjalo configurado como `Anyone in <tu-organización> cannot access files`.
2. Dentro del Shared Drive recién creado, haz click en **Manage members** (arriba a la derecha, junto al nombre).
   - Agrega el email de la service account (formato `...@<proyecto>.iam.gserviceaccount.com`).
   - Rol: **Content Manager**.
3. Dentro del Shared Drive, crea una carpeta (ej. `Comprobantes Telegram`).
4. Copia el **ID** de la carpeta desde la URL: `https://drive.google.com/drive/folders/<ESTE_ID>` → irá en `GOOGLE_DRIVE_FOLDER_ID`.
5. (Opcional) Copia el **ID** del Shared Drive desde la URL: `https://drive.google.com/drive/folders/<ID>?ths=true` y guárdalo. No se usa por ahora pero puede ser útil en el futuro.

## 7. Configurar el `.env`

```bash
cp .env.example .env
```

Edita `.env` y rellena:

```
TELEGRAM_BOT_TOKEN=<paso 1>
GEMINI_API_KEY=<paso 3>
GOOGLE_SERVICE_ACCOUNT_FILE=./secrets/sa.json   # local; en Docker usa /app/secrets/sa.json
GOOGLE_SHEET_ID=<paso 5>
GOOGLE_DRIVE_FOLDER_ID=<paso 6>
TIMEZONE=America/Bogota
DEFAULT_CURRENCY=COP
```

`ALLOWED_CHAT_IDS` déjalo vacío al inicio.

## 8. Probar localmente

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

En Telegram:
- Envía `/start` al bot en privado para verificar que responde.
- Envía `/id` dentro del grupo de comprobantes → copia el `chat_id` (será negativo, ej. `-1001234567890`).
- Pon ese ID en `ALLOWED_CHAT_IDS` del `.env` y reinicia el bot.
- Envía una foto de un comprobante al grupo → debería responder con el resumen y aparecer una fila nueva en el Sheet.

## 9. Desplegar en VPS con Coolify

1. Sube el repo a GitHub (privado recomendado).
2. En Coolify → New Resource → **Application** → conecta tu repo.
3. Build pack: **Dockerfile**.
4. Variables de entorno: pega todo el `.env` (sin `GOOGLE_SERVICE_ACCOUNT_FILE`, lo sobreescribimos).
5. Sobre `GOOGLE_SERVICE_ACCOUNT_FILE` ponle `/app/secrets/sa.json`.
6. Storage / Volumes:
   - Tipo **File Mount** → ruta destino `/app/secrets/sa.json` → contenido: pega el JSON de la service account.
7. **Deploy**. Mira los logs: deberías ver `Bot iniciado, esperando mensajes...`.

## 10. Estructura del Sheet generada

El bot crea automáticamente al primer arranque:
- **Todas**: una fila por comprobante (todas las transacciones históricas).
- **YYYY-MM** (ej. `2026-05`): se crea al recibir la primera transacción de cada mes.
- **Resumen**: totales por banco, por remitente, por estado, y total general (fórmulas).

Las columnas **Verificado** y **Notas Manuales** son para tu uso — el bot nunca las sobrescribe (solo hace append).
