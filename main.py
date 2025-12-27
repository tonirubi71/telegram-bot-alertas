import os
import json
import asyncio
import logging
from typing import Optional, Dict, Any, Tuple

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("telegram-bot")

# ==========
# ENV
# ==========
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DEFAULT_CHAT_ID = os.getenv("CHAT_ID", "").strip()

DEFAULT_LAT = os.getenv("WEATHER_LAT", "41.491").strip()
DEFAULT_LON = os.getenv("WEATHER_LON", "2.365").strip()
DEFAULT_TZ = os.getenv("WEATHER_TIMEZONE", "Europe/Madrid").strip()

CHECK_MINUTES = int(os.getenv("WEATHER_CHECK_MINUTES", "15").strip())

# Umbrales (los usaremos luego para alertas automáticas; ahora solo los dejamos listos)
WIND_GUST_KMH = float(os.getenv("WEATHER_WIND_GUST_KMH", "60").strip())

NOMINATIM_USER_AGENT = os.getenv("NOMINATIM_USER_AGENT", "").strip()

# Archivo de preferencias (simple)
PREFS_FILE = "prefs.json"

# Rate limit para Nominatim (máx 1 req/s)
_LAST_NOMINATIM_CALL = 0.0


def must_env() -> None:
    if not TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN")
    if not DEFAULT_CHAT_ID:
        raise RuntimeError("Falta CHAT_ID")
    if not NOMINATIM_USER_AGENT:
        raise RuntimeError("Falta NOMINATIM_USER_AGENT")


def load_prefs() -> Dict[str, Any]:
    try:
        with open(PREFS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_prefs(prefs: Dict[str, Any]) -> None:
    with open(PREFS_FILE, "w", encoding="utf-8") as f:
        json.dump(prefs, f, ensure_ascii=False, indent=2)


def get_chat_key(chat_id: int) -> str:
    return str(chat_id)


def get_active_location(chat_id: int) -> Tuple[str, str, str]:
    """
    Devuelve (lat, lon, label) para el chat.
    Si no hay ciudad guardada, usa las variables por defecto (Premià).
    """
    prefs = load_prefs()
    key = get_chat_key(chat_id)
    entry = prefs.get(key, {})
    lat = str(entry.get("lat", DEFAULT_LAT))
    lon = str(entry.get("lon", DEFAULT_LON))
    label = str(entry.get("label", "Premià de Mar (por defecto)"))
    return lat, lon, label


def set_active_location(chat_id: int, lat: str, lon: str, label: str) -> None:
    prefs = load_prefs()
    prefs[get_chat_key(chat_id)] = {"lat": lat, "lon": lon, "label": label}
    save_prefs(prefs)


def nominatim_search(query: str, countrycodes: str = "es") -> Optional[Dict[str, Any]]:
    """
    Busca un lugar con Nominatim (OSM).
    Docs: /search con q, format=json, limit=1, addressdetails=1, countrycodes=...
    Requiere User-Agent identificable y sin exceder 1 req/s.
    """
    global _LAST_NOMINATIM_CALL

    # Rate limit simple
    now = asyncio.get_event_loop().time()
    wait = 1.05 - (now - _LAST_NOMINATIM_CALL)
    if wait > 0:
        # Dormimos de forma bloqueante aquí porque esta función se llama desde un handler async;
        # para no complicarlo, hacemos un sleep corto con requests (y la carga es mínima).
        import time
        time.sleep(wait)
    _LAST_NOMINATIM_CALL = asyncio.get_event_loop().time()

    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": query,
        "format": "json",
        "limit": 1,
        "addressdetails": 1,
        "countrycodes": countrycodes,
    }
    headers = {"User-Agent": NOMINATIM_USER_AGENT}

    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data:
        return None
    return data[0]


def open_meteo_forecast(lat: str, lon: str, tz: str) -> Dict[str, Any]:
    """
    Open-Meteo: sin API key. Pedimos current + próximas horas.
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": tz,
        "current": "temperature_2m,wind_speed_10m,wind_gusts_10m,precipitation",
        "hourly": "temperature_2m,precipitation_probability,wind_gusts_10m",
        "forecast_days": 2,
        "wind_speed_unit": "kmh",
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


# ==========
# Commands
# ==========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "✅ Bot activo.\n"
        "Comandos:\n"
        "• /estado\n"
        "• /id\n"
        "• /ciudad <nombre>\n"
        "• /tiempo"
    )


async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🟢 Estado: OK. (Servidor conectado y bot operativo)")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    await update.message.reply_text(
        f"🆔 chat_id: {chat.id}\n"
        f"👤 user_id: {user.id}\n"
        f"💬 tipo: {chat.type}"
    )


async def cmd_ciudad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    args = context.args

    if not args:
        lat, lon, label = get_active_location(chat_id)
        await update.message.reply_text(
            f"📍 Ciudad activa: {label}\n"
            f"🌐 Coordenadas: {lat}, {lon}\n\n"
            "Para cambiar:\n"
            "• /ciudad Barcelona\n"
            "• /ciudad Premià de Mar\n"
            "• /ciudad Mataró"
        )
        return

    query = " ".join(args).strip()
    await update.message.reply_text(f"🔎 Buscando: {query} ...")

    try:
        result = nominatim_search(query=query, countrycodes="es")
        if not result:
            await update.message.reply_text("❌ No he encontrado esa ciudad/población. Prueba con más detalle.")
            return

        lat = str(result.get("lat", "")).strip()
        lon = str(result.get("lon", "")).strip()
        label = str(result.get("display_name", query)).strip()

        if not lat or not lon:
            await update.message.reply_text("❌ Encontré el lugar, pero sin coordenadas válidas.")
            return

        set_active_location(chat_id, lat, lon, label)
        await update.message.reply_text(
            f"✅ Ciudad guardada.\n"
            f"📍 {label}\n"
            f"🌐 {lat}, {lon}\n\n"
            "Ahora usa /tiempo para ver la previsión."
        )

    except Exception as e:
        log.exception("Error en /ciudad: %s", e)
        await update.message.reply_text("❌ Error buscando la ciudad. Inténtalo de nuevo en unos segundos.")


async def cmd_tiempo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    lat, lon, label = get_active_location(chat_id)

    try:
        data = open_meteo_forecast(lat, lon, DEFAULT_TZ)
        current = data.get("current", {})
        temp = current.get("temperature_2m")
        wind = current.get("wind_speed_10m")
        gust = current.get("wind_gusts_10m")
        precip = current.get("precipitation")

        msg = (
            f"🌦️ Tiempo — {label}\n"
            f"📍 {lat}, {lon}\n\n"
            f"Ahora:\n"
            f"• 🌡️ {temp} °C\n"
            f"• 💨 Viento {wind} km/h (racha {gust} km/h)\n"
            f"• 🌧️ Lluvia {precip} mm\n"
        )
        await update.message.reply_text(msg)

    except Exception as e:
        log.exception("Error en /tiempo: %s", e)
        await update.message.reply_text("❌ No he podido obtener el tiempo ahora mismo. Prueba de nuevo.")


# ==========
# Main
# ==========
def main() -> None:
    must_env()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("ciudad", cmd_ciudad))
    app.add_handler(CommandHandler("tiempo", cmd_tiempo))

    log.info("Bot arrancando...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
