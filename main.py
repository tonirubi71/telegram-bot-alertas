import os
import json
import time
import logging
from typing import Optional, Dict, Any, Tuple

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("alertas-bot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# Por defecto (Premià de Mar)
DEFAULT_CITY_NAME = os.getenv("DEFAULT_CITY_NAME", "Premià de Mar").strip()
DEFAULT_LAT = os.getenv("DEFAULT_LAT", "41.491").strip()
DEFAULT_LON = os.getenv("DEFAULT_LON", "2.365").strip()
DEFAULT_TZ = os.getenv("WEATHER_TIMEZONE", "Europe/Madrid").strip()

NOMINATIM_USER_AGENT = os.getenv("NOMINATIM_USER_AGENT", "").strip()

PREFS_FILE = "prefs.json"

_last_nominatim_ts = 0.0


def must_env():
    if not TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN")
    if not CHAT_ID:
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


def get_active_location(chat_id: int) -> Tuple[str, str, str]:
    prefs = load_prefs()
    entry = prefs.get(str(chat_id), {})
    lat = str(entry.get("lat", DEFAULT_LAT))
    lon = str(entry.get("lon", DEFAULT_LON))
    label = str(entry.get("label", f"{DEFAULT_CITY_NAME} (por defecto)"))
    return lat, lon, label


def set_active_location(chat_id: int, lat: str, lon: str, label: str) -> None:
    prefs = load_prefs()
    prefs[str(chat_id)] = {"lat": lat, "lon": lon, "label": label}
    save_prefs(prefs)


def _rate_limit_nominatim():
    global _last_nominatim_ts
    now = time.time()
    elapsed = now - _last_nominatim_ts
    if elapsed < 1.0:
        time.sleep(1.05 - elapsed)
    _last_nominatim_ts = time.time()


def nominatim_search(query: str, countrycodes: str = "es") -> Optional[Dict[str, Any]]:
    """
    Nominatim /search: q, format=json, limit=1, addressdetails=1, countrycodes=...
    - Requiere User-Agent identificable.
    - Máx ~1 req/s.
    """
    _rate_limit_nominatim()

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
    return data[0] if data else None


def open_meteo_now(lat: str, lon: str, tz: str) -> Dict[str, Any]:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": tz,
        "current": "temperature_2m,wind_speed_10m,wind_gusts_10m,precipitation",
        "hourly": "precipitation_probability",
        "forecast_days": 1,
        "wind_speed_unit": "kmh",
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bot activo.\n"
        "Comandos:\n"
        "• /estado\n"
        "• /id\n"
        "• /ciudad <nombre>\n"
        "• /tiempo"
    )


async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lat, lon, label = get_active_location(update.effective_chat.id)
    await update.message.reply_text(f"🟢 Estado OK.\n📍 Ciudad activa: {label}\n🌐 {lat}, {lon}")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 chat_id: {update.effective_chat.id}")


async def cmd_ciudad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if not context.args:
        lat, lon, label = get_active_location(chat_id)
        await update.message.reply_text(
            f"📍 Ciudad activa: {label}\n"
            f"🌐 {lat}, {lon}\n\n"
            "Para cambiar:\n"
            "/ciudad Barcelona\n"
            "/ciudad Mataró\n"
            "/ciudad Premià de Mar"
        )
        return

    query = " ".join(context.args).strip()
    await update.message.reply_text(f"🔎 Buscando: {query} ...")

    try:
        res = nominatim_search(query=query, countrycodes="es")
        if not res:
            await update.message.reply_text("❌ No he encontrado ese lugar. Prueba a ser más específico.")
            return

        lat = str(res.get("lat", "")).strip()
        lon = str(res.get("lon", "")).strip()
        label = str(res.get("display_name", query)).strip()

        if not lat or not lon:
            await update.message.reply_text("❌ Encontré el lugar, pero no trae coordenadas válidas.")
            return

        set_active_location(chat_id, lat, lon, label)
        await update.message.reply_text(
            f"✅ Guardado.\n📍 {label}\n🌐 {lat}, {lon}\n\nAhora usa /tiempo."
        )
    except Exception as e:
        log.exception("Error /ciudad: %s", e)
        await update.message.reply_text("❌ Error buscando la ciudad. Inténtalo en unos segundos.")


async def cmd_tiempo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lat, lon, label = get_active_location(chat_id)

    try:
        data = open_meteo_now(lat, lon, DEFAULT_TZ)
        cur = data.get("current", {})
        temp = cur.get("temperature_2m")
        wind = cur.get("wind_speed_10m")
        gust = cur.get("wind_gusts_10m")
        rain = cur.get("precipitation")

        await update.message.reply_text(
            f"🌦️ Tiempo — {label}\n"
            f"🌡️ {temp} °C\n"
            f"💨 {wind} km/h (racha {gust} km/h)\n"
            f"🌧️ {rain} mm\n"
            f"ℹ️ Fuente: Open-Meteo"
        )
    except Exception as e:
        log.exception("Error /tiempo: %s", e)
        await update.message.reply_text("❌ No he podido obtener el tiempo ahora mismo.")


def main():
    must_env()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("ciudad", cmd_ciudad))
    app.add_handler(CommandHandler("tiempo", cmd_tiempo))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
