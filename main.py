import os
import logging

from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["BOT_TOKEN"]
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL")

app = FastAPI()

telegram_app = Application.builder().token(TOKEN).build()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot de gravação online!\n\n"
        "Comandos disponíveis:\n"
        "/start - Iniciar\n"
        "/status - Ver status\n"
        "/gravar - Iniciar gravação\n"
        "/parar - Parar gravação"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🟢 Bot online!\n"
        "🎥 Gravador: aguardando configuração"
    )


async def gravar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎥 Comando recebido!\n"
        "O módulo de gravação ainda será configurado."
    )


async def parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⏹️ Comando recebido!\n"
        "Nenhuma gravação está ativa."
    )


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("status", status))
telegram_app.add_handler(CommandHandler("gravar", gravar))
telegram_app.add_handler(CommandHandler("parar", parar))


@app.on_event("startup")
async def startup():
    await telegram_app.initialize()
    await telegram_app.start()

    if RENDER_URL:
        webhook_url = f"{RENDER_URL}/telegram/webhook"
        await telegram_app.bot.set_webhook(webhook_url)
        logging.info("Webhook configurado: %s", webhook_url)


@app.on_event("shutdown")
async def shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()


@app.get("/")
async def home():
    return {
        "status": "online",
        "service": "Telegram Live Recorder Bot"
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()

    update = Update.de_json(
        data,
        telegram_app.bot
    )

    await telegram_app.process_update(update)

    return {"ok": True}
