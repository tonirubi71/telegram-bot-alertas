import os
import json
import time
import logging
import re
import xml.etree.ElementTree as ET
from typing import Optional, Dict, Any, Tuple, List

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("alertas-bot")

# =====================
# ENV
# =====================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# Ciudad por defecto
DEFAULT_CITY_NAME = os.getenv("DEFAULT_CITY_NAME", "Premià de Mar").strip()
DEFAULT_LAT = os.getenv("DEFAULT_LAT", "41.491").strip()
DEFAULT_LON = os.getenv("DEFAULT_LON", "2.365").strip()
DEFAULT_TZ = os.getenv("WEATHER_TIMEZONE", "Europe/Madrid").strip()

# Nominatim
NOMINATIM_USER_AGENT = os.getenv("NOMINATIM_USER_AGENT", "").strip()

# Alertas tiempo (estricto automático)
WEATHER_CHECK_MINUTES = int(os.getenv("WEATHER_CHECK_MINUTES", "15").strip())
WEATHER_WIND_GUST_KMH = float(os.getenv("WEATHER_WIND_GUST_KMH", "60").strip())
WEATHER_RAIN_MM_H = float(os.getenv("WEATHER_RAIN_MM_H", "5").strip())
WEATHER_RAIN_PROB_PCT = float(os.getenv("WEATHER_RAIN_PROB_PCT", "80").strip())
ALERT_COOLDOWN_MIN = int(os.getenv("ALERT_COOLDOWN_MIN", "60").strip())

# Trenes/Aire (intervalo)
TRAIN_AIR_CHECK_MINUTES = int(os.getenv("TRAIN_AIR_CHECK_MINUTES", "10").strip())

RODALIES_R1_RSS = os.getenv("RODALIES_R1_RSS", "").strip()
RODALIES_RG1_RSS = os.getenv("RODALIES_RG1_RSS", "").strip()
AIR_QUALITY_URL = os.getenv("AIR_QUALITY_URL", "").strip()

# AEMET
AEMET_ALERTS_RSS = os.getenv("AEMET_ALERTS_RSS", "https://www.aemet.es/en/rss_info/avisos/cat").strip()

# Preferencias (nota: pueden perderse en redeploy; tú elegiste dejarlo así)
PREFS_FILE = "prefs.json"

# Rate limit Nominatim
_last_nominatim_ts = 0.0

# Anti-spam / deduplicación
_state = {
    "weather_last_sig": None,
    "weather_last_ts": 0.0,
    "train_r1_last_id": None,
    "train_rg1_last_id": None,
    "aemet_last_id": None,
    "air_last_sig": None,
    "air_last_ts": 0.0,
}


# =====================
# Utils
# =====================
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
    # Política: User-Agent identificable + no más de ~1 req/s. (Cumplimos)  :contentReference[oaicite:5]{index=5}
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
    # Docs Open-Meteo: /v1/forecast + variables hourly/current.  :contentReference[oaicite:6]{index=6}
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
    cur = data.get("current", {}) or {}
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


def _max_first_n(values: List[Any], n: int) -> float:
    mx = 0.0
    for x in values[:n]:
        if x is None:
            continue
        try:
            mx = max(mx, float(x))
        except Exception:
            continue
    return mx


def compute_weather_alert(city: str, data: dict, *, gust_kmh: float, rain_prob_pct: float, rain_mm_h: float, label: str) -> Optional[Tuple[str, str]]:
    cur = data.get("current", {}) or {}
    gust_now = cur.get("wind_gusts_10m")

    hourly = data.get("hourly", {}) or {}
    pp = hourly.get("precipitation_probability", []) or []
    pr = hourly.get("precipitation", []) or []
    gusts = hourly.get("wind_gusts_10m", []) or []

    max_pp = _max_first_n(pp, 6)
    max_pr = _max_first_n(pr, 6)
    max_gust = _max_first_n(gusts, 6)

    reasons = []
    try:
        if gust_now is not None and float(gust_now) >= gust_kmh:
            reasons.append(f"💨 Rachas fuertes ahora ({float(gust_now):.0f} km/h)")
    except Exception:
        pass

    if max_gust >= gust_kmh:
        reasons.append(f"💨 Rachas fuertes próximas horas (máx {max_gust:.0f} km/h)")
    if max_pp >= rain_prob_pct:
        reasons.append(f"🌧️ Prob. lluvia alta (máx {max_pp:.0f}%)")
    if max_pr >= rain_mm_h:
        reasons.append(f"🌧️ Lluvia intensa posible (hasta {max_pr:.1f} mm/h)")

    if not reasons:
        return None

    signature = "|".join(reasons) + f"|{label}"
    msg = (
        f"⚠️ Alerta meteorológica ({label})\n"
        + "\n".join(f"• {r}" for r in reasons)
        + f"\n\n📍 {city}\nℹ️ Fuente: Open-Meteo"
    )
    return signature, msg


# =====================
# RSS (AEMET / Rodalies)
# =====================
def parse_rss_items(url: str, limit: int = 5) -> List[Dict[str, str]]:
    """
    Devuelve lista de items con keys: id,title,link,pubDate/updated
    Soporta RSS y Atom básico.
    """
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    xml = r.text.strip()
    root = ET.fromstring(xml)

    items: List[Dict[str, str]] = []

    # RSS
    channel = root.find("channel")
    if channel is not None:
        for it in channel.findall("item")[:limit]:
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            guid = (it.findtext("guid") or link or title).strip()
            pub = (it.findtext("pubDate") or "").strip()
            items.append({"id": guid, "title": title, "link": link, "when": pub})
        return items

    # Atom (muy simple)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = root.findall("atom:entry", ns) or root.findall("entry")
    for e in entries[:limit]:
        title = (e.findtext("atom:title", default="", namespaces=ns) or e.findtext("title") or "").strip()
        eid = (e.findtext("atom:id", default="", namespaces=ns) or e.findtext("id") or title).strip()
        updated = (e.findtext("atom:updated", default="", namespaces=ns) or e.findtext("updated") or "").strip()
        link_el = e.find("atom:link", ns) or e.find("link")
        link = (link_el.get("href") if link_el is not None and link_el.get("href") else "").strip()
        items.append({"id": eid, "title": title, "link": link, "when": updated})
    return items


def looks_serious_train(title: str) -> bool:
    t = (title or "").lower()
    # Catalán / castellano típicos de incidencias
    keywords = [
        "incid", "avaria", "avería", "suprimit", "suprimido", "cancel",
        "interromp", "interrump", "susp", "retard", "retras", "demora",
        "servei", "servicio", "limit", "tall", "corte", "sense servei", "sin servicio",
    ]
    return any(k in t for k in keywords)


# =====================
# Air quality (genérico)
# =====================
def summarize_air_quality(data: Any) -> str:
    """
    Como no sabemos el esquema de tu AIR_QUALITY_URL,
    hacemos un resumen "robusto":
    - si hay campo tipo 'aqi'/'ica'/'index' en el primer objeto, lo mostramos
    - si no, mostramos claves principales para confirmar que responde
    """
    # Lista de posibles campos comunes
    candidate_fields = ["aqi", "ica", "index", "quality", "qualitat", "value", "valor"]

    # data puede ser dict o list
    if isinstance(data, list) and data:
        obj = data[0]
        if isinstance(obj, dict):
            for f in candidate_fields:
                if f in obj:
                    return f"🌫️ Aire: {f}={obj.get(f)}"
            return f"🌫️ Aire: OK (lista) — claves: {', '.join(list(obj.keys())[:6])}"
        return "🌫️ Aire: OK (lista)"
    if isinstance(data, dict):
        for f in candidate_fields:
            if f in data:
                return f"🌫️ Aire: {f}={data.get(f)}"
        return f"🌫️ Aire: OK — claves: {', '.join(list(data.keys())[:8])}"
    return "🌫️ Aire: OK"


def air_signature(data: Any) -> str:
    # Firma simple para deduplicar (sin depender de esquema)
    try:
        s = json.dumps(data, ensure_ascii=False, sort_keys=True)
        return str(hash(s))
    except Exception:
        return str(hash(str(data)))


# =====================
# Background loops
# =====================
async def maybe_send_weather_alert(app: Application) -> None:
    chat_id = int(CHAT_ID)
    lat, lon, city = get_active_location(chat_id)
    data = open_meteo_hourly(lat, lon, DEFAULT_TZ)

    alert = compute_weather_alert(
        city,
        data,
        gust_kmh=WEATHER_WIND_GUST_KMH,
        rain_prob_pct=WEATHER_RAIN_PROB_PCT,
        rain_mm_h=WEATHER_RAIN_MM_H,
        label="estricto",
    )
    if not alert:
        return

    sig, msg = alert
    now = time.time()
    cooldown_ok = (now - _state["weather_last_ts"]) >= (ALERT_COOLDOWN_MIN * 60)

    if sig == _state["weather_last_sig"] and not cooldown_ok:
        return

    await app.bot.send_message(chat_id=chat_id, text=msg)
    _state["weather_last_sig"] = sig
    _state["weather_last_ts"] = now


async def weather_loop(app: Application):
    try:
        await app.bot.send_message(chat_id=int(CHAT_ID), text="✅ Alertas de tiempo activadas (modo estricto automático).")
    except Exception:
        pass

    import asyncio
    while True:
        try:
            await maybe_send_weather_alert(app)
        except Exception as e:
            log.exception("weather_loop error: %s", e)
        await asyncio.sleep(max(60, WEATHER_CHECK_MINUTES * 60))


async def check_trains_and_air(app: Application) -> None:
    chat_id = int(CHAT_ID)

    # R1
    if RODALIES_R1_RSS:
        try:
            items = parse_rss_items(RODALIES_R1_RSS, limit=1)
            if items:
                it = items[0]
                if it["id"] != _state["train_r1_last_id"]:
                    _state["train_r1_last_id"] = it["id"]
                    if looks_serious_train(it["title"]):
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=f"🚆 R1 — Incidencia\n• {it['title']}\n{it['when']}\n{it['link']}".strip()
                        )
        except Exception as e:
            log.exception("R1 check error: %s", e)

    # RG1
    if RODALIES_RG1_RSS:
        try:
            items = parse_rss_items(RODALIES_RG1_RSS, limit=1)
            if items:
                it = items[0]
                if it["id"] != _state["train_rg1_last_id"]:
                    _state["train_rg1_last_id"] = it["id"]
                    if looks_serious_train(it["title"]):
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=f"🚆 RG1 — Incidencia\n• {it['title']}\n{it['when']}\n{it['link']}".strip()
                        )
        except Exception as e:
            log.exception("RG1 check error: %s", e)

    # Aire
    if AIR_QUALITY_URL:
        try:
            r = requests.get(AIR_QUALITY_URL, timeout=20)
            r.raise_for_status()
            data = r.json()
            sig = air_signature(data)

            now = time.time()
            cooldown_ok = (now - _state["air_last_ts"]) >= (ALERT_COOLDOWN_MIN * 60)

            # Solo avisamos si cambió (firma distinta) y pasó cooldown
            if sig != _state["air_last_sig"] and cooldown_ok:
                _state["air_last_sig"] = sig
                _state["air_last_ts"] = now
                summary = summarize_air_quality(data)
                await app.bot.send_message(chat_id=chat_id, text=f"{summary}\nℹ️ Fuente: AIR_QUALITY_URL")
        except Exception as e:
            log.exception("AIR check error: %s", e)


async def trains_air_loop(app: Application):
    import asyncio
    while True:
        try:
            await check_trains_and_air(app)
        except Exception as e:
            log.exception("trains_air_loop error: %s", e)
        await asyncio.sleep(max(60, TRAIN_AIR_CHECK_MINUTES * 60))


async def check_aemet(app: Application) -> None:
    """
    AEMET avisos RSS (Catalunya). Enviamos si hay item nuevo.
    RSS oficial: https://www.aemet.es/en/rss_info/avisos/cat  :contentReference[oaicite:7]{index=7}
    """
    chat_id = int(CHAT_ID)
    try:
        items = parse_rss_items(AEMET_ALERTS_RSS, limit=1)
        if not items:
            return
        it = items[0]
        if it["id"] == _state["aemet_last_id"]:
            return
        _state["aemet_last_id"] = it["id"]

        await app.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ AEMET — Aviso (Catalunya)\n"
                f"• {it['title']}\n"
                f"{it['when']}\n"
                f"{it['link']}".strip()
            )
        )
    except Exception as e:
        log.exception("AEMET check error: %s", e)


async def aemet_loop(app: Application):
    import asyncio
    while True:
        try:
            await check_aemet(app)
        except Exception as e:
            log.exception("aemet_loop error: %s", e)
        await asyncio.sleep(max(120, 15 * 60))  # cada 15 min


# =====================
# Commands
# =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bot activo.\n"
        "Clima:\n"
        "• /tiempo\n"
        "• /alertas_tiempo (estricto)\n"
        "• /alertas_sensible (puntual)\n\n"
        "Trenes:\n"
        "• /trenes\n"
        "• /alertas_trenes (forzar)\n\n"
        "Aire:\n"
        "• /aire\n"
        "• /alertas_aire (forzar)\n\n"
        "AEMET:\n"
        "• /aemet\n"
        "• /alertas_aemet (forzar)\n\n"
        "Otros:\n"
        "• /ciudad <nombre>\n"
        "• /mapa\n"
        "• /estado\n"
        "• /id"
    )


async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lat, lon, label = get_active_location(update.effective_chat.id)
    await update.message.reply_text(
        f"🟢 Estado OK.\n"
        f"📍 Ciudad activa: {label}\n"
        f"🌐 {lat}, {lon}\n\n"
        f"⏱️ Tiempo: cada {WEATHER_CHECK_MINUTES} min (auto estricto)\n"
        f"⏱️ Trenes/Aire: cada {TRAIN_AIR_CHECK_MINUTES} min\n"
        f"⏱️ AEMET: cada 15 min\n"
        f"Umbrales estricto: racha≥{WEATHER_WIND_GUST_KMH:.0f} | prob≥{WEATHER_RAIN_PROB_PCT:.0f}% | lluvia≥{WEATHER_RAIN_MM_H:.1f} mm/h"
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
            "/ciudad Premià de Mar\n"
            "/ciudad Mataró"
        )
        return

    query = " ".join(context.args).strip()
    await update.message.reply_text(f"🔎 Buscando: {query} ...")
    try:
        res = nominatim_search(query=query, countrycodes="es")
        if not res:
            await update.message.reply_text("❌ No encontrado. Prueba con más detalle.")
            return
        lat = str(res.get("lat", "")).strip()
        lon = str(res.get("lon", "")).strip()
        label = str(res.get("display_name", query)).strip()
        if not lat or not lon:
            await update.message.reply_text("❌ Encontré el lugar, pero sin coordenadas válidas.")
            return
        set_active_location(chat_id, lat, lon, label)
        await update.message.reply_text(f"✅ Guardado.\n📍 {label}\n🌐 {lat}, {lon}")
    except Exception as e:
        log.exception("Error /ciudad: %s", e)
        await update.message.reply_text("❌ Error buscando la ciudad. Inténtalo luego.")


async def cmd_tiempo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lat, lon, label = get_active_location(update.effective_chat.id)
    try:
        data = open_meteo_hourly(lat, lon, DEFAULT_TZ)
        await update.message.reply_text(build_time_text(label, data))
    except Exception as e:
        log.exception("Error /tiempo: %s", e)
        await update.message.reply_text("❌ No he podido obtener el tiempo ahora mismo.")


async def cmd_alertas_tiempo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔔 Comprobando alertas (estricto)…")
    try:
        await maybe_send_weather_alert(context.application)
        await update.message.reply_text("✅ Hecho. Si había alerta, ya la envié.")
    except Exception:
        await update.message.reply_text("❌ Error comprobando alertas.")


async def cmd_alertas_sensible(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔔 Comprobando alertas (modo sensible)…")
    try:
        chat_id = int(CHAT_ID)
        lat, lon, city = get_active_location(chat_id)
        data = open_meteo_hourly(lat, lon, DEFAULT_TZ)
        alert = compute_weather_alert(city, data, gust_kmh=45.0, rain_prob_pct=60.0, rain_mm_h=2.0, label="sensible")
        if alert:
            _, msg = alert
            await context.application.bot.send_message(chat_id=chat_id, text=msg)
        else:
            await update.message.reply_text("✅ No hay alerta sensible ahora mismo.")
    except Exception:
        await update.message.reply_text("❌ Error comprobando alertas sensibles.")


async def cmd_trenes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msgs = []
    try:
        if RODALIES_R1_RSS:
            items = parse_rss_items(RODALIES_R1_RSS, limit=1)
            if items:
                it = items[0]
                msgs.append(f"🚆 R1 último: {it['title']}\n{it['link']}".strip())
            else:
                msgs.append("🚆 R1: RSS vacío/no parseable")
        else:
            msgs.append("🚆 R1: (sin RSS configurado)")
    except Exception:
        msgs.append("🚆 R1: error leyendo RSS")

    try:
        if RODALIES_RG1_RSS:
            items = parse_rss_items(RODALIES_RG1_RSS, limit=1)
            if items:
                it = items[0]
                msgs.append(f"🚆 RG1 último: {it['title']}\n{it['link']}".strip())
            else:
                msgs.append("🚆 RG1: RSS vacío/no parseable")
        else:
            msgs.append("🚆 RG1: (sin RSS configurado)")
    except Exception:
        msgs.append("🚆 RG1: error leyendo RSS")

    await update.message.reply_text("\n\n".join(msgs))


async def cmd_alertas_trenes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔔 Comprobando trenes ahora…")
    try:
        await check_trains_and_air(context.application)
        await update.message.reply_text("✅ Hecho. Si había incidencia seria nueva, ya la envié.")
    except Exception:
        await update.message.reply_text("❌ Error comprobando trenes.")


async def cmd_aire(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not AIR_QUALITY_URL:
        await update.message.reply_text("🌫️ AIR_QUALITY_URL no está configurada.")
        return
    try:
        r = requests.get(AIR_QUALITY_URL, timeout=20)
        r.raise_for_status()
        data = r.json()
        await update.message.reply_text(f"{summarize_air_quality(data)}\nℹ️ Fuente: AIR_QUALITY_URL")
    except Exception:
        await update.message.reply_text("❌ No he podido leer la calidad del aire ahora mismo.")


async def cmd_alertas_aire(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔔 Comprobando aire ahora…")
    try:
        await check_trains_and_air(context.application)
        await update.message.reply_text("✅ Hecho. Si hubo cambio relevante (y pasó cooldown), lo envié.")
    except Exception:
        await update.message.reply_text("❌ Error comprobando aire.")


async def cmd_aemet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        items = parse_rss_items(AEMET_ALERTS_RSS, limit=3)
        if not items:
            await update.message.reply_text("⚠️ AEMET: no hay items en el RSS ahora.")
            return
        lines = ["⚠️ AEMET — Avisos Catalunya (últimos)"]
        for it in items:
            lines.append(f"• {it['title']}\n{it['link']}".strip())
        lines.append("\nPágina oficial avisos: https://www.aemet.es/ca/eltiempo/prediccion/avisos?k=cat")
        await update.message.reply_text("\n\n".join(lines))
    except Exception:
        await update.message.reply_text("❌ No he podido leer el RSS de AEMET ahora mismo.")


async def cmd_alertas_aemet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔔 Comprobando AEMET ahora…")
    try:
        await check_aemet(context.application)
        await update.message.reply_text("✅ Hecho. Si había aviso nuevo, ya lo envié.")
    except Exception:
        await update.message.reply_text("❌ Error comprobando AEMET.")


async def cmd_mapa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lat, lon, label = get_active_location(update.effective_chat.id)
    # Enlace rápido (sin API) a OSM y Google Maps
    osm = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=13/{lat}/{lon}"
    gmaps = f"https://www.google.com/maps?q={lat},{lon}"
    await update.message.reply_text(
        f"🗺️ Mapa — {label}\n"
        f"• OpenStreetMap: {osm}\n"
        f"• Google Maps: {gmaps}\n"
        f"\n(Si quieres, puedo hacer que además envíe el pin como ubicación.)"
    )


# =====================
# Lifecycle
# =====================
async def post_init(app: Application):
    # Arranque de loops
    app.create_task(weather_loop(app))
    app.create_task(trains_air_loop(app))
    app.create_task(aemet_loop(app))

    # Ping de arranque
    try:
        await app.bot.send_message(chat_id=int(CHAT_ID), text="✅ Bot reiniciado y operativo (clima + trenes + aire + AEMET).")
    except Exception:
        pass


def main():
    must_env()

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler("id", cmd_id))

    app.add_handler(CommandHandler("ciudad", cmd_ciudad))
    app.add_handler(CommandHandler("tiempo", cmd_tiempo))
    app.add_handler(CommandHandler("alertas_tiempo", cmd_alertas_tiempo))
    app.add_handler(CommandHandler("alertas_sensible", cmd_alertas_sensible))

    app.add_handler(CommandHandler("trenes", cmd_trenes))
    app.add_handler(CommandHandler("alertas_trenes", cmd_alertas_trenes))

    app.add_handler(CommandHandler("aire", cmd_aire))
    app.add_handler(CommandHandler("alertas_aire", cmd_alertas_aire))

    app.add_handler(CommandHandler("aemet", cmd_aemet))
    app.add_handler(CommandHandler("alertas_aemet", cmd_alertas_aemet))

    app.add_handler(CommandHandler("mapa", cmd_mapa))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
