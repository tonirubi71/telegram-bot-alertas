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

# Alertas meteo
WEATHER_CHECK_MINUTES = int(os.getenv("WEATHER_CHECK_MINUTES", "15").strip())
WEATHER_WIND_GUST_KMH = float(os.getenv("WEATHER_WIND_GUST_KMH", "60").strip())
WEATHER_RAIN_MM_H = float(os.getenv("WEATHER_RAIN_MM_H", "5").strip())
WEATHER_RAIN_PROB_PCT = float(os.getenv("WEATHER_RAIN_PROB_PCT", "80").strip())
ALERT_COOLDOWN_MIN = int(os.getenv("ALERT_COOLDOWN_MIN", "60").strip())

NOMINATIM_USER_AGENT = os.getenv("NOMINATIM_USER_AGENT", "").strip()

PREFS_FILE = "prefs.json"

_last_nominatim_ts = 0.0
_last_alert_ts = 0.0
_last_alert_signature = None


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


def open_meteo_hourly(lat: str, lon: str, tz: str) -> Dict[str, Any]:
    """
    Pedimos:
    - current: racha de viento, precipitación actual
    - hourly: precipitación y probabilidad por hora, rachas de viento por hora
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": tz,
        "current": "temperature_2m,wind_gusts_10m,precipitation",
        "hourly": "precipitation,precipitation_probability,wind_gusts_10m",
        "forecast_days": 1,
        "wind_speed_unit": "kmh",
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def build_time_text(city: str, data: dict) -> str:
    cur = data.get("current", {})
    temp = cur.get("temperature_2m")
    gust = cur.get("wind_gusts_10m")
    rain = cur.get("precipitation")
    return (
        f"🌦️ Tiempo — {city}\n"
        f"🌡️ {temp} °C\n"
        f"💨 Racha: {gust} km/h\n"
        f"🌧️ Lluvia: {rain} mm\n"
        f"ℹ️ Fuente: Open-Meteo"
    )


def compute_weather_alert(city: str, data: dict) -> Optional[Tuple[str, str]]:
    """
    Genera alerta si:
    - racha actual >= umbral
    - en próximas 6h: precipitación >= umbral mm/h
    - en próximas 6h: prob lluvia >= umbral
    Devuelve (signature, message) o None.
    """
    cur = data.get("current", {}) or {}
    gust_now = cur.get("wind_gusts_10m")
    rain_now = cur.get("precipitation")

    hourly = data.get("hourly", {}) or {}
    times = hourly.get("time", []) or []
    pr = hourly.get("precipitation", []) or []
    pp = hourly.get("precipitation_probability", []) or []
    gusts = hourly.get("wind_gusts_10m", []) or []

    # Ventana próximas 6h
    max_pp = 0.0
    max_pr = 0.0
    max_gust = 0.0
    for i in range(min(len(times), 24)):
        # tomamos 6 primeras horas a partir del índice 0 (Open-Meteo devuelve desde hoy 00:00 local;
        # es suficiente para alertas simples, y evita líos con parseo de fechas)
        if i >= 6:
            break
        if i < len(pp) and pp[i] is not None:
            max_pp = max(max_pp, float(pp[i]))
        if i < len(pr) and pr[i] is not None:
            max_pr = max(max_pr, float(pr[i]))
        if i < len(gusts) and gusts[i] is not None:
            max_gust = max(max_gust, float(gusts[i]))

    reasons = []

    try:
        if gust_now is not None and float(gust_now) >= WEATHER_WIND_GUST_KMH:
            reasons.append(f"💨 Rachas fuertes ahora ({float(gust_now):.0f} km/h)")
    except Exception:
        pass

    if max_gust >= WEATHER_WIND_GUST_KMH:
        reasons.append(f"💨 Rachas fuertes próximas horas (máx {max_gust:.0f} km/h)")

    if max_pp >= WEATHER_RAIN_PROB_PCT:
        reasons.append(f"🌧️ Alta prob. de lluvia (máx {max_pp:.0f}%)")

    if max_pr >= WEATHER_RAIN_MM_H:
        reasons.append(f"🌧️ Lluvia intensa posible (hasta {max_pr:.1f} mm/h)")

    if not reasons:
        return None

    signature = "|".join(reasons)
    msg = "⚠️ Alerta meteorológica\n" + "\n".join(f"• {r}" for r in reasons) + f"\n\n📍 {city}\nℹ️ Fuente: Open-Meteo"
    return signature, msg


async def maybe_send_weather_alert(app: Application) -> None:
    global _last_alert_ts, _last_alert_signature

    try:
        chat_id = int(CHAT_ID)
    except Exception:
        return

    lat, lon, label = get_active_location(chat_id)

    data = open_meteo_hourly(lat, lon, DEFAULT_TZ)
    alert = compute_weather_alert(label, data)
    if not alert:
        return

    signature, msg = alert

    now = time.time()
    cooldown_ok = (now - _last_alert_ts) >= (ALERT_COOLDOWN_MIN * 60)

    # Evita spam: si es la misma alerta y aún no pasó cooldown, no enviamos
    if (signature == _last_alert_signature) and (not cooldown_ok):
        return

    await app.bot.send_message(chat_id=chat_id, text=msg)
    _last_alert_signature = signature
    _last_alert_ts = now


async def weather_loop(app: Application):
    # mensaje de arranque (una sola vez)
    try:
        await app.bot.send_message(chat_id=int(CHAT_ID), text="✅ Alertas de tiempo activadas.")
    except Exception:
        pass

    while True:
        try:
            await maybe_send_weather_alert(app)
        except Exception as e:
            log.exception("Error en weather_loop: %s", e)
        await asyncio_sleep_minutes(WEATHER_CHECK_MINUTES)


async def asyncio_sleep_minutes(minutes: int):
    import asyncio
    await asyncio.sleep(max(60, minutes * 60))


# ---- comandos ----
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bot activo.\n"
        "Comandos:\n"
        "• /estado\n"
        "• /id\n"
        "• /ciudad <nombre>\n"
        "• /tiempo\n"
        "• /alertas_tiempo (forzar comprobación)"
    )


async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lat, lon, label = get_active_location(update.effective_chat.id)
    await update.message.reply_text(
        f"🟢 Estado OK.\n"
        f"📍 Ciudad activa: {label}\n"
        f"🌐 {lat}, {lon}\n"
        f"⏱️ Revisión: cada {WEATHER_CHECK_MINUTES} min\n"
        f"Umbrales: racha≥{WEATHER_WIND_GUST_KMH:.0f} km/h | prob lluvia≥{WEATHER_RAIN_PROB_PCT:.0f}% | lluvia≥{WEATHER_RAIN_MM_H:.1f} mm/h"
    )


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
        await update.message.reply_text(f"✅ Guardado.\n📍 {label}\n🌐 {lat}, {lon}\n\nAhora usa /tiempo.")
    except Exception as e:
        log.exception("Error /ciudad: %s", e)
        await update.message.reply_text("❌ Error buscando la ciudad. Inténtalo en unos segundos.")


async def cmd_tiempo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lat, lon, label = get_active_location(chat_id)

    try:
        data = open_meteo_hourly(lat, lon, DEFAULT_TZ)
        await update.message.reply_text(build_time_text(label, data))
    except Exception as e:
        log.exception("Error /tiempo: %s", e)
        await update.message.reply_text("❌ No he podido obtener el tiempo ahora mismo.")


async def cmd_alertas_tiempo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔔 Comprobando alertas de tiempo…")
    try:
        await maybe_send_weather_alert(context.application)
        await update.message.reply_text("✅ Comprobación hecha. Si había alerta, ya la envié.")
    except Exception:
        await update.message.reply_text("❌ Error comprobando alertas.")


async def post_init(app: Application):
    # Lanzamos el loop de alertas como tarea de fondo gestionada por PTB
    app.create_task(weather_loop(app))


def main():
    must_env()

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("ciudad", cmd_ciudad))
    app.add_handler(CommandHandler("tiempo", cmd_tiempo))
    app.add_handler(CommandHandler("alertas_tiempo", cmd_alertas_tiempo))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
