import os
import asyncio
from telegram import Bot

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

async def main():
    if not TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN")
    if not CHAT_ID:
        raise RuntimeError("Falta CHAT_ID")

    bot = Bot(token=TOKEN)
    await bot.send_message(
        chat_id=CHAT_ID,
        text="✅ Bot iniciado correctamente en Railway"
    )

    # Mantener el proceso vivo
    while True:
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
