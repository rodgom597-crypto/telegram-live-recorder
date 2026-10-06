import os
import asyncio
import logging
import signal
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
        "Comandos:\n\n"
        "/gravar usuario - iniciar gravação\n"
        "/parar - parar gravação\n"
        "/status - verificar status"
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


async def finalizar_arquivo(
    username,
    output_dir,
    chat_id
):

    try:

        files = list(
            output_dir.glob(
                f"{username}.*"
            )
        )

        files = [
            f for f in files
            if f.is_file()
        ]

        part_files = [
            f for f in files
            if f.name.endswith(".part")
        ]

        if part_files:

            part_file = part_files[0]

            final_file = (
                output_dir /
                f"{username}.mp4"
            )

            logging.info(
                "Finalizando arquivo parcial..."
            )

            ffmpeg = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                str(part_file),
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                str(final_file),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await ffmpeg.communicate()

            if ffmpeg.returncode == 0:

                try:
                    part_file.unlink()
                except Exception:
                    pass

            else:

                logging.error(
                    stderr.decode(
                        "utf-8",
                        errors="ignore"
                    )
                )

        final_files = list(
            output_dir.glob(
                f"{username}.*"
            )
        )

        final_files = [
            f for f in final_files
            if f.is_file()
            and not f.name.endswith(".part")
        ]

        if not final_files:

            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ A gravação de @{username} "
                    f"terminou, mas o arquivo não foi encontrado."
                )
            )

            return

        final_file = max(
            final_files,
            key=lambda f: f.stat().st_mtime
        )

        size = (
            final_file.stat().st_size
            / (1024 * 1024)
        )

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                "✅ Gravação finalizada!\n\n"
                f"👤 @{username}\n"
                f"📦 Tamanho: {size:.1f} MB\n"
                "📤 Enviando vídeo..."
            )
        )

        # Envia o vídeo para o Telegram
        with open(
            final_file,
            "rb"
        ) as video:

            await telegram_app.bot.send_video(
                chat_id=chat_id,
                video=video,
                caption=(
                    f"🎥 @{username}\n"
                    f"📦 {size:.1f} MB"
                ),
                supports_streaming=True
            )

        logging.info(
            "Vídeo enviado para o Telegram: %s",
            final_file
        )

        # Apaga o arquivo do Render depois do envio
        try:

            final_file.unlink()

            logging.info(
                "Arquivo removido do Render."
            )

        except Exception as e:

            logging.warning(
                "Não foi possível apagar arquivo: %s",
                e
            )

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text="🗑️ Arquivo removido do armazenamento temporário."

        )

    except Exception as e:

        logging.exception(
            "Erro ao finalizar/enviar arquivo"
        )

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"❌ Erro ao enviar o vídeo "
                f"de @{username}:\n\n{e}"
            )
        )


async def record_live(
    username,
    chat_id
):

    output_dir = Path(
        "/tmp/recordings"
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    filename = (
        output_dir /
        f"{username}.%(ext)s"
    )

    url = (
        f"https://www.tiktok.com/"
        f"@{username}/live"
    )

    process = None

    try:

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔎 Procurando a live de "
                f"@{username}..."
            )
        )

        command = [
            "yt-dlp",
            "--newline",
            "--no-warnings",
            "-f",
            "best",
            "--no-part",
            "-o",
            str(filename),
            url
        ]

        logging.info(
            "Comando: %s",
            " ".join(command)
        )

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True
        )

        recordings[username] = {
            "process": process,
            "file": str(filename),
            "stopping": False,
            "chat_id": chat_id
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

        await process.wait()

        recordings.pop(
            username,
            None
        )

        await finalizar_arquivo(
            username,
            output_dir,
            chat_id
        )

    except asyncio.CancelledError:

        if process:

            try:

                if process.returncode is None:

                    os.killpg(
                        process.pid,
                        signal.SIGKILL
                    )

            except Exception:
                pass

        recordings.pop(
            username,
            None
        )

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
                f"❌ Erro ao gravar "
                f"@{username}:\n\n{e}"
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

        data["stopping"] = True

        if process.returncode is None:

            await update.message.reply_text(
                f"⏹️ Parando @{username}..."
            )

            try:

                os.killpg(
                    process.pid,
                    signal.SIGINT
                )

                try:

                    await asyncio.wait_for(
                        process.wait(),
                        timeout=15
                    )

                except asyncio.TimeoutError:

                    logging.warning(
                        "Forçando encerramento..."
                    )

                    try:

                        os.killpg(
                            process.pid,
                            signal.SIGKILL
                        )

                    except Exception:
                        pass

            except Exception as e:

                logging.exception(
                    "Erro ao parar gravação"
                )

                await update.message.reply_text(
                    f"❌ Erro ao parar "
                    f"@{username}:\n{e}"
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

            try:

                os.killpg(
                    process.pid,
                    signal.SIGKILL
                )

            except Exception:
                pass

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
