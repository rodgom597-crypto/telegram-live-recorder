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

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["BOT_TOKEN"]

app = FastAPI()

telegram_app = Application.builder().token(TOKEN).build()

recordings = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 TikTok Live Recorder\n\n"
        "Comandos disponíveis:\n\n"
        "/gravar usuario - iniciar gravação\n"
        "/parar - parar gravação\n"
        "/status - verificar gravações"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not recordings:
        await update.message.reply_text(
            "🟢 Nenhuma gravação ativa."
        )
        return

    lista = "\n".join(
        f"🔴 @{username}"
        for username in recordings
    )

    await update.message.reply_text(
        f"📹 Gravações ativas:\n\n{lista}"
    )


async def record_live(username, chat_id):

    output_dir = Path("/tmp/recordings")
    output_dir.mkdir(parents=True, exist_ok=True)

    filename = output_dir / f"{username}.%(ext)s"

    url = f"https://www.tiktok.com/@{username}/live"

    try:

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔎 Procurando a live de @{username}..."
            )
        )

        command = [
            "yt-dlp",

            "--newline",

            "--no-warnings",

            "-f",
            "best",

            "-o",
            str(filename),

            url
        ]

        logging.info(
            "Iniciando gravação: %s",
            " ".join(command)
        )

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        recordings[username] = {
            "process": process,
            "file": str(filename)
        }

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔴 Gravação iniciada!\n\n"
                f"👤 @{username}"
            )
        )

        while True:

            line = await process.stdout.readline()

            if not line:
                break

            text = line.decode(
                "utf-8",
                errors="ignore"
            ).strip()

            if text:
                logging.info(
                    "[%s] %s",
                    username,
                    text
                )

        return_code = await process.wait()

        if username not in recordings:
            return

        recordings.pop(username, None)

        files = list(
            output_dir.glob(
                f"{username}.*"
            )
        )

        if files:

            file_path = files[0]

            size = file_path.stat().st_size / (
                1024 * 1024
            )

            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ Gravação finalizada!\n\n"
                    f"👤 @{username}\n"
                    f"📦 {size:.1f} MB"
                )
            )

        else:

            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ A gravação de @{username} "
                    f"terminou, mas nenhum arquivo "
                    f"foi encontrado."
                )
            )

    except asyncio.CancelledError:

        if username in recordings:
            data = recordings.pop(username)

            process = data["process"]

            if process.returncode is None:
                process.terminate()

        raise

    except Exception as e:

        logging.exception(
            "Erro na gravação"
        )

        recordings.pop(
            username,
            None
        )

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"❌ Erro ao gravar @{username}:\n\n"
                f"{e}"
            )
        )


async def gravar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.args:

        await update.message.reply_text(
            "Use:\n\n"
            "/gravar cmlykimberly"
        )

        return

    username = (
        context.args[0]
        .replace("@", "")
        .strip()
        .lower()
    )

    if username in recordings:

        await update.message.reply_text(
            f"⚠️ @{username} "
            f"já está sendo gravado."
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


async def parar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not recordings:

        await update.message.reply_text(
            "🟢 Não existe nenhuma "
            "gravação ativa."
        )

        return

    for username, data in list(
        recordings.items()
    ):

        process = data["process"]

        if process.returncode is None:

            process.terminate()

            await update.message.reply_text(
                f"⏹️ Parando @{username}..."
            )


telegram_app.add_handler(
    CommandHandler(
        "start",
        start
    )
)

telegram_app.add_handler(
    CommandHandler(
        "status",
        status
    )
)

telegram_app.add_handler(
    CommandHandler(
        "gravar",
        gravar
    )
)

telegram_app.add_handler(
    CommandHandler(
        "parar",
        parar
    )
)


@app.on_event("startup")
async def startup():

    await telegram_app.initialize()

    await telegram_app.start()

    render_url = os.environ.get(
        "RENDER_EXTERNAL_URL"
    )

    if render_url:

        await telegram_app.bot.set_webhook(
            f"{render_url}/telegram/webhook"
        )

        logging.info(
            "Webhook configurado."
        )


@app.on_event("shutdown")
async def shutdown():

    for username, data in list(
        recordings.items()
    ):

        process = data["process"]

        if process.returncode is None:

            process.terminate()

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

    return {
        "status": "ok"
    }


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request
):

    data = await request.json()

    update = Update.de_json(
        data,
        telegram_app.bot
    )

    await telegram_app.process_update(
        update
    )

    return {
        "ok": True
    }
