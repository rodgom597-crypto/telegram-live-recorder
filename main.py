import os
import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from tiktok_live_recorder import (
    TikTokLiveRecorder,
    StreamOfflineError,
)

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["BOT_TOKEN"]

app = FastAPI()

telegram_app = Application.builder().token(TOKEN).build()

recordings = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 TikTok Live Recorder\n\n"
        "/gravar usuario - iniciar gravação\n"
        "/parar - parar gravação\n"
        "/status - verificar status"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if recordings:
        lista = "\n".join(
            f"🔴 @{u}" for u in recordings
        )
        await update.message.reply_text(
            f"Gravações ativas:\n\n{lista}"
        )
    else:
        await update.message.reply_text(
            "🟢 Nenhuma gravação ativa."
        )


async def record_live(username, chat_id):
    try:
        output_dir = Path("/tmp/recordings")
        output_dir.mkdir(parents=True, exist_ok=True)

        filename = output_dir / f"{username}.mp4"

        recorder = TikTokLiveRecorder(username)

        recordings[username] = {
            "task": asyncio.current_task(),
            "file": str(filename),
        }

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=f"🔴 Live encontrada!\n\n"
                 f"🎥 Iniciando gravação de @{username}"
        )

        await asyncio.to_thread(
            recorder.record,
            out_file=str(filename),
            quality="origin",
        )

        if filename.exists():
            size = filename.stat().st_size / (1024 * 1024)

            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ Gravação finalizada!\n\n"
                    f"👤 @{username}\n"
                    f"📦 Tamanho: {size:.1f} MB"
                ),
            )
        else:
            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ A gravação de @{username} terminou, "
                     "mas o arquivo não foi encontrado."
            )

    except StreamOfflineError:
        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=f"⚫ @{username} não está ao vivo."
        )

    except Exception as e:
        logging.exception("Erro na gravação")

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Erro gravando @{username}:\n{e}"
        )

    finally:
        recordings.pop(username, None)


async def gravar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Use:\n/gravar cmlykimberly"
        )
        return

    username = context.args[0].replace("@", "").strip()

    if username in recordings:
        await update.message.reply_text(
            f"⚠️ @{username} já está sendo gravado."
        )
        return

    await update.message.reply_text(
        f"🔎 Verificando @{username}..."
    )

    asyncio.create_task(
        record_live(
            username,
            update.effective_chat.id
        )
    )


async def parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not recordings:
        await update.message.reply_text(
            "🟢 Não existe nenhuma gravação ativa."
        )
        return

    for username, data in list(recordings.items()):
        task = data["task"]

        task.cancel()

        await update.message.reply_text(
            f"⏹️ Solicitação para parar @{username} enviada."
        )


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("status", status))
telegram_app.add_handler(CommandHandler("gravar", gravar))
telegram_app.add_handler(CommandHandler("parar", parar))


@app.on_event("startup")
async def startup():
    await telegram_app.initialize()
    await telegram_app.start()

    render_url = os.environ.get("RENDER_EXTERNAL_URL")

    if render_url:
        await telegram_app.bot.set_webhook(
            f"{render_url}/telegram/webhook"
        )

        logging.info("Webhook configurado.")


@app.on_event("shutdown")
async def shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()


@app.get("/")
async def home():
    return {
        "status": "online",
        "service": "TikTok Live Recorder"
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
