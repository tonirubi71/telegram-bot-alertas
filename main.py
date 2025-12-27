import os
import asyncio
import logging
import requests
from telegram import Bot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("alertas-bot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

R1_SOURCE = os.getenv("RODALIES_R1_SOURCE")
RG1_SOURCE = os.getenv("RODALIES_RG1_SOURCE")
AIR_QUALITY_URL = os.getenv("AIR_QUALITY_URL")


def check_url(url: str) -> bool:
    try:
        r = requests.get(url, timeout=10)
        return r.status_code == 200
    except Exception:
        return False


async def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Faltan TELEGRAM_BOT_TOKEN o CHAT_ID")

    bot = Bot(token=TOKEN)

    results = {
        "R1": check_url(R1_SOURCE) if R1_SOURCE else False,
        "RG1": check_url(RG1_SOURCE) if RG1_SOURCE else False,
        "Aire": check_url(AIR_QUALITY_URL) if AIR_QUALITY_URL else False,
    }

    msg = (
        "🧪 *Prueba de fuentes completada*\n\n"
        f"🚆 R1: {'OK' if results['R1'] else 'ERROR'}\n"
        f"🚆 RG1: {'OK' if results['RG1'] else 'ERROR'}\n"
        f"🌫️ Calidad del aire: {'OK' if results['Aire'] else 'ERROR'}"
    )

    await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

    # Mantener vivo el proceso
    while True:
        await asyncio.sleep(300)


if __name__ == "__main__":
    asyncio.run(main())
