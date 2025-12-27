import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("✅ Bot activo. Escribe /estado para ver el estado y /id para ver tu chat_id.")

async def estado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🟢 Estado: OK. (Servidor conectado y bot operativo)")

async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    await update.message.reply_text(
        f"🆔 chat_id: {chat.id}\n"
        f"👤 user_id: {user.id}\n"
        f"💬 tipo: {chat.type}"
    )

def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Falta la variable de entorno TELEGRAM_BOT_TOKEN")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("estado", estado))
    app.add_handler(CommandHandler("id", get_id))

    # Long polling (ideal para Railway)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
