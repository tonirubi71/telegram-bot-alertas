import os
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

RODALIES_R1_RSS = os.getenv("RODALIES_R1_RSS")
RODALIES_RG1_RSS = os.getenv("RODALIES_RG1_RSS")
AIR_QUALITY_URL = os.getenv("AIR_QUALITY_URL")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bot activo.\n\n"
        "Comandos disponibles:\n"
        "/estado – comprobar estado del bot\n"
        "/id – ver tu chat_id\n"
        "/fuentes – comprobar fuentes activas"
    )

async def estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🟢 Estado: OK. (Servidor conectado y bot operativo)")

async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Tu chat_id es:\n{update.effective_chat.id}")

async def fuentes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensajes = []

    try:
        requests.get(RODALIES_R1_RSS, timeout=10)
        mensajes.append("🚆 R1: OK")
    except:
        mensajes.append("🚆 R1: ERROR")

    try:
        requests.get(RODALIES_RG1_RSS, timeout=10)
        mensajes.append("🚆 RG1: OK")
    except:
        mensajes.append("🚆 RG1: ERROR")

    try:
        requests.get(AIR_QUALITY_URL, timeout=10)
        mensajes.append("🌫️ Calidad del aire: OK")
    except:
        mensajes.append("🌫️ Calidad del aire: ERROR")

    await update.message.reply_text("🧪 Prueba de fuentes completada\n\n" + "\n".join(mensajes))

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("estado", estado))
    app.add_handler(CommandHandler("id", chat_id))
    app.add_handler(CommandHandler("fuentes", fuentes))

    print("🤖 Bot iniciado correctamente")
    app.run_polling()

if __name__ == "__main__":
    main()
