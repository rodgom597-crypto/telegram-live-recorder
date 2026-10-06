import os
import asyncio
import logging
import signal
from pathlib import Path

from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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


def procurar_arquivo(username, output_dir):
    """
    Procura arquivos relacionados ao usuário.
    Não depende de uma extensão específica.
    """

    todos = list(output_dir.iterdir())

    logging.info(
        "Arquivos existentes no diretório: %s",
        [f.name for f in todos]
    )

    candidatos = []

    extensoes_validas = {
        ".mp4",
        ".mkv",
        ".flv",
        ".ts",
        ".webm",
        ".m4v",
        ".mov",
        ".avi"
    }

    for arquivo in todos:

        if not arquivo.is_file():
            continue

        nome = arquivo.name.lower()

        # Ignora arquivos temporários
        if (
            nome.endswith(".part")
            or nome.endswith(".ytdl")
            or nome.endswith(".tmp")
        ):
            continue

        # O nome precisa conter o usuário
        if username.lower() not in nome:
            continue

        # Aceita extensões conhecidas
        if arquivo.suffix.lower() in extensoes_validas:

            try:
                tamanho = arquivo.stat().st_size

                if tamanho > 0:
                    candidatos.append(arquivo)

            except Exception:
                pass

    if not candidatos:
        return None

    # Pega o arquivo modificado mais recentemente
    return max(
        candidatos,
        key=lambda f: f.stat().st_mtime
    )


async def finalizar_arquivo(
    username,
    output_dir,
    chat_id
):

    try:

        logging.info(
            "Procurando arquivo final de @%s...",
            username
        )

        # Pequena espera para garantir que o sistema
        # terminou de gravar o arquivo
        await asyncio.sleep(2)

        final_file = procurar_arquivo(
            username,
            output_dir
        )

        if not final_file:

            logging.error(
                "Nenhum arquivo encontrado para @%s",
                username
            )

            arquivos = list(output_dir.iterdir())

            logging.error(
                "Conteúdo de %s: %s",
                output_dir,
                [f.name for f in arquivos]
            )

            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ A gravação de @{username} terminou, "
                    f"mas o arquivo não foi encontrado.\n\n"
                    f"📁 Pasta verificada:\n"
                    f"{output_dir}"
                )
            )

            return

        logging.info(
            "Arquivo encontrado: %s",
            final_file
        )

        tamanho_bytes = final_file.stat().st_size

        tamanho_mb = (
            tamanho_bytes /
            (1024 * 1024)
        )

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                "✅ Gravação finalizada!\n\n"
                f"👤 @{username}\n"
                f"📁 {final_file.name}\n"
                f"📦 {tamanho_mb:.1f} MB\n\n"
                "📤 Enviando vídeo para o Telegram..."
            )
        )

        enviado = False

        # Primeiro tenta enviar como vídeo
        try:

            with open(
                final_file,
                "rb"
            ) as video:

                await telegram_app.bot.send_video(
                    chat_id=chat_id,
                    video=video,
                    caption=(
                        f"🎥 Gravação de @{username}\n"
                        f"📦 {tamanho_mb:.1f} MB"
                    ),
                    supports_streaming=True
                )

            enviado = True

            logging.info(
                "Vídeo enviado com sucesso."
            )

        except Exception as erro_video:

            logging.warning(
                "send_video falhou: %s",
                erro_video
            )

            # Se vídeo falhar, tenta enviar como documento
            try:

                with open(
                    final_file,
                    "rb"
                ) as documento:

                    await telegram_app.bot.send_document(
                        chat_id=chat_id,
                        document=documento,
                        caption=(
                            f"🎥 Gravação de @{username}\n"
                            f"📦 {tamanho_mb:.1f} MB"
                        )
                    )

                enviado = True

                logging.info(
                    "Arquivo enviado como documento."
                )

            except Exception as erro_documento:

                logging.exception(
                    "Falha também no envio como documento."
                )

                await telegram_app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "❌ Não consegui enviar o arquivo "
                        "para o Telegram.\n\n"
                        f"Erro:\n{erro_documento}"
                    )
                )

        # Só apaga depois que realmente conseguiu enviar
        if enviado:

            try:

                final_file.unlink()

                logging.info(
                    "Arquivo removido do Render: %s",
                    final_file
                )

            except Exception as erro:

                logging.warning(
                    "Não consegui apagar arquivo: %s",
                    erro
                )

            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text="✅ Vídeo enviado com sucesso! 🎥"
            )

    except Exception as e:

        logging.exception(
            "Erro ao finalizar arquivo"
        )

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"❌ Erro ao finalizar @{username}:\n\n"
                f"{e}"
            )
        )


async def record_live(username, chat_id):

    output_dir = Path("/tmp/recordings")

    output_dir.mkdir(
        parents=True,
        exist_ok=True
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

        # Nome único para evitar conflito
        filename = (
            output_dir /
            f"{username}.%(ext)s"
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

            texto = line.decode(
                "utf-8",
                errors="ignore"
            ).strip()

            if texto:

                logging.info(
                    "[%s] %s",
                    username,
                    texto
                )

        await process.wait()

        logging.info(
            "yt-dlp terminou para @%s. Código: %s",
            username,
            process.returncode
        )

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

        if process.returncode is None:

            await update.message.reply_text(
                f"⏹️ Parando @{username}..."
            )

            try:

                # Envia CTRL+C para o grupo inteiro
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
                        "Processo não encerrou. "
                        "Forçando encerramento."
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
    CommandHandler("start", start)
)

telegram_app.add_handler(
    CommandHandler("status", status)
)

telegram_app.add_handler(
    CommandHandler("gravar", gravar)
)

telegram_app.add_handler(
    CommandHandler("parar", parar)
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
