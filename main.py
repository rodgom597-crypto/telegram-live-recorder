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

# =========================================================
# CONFIGURAÇÃO
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

TOKEN = os.environ["BOT_TOKEN"]

OUTPUT_DIR = Path("/tmp/recordings")

app = FastAPI()

telegram_app = Application.builder().token(TOKEN).build()

recordings = {}


# =========================================================
# COMANDO /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "🤖 TikTok Live Recorder\n\n"
        "Comandos:\n\n"
        "/gravar usuario - iniciar gravação\n"
        "/parar - parar gravação\n"
        "/status - verificar status"
    )


# =========================================================
# COMANDO /STATUS
# =========================================================

async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

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


# =========================================================
# LOCALIZAR ARQUIVO FLV
# =========================================================

def procurar_flv(username):

    if not OUTPUT_DIR.exists():
        return None

    arquivos = list(
        OUTPUT_DIR.iterdir()
    )

    logging.info(
        "Arquivos encontrados: %s",
        [arquivo.name for arquivo in arquivos]
    )

    candidatos = []

    for arquivo in arquivos:

        if not arquivo.is_file():
            continue

        nome = arquivo.name.lower()

        if username.lower() not in nome:
            continue

        if nome.endswith(".part"):
            continue

        if nome.endswith(".ytdl"):
            continue

        if arquivo.suffix.lower() != ".flv":
            continue

        try:

            tamanho = arquivo.stat().st_size

            if tamanho > 0:
                candidatos.append(arquivo)

        except Exception:
            pass

    if not candidatos:
        return None

    return max(
        candidatos,
        key=lambda arquivo: arquivo.stat().st_mtime
    )


# =========================================================
# CONVERTER FLV -> MP4
# =========================================================

async def converter_para_mp4(
    flv_file,
    mp4_file
):

    logging.info(
        "Convertendo FLV para MP4..."
    )

    # -----------------------------------------------------
    # PRIMEIRA TENTATIVA:
    # Apenas troca o container.
    # É muito mais rápido porque não recodifica.
    # -----------------------------------------------------

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(flv_file),
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        str(mp4_file)
    ]

    logging.info(
        "FFmpeg: %s",
        " ".join(command)
    )

    processo = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await processo.communicate()

    if processo.returncode == 0:

        logging.info(
            "Conversão para MP4 concluída."
        )

        return True

    logging.warning(
        "Conversão sem recodificação falhou."
    )

    logging.warning(
        stderr.decode(
            "utf-8",
            errors="ignore"
        )
    )

    # -----------------------------------------------------
    # SEGUNDA TENTATIVA:
    # Se o copy não funcionar, recodifica.
    # -----------------------------------------------------

    logging.info(
        "Tentando conversão com recodificação..."
    )

    if mp4_file.exists():

        try:
            mp4_file.unlink()
        except Exception:
            pass

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(flv_file),

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        "23",

        "-c:a",
        "aac",

        "-b:a",
        "128k",

        "-movflags",
        "+faststart",

        str(mp4_file)
    ]

    logging.info(
        "FFmpeg recodificação: %s",
        " ".join(command)
    )

    processo = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await processo.communicate()

    if processo.returncode == 0:

        logging.info(
            "Recodificação concluída."
        )

        return True

    logging.error(
        "FFmpeg falhou definitivamente."
    )

    logging.error(
        stderr.decode(
            "utf-8",
            errors="ignore"
        )
    )

    return False


# =========================================================
# FINALIZAR GRAVAÇÃO
# =========================================================

async def finalizar_arquivo(
    username,
    chat_id
):

    try:

        logging.info(
            "Procurando FLV de @%s...",
            username
        )

        # Dá alguns segundos para o arquivo terminar
        await asyncio.sleep(3)

        flv_file = procurar_flv(
            username
        )

        if not flv_file:

            logging.error(
                "FLV não encontrado."
            )

            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ A gravação de @{username} "
                    "terminou, mas o FLV não foi encontrado."
                )
            )

            return

        tamanho_flv = (
            flv_file.stat().st_size
            / (1024 * 1024)
        )

        logging.info(
            "FLV encontrado: %s",
            flv_file
        )

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                "✅ Gravação finalizada!\n\n"
                f"👤 @{username}\n"
                f"📁 {flv_file.name}\n"
                f"📦 {tamanho_flv:.1f} MB\n\n"
                "🔄 Convertendo para MP4..."
            )
        )

        # -------------------------------------------------
        # NOME DO MP4
        # -------------------------------------------------

        mp4_file = (
            OUTPUT_DIR /
            f"{username}.mp4"
        )

        # Se existir algum MP4 antigo, remove
        if mp4_file.exists():

            try:
                mp4_file.unlink()
            except Exception:
                pass

        # -------------------------------------------------
        # CONVERTER
        # -------------------------------------------------

        convertido = await converter_para_mp4(
            flv_file,
            mp4_file
        )

        if not convertido:

            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❌ Não foi possível converter "
                    f"@{username} para MP4."
                )
            )

            return

        # -------------------------------------------------
        # VERIFICAR MP4
        # -------------------------------------------------

        if not mp4_file.exists():

            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ O FFmpeg informou que terminou, "
                    "mas o MP4 não foi encontrado."
                )
            )

            return

        tamanho_mp4 = (
            mp4_file.stat().st_size
            / (1024 * 1024)
        )

        logging.info(
            "MP4 criado: %s",
            mp4_file
        )

        logging.info(
            "Tamanho: %.2f MB",
            tamanho_mp4
        )

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                "✅ MP4 pronto!\n\n"
                f"👤 @{username}\n"
                f"📦 {tamanho_mp4:.1f} MB\n\n"
                "📤 Enviando para o Telegram..."
            )
        )

        enviado = False

        # -------------------------------------------------
        # ENVIAR COMO VÍDEO
        # -------------------------------------------------

        try:

            with open(
                mp4_file,
                "rb"
            ) as video:

                await telegram_app.bot.send_video(
                    chat_id=chat_id,
                    video=video,
                    caption=(
                        f"🎥 @{username}\n"
                        f"📦 {tamanho_mp4:.1f} MB"
                    ),
                    supports_streaming=True
                )

            enviado = True

            logging.info(
                "MP4 enviado como vídeo."
            )

        except Exception as erro:

            logging.warning(
                "Falha no envio como vídeo: %s",
                erro
            )

        # -------------------------------------------------
        # FALLBACK: DOCUMENTO
        # -------------------------------------------------

        if not enviado:

            try:

                with open(
                    mp4_file,
                    "rb"
                ) as documento:

                    await telegram_app.bot.send_document(
                        chat_id=chat_id,
                        document=documento,
                        caption=(
                            f"🎥 Gravação de @{username}\n"
                            f"📦 {tamanho_mp4:.1f} MB"
                        )
                    )

                enviado = True

                logging.info(
                    "MP4 enviado como documento."
                )

            except Exception as erro:

                logging.exception(
                    "Falha ao enviar MP4."
                )

                await telegram_app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "❌ Não consegui enviar "
                        "o MP4 para o Telegram.\n\n"
                        f"Erro: {erro}"
                    )
                )

        # -------------------------------------------------
        # APAGAR ARQUIVOS
        # -------------------------------------------------

        if enviado:

            try:

                if flv_file.exists():
                    flv_file.unlink()

                logging.info(
                    "FLV removido."
                )

            except Exception as erro:

                logging.warning(
                    "Erro ao apagar FLV: %s",
                    erro
                )

            try:

                if mp4_file.exists():
                    mp4_file.unlink()

                logging.info(
                    "MP4 removido."
                )

            except Exception as erro:

                logging.warning(
                    "Erro ao apagar MP4: %s",
                    erro
                )

            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    "✅ Vídeo enviado com sucesso! 🎥\n"
                    "🗑️ Arquivos temporários removidos."
                )
            )

    except Exception as erro:

        logging.exception(
            "Erro ao finalizar gravação."
        )

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"❌ Erro ao finalizar "
                f"@{username}:\n\n{erro}"
            )
        )


# =========================================================
# GRAVAR
# =========================================================

async def record_live(
    username,
    chat_id
):

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    url = (
        f"https://www.tiktok.com/"
        f"@{username}/live"
    )

    processo = None

    try:

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔎 Procurando a live de "
                f"@{username}..."
            )
        )

        filename = (
            OUTPUT_DIR /
            f"{username}.%(ext)s"
        )

        # =================================================
        # FLV PRIMEIRO
        # =================================================

        command = [
            "yt-dlp",

            "--newline",

            "--no-warnings",

            "--no-color",

            # FLV primeiro
            "-f",
            "best[ext=flv]/best",

            # Tentativas
            "--fragment-retries",
            "20",

            "--retries",
            "10",

            "--retry-sleep",
            "2",

            # Não criar .part
            "--no-part",

            # Arquivo
            "-o",
            str(filename),

            url
        ]

        logging.info(
            "========================================"
        )

        logging.info(
            "INICIANDO GRAVAÇÃO"
        )

        logging.info(
            "Usuário: @%s",
            username
        )

        logging.info(
            "Formato: FLV"
        )

        logging.info(
            "Comando: %s",
            " ".join(command)
        )

        logging.info(
            "========================================"
        )

        processo = await asyncio.create_subprocess_exec(
            *command,

            stdout=asyncio.subprocess.PIPE,

            stderr=asyncio.subprocess.STDOUT,

            start_new_session=True
        )

        recordings[username] = {
            "process": processo,
            "chat_id": chat_id
        }

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                "🔴 Gravação iniciada!\n\n"
                f"👤 @{username}\n"
                "🎞️ Formato: FLV"
            )
        )

        # =================================================
        # LER LOG
        # =================================================

        while True:

            linha = await processo.stdout.readline()

            if not linha:
                break

            texto = linha.decode(
                "utf-8",
                errors="ignore"
            ).strip()

            if texto:

                logging.info(
                    "[%s] %s",
                    username,
                    texto
                )

        await processo.wait()

        logging.info(
            "========================================"
        )

        logging.info(
            "YT-DLP FINALIZOU"
        )

        logging.info(
            "Usuário: @%s",
            username
        )

        logging.info(
            "Código: %s",
            processo.returncode
        )

        logging.info(
            "========================================"
        )

        recordings.pop(
            username,
            None
        )

        # =================================================
        # FINALIZAR
        # =================================================

        await finalizar_arquivo(
            username,
            chat_id
        )

    except asyncio.CancelledError:

        logging.warning(
            "Gravação cancelada: @%s",
            username
        )

        if processo:

            try:

                if processo.returncode is None:

                    os.killpg(
                        processo.pid,
                        signal.SIGKILL
                    )

            except Exception:
                pass

        recordings.pop(
            username,
            None
        )

        raise

    except Exception as erro:

        logging.exception(
            "Erro na gravação."
        )

        recordings.pop(
            username,
            None
        )

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"❌ Erro ao gravar "
                f"@{username}:\n\n{erro}"
            )
        )


# =========================================================
# /GRAVAR
# =========================================================

async def gravar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.args:

        await update.message.reply_text(
            "Use:\n\n"
            "/gravar joaobfilhoo1"
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
            "já está sendo gravado."
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


# =========================================================
# /PARAR
# =========================================================

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

        processo = data["process"]

        if processo.returncode is None:

            await update.message.reply_text(
                f"⏹️ Parando @{username}..."
            )

            try:

                # Parar de forma normal
                os.killpg(
                    processo.pid,
                    signal.SIGINT
                )

                try:

                    await asyncio.wait_for(
                        processo.wait(),
                        timeout=20
                    )

                except asyncio.TimeoutError:

                    logging.warning(
                        "Processo não encerrou. "
                        "Forçando encerramento."
                    )

                    try:

                        os.killpg(
                            processo.pid,
                            signal.SIGKILL
                        )

                    except Exception:
                        pass

            except Exception as erro:

                logging.exception(
                    "Erro ao parar gravação."
                )

                await update.message.reply_text(
                    f"❌ Erro ao parar "
                    f"@{username}:\n\n{erro}"
                )


# =========================================================
# HANDLERS
# =========================================================

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


# =========================================================
# STARTUP
# =========================================================

@app.on_event("startup")
async def startup():

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    await telegram_app.initialize()

    await telegram_app.start()

    render_url = os.environ.get(
        "RENDER_EXTERNAL_URL"
    )

    if render_url:

        webhook_url = (
            f"{render_url}/telegram/webhook"
        )

        await telegram_app.bot.set_webhook(
            webhook_url
        )

        logging.info(
            "Webhook configurado: %s",
            webhook_url
        )


# =========================================================
# SHUTDOWN
# =========================================================

@app.on_event("shutdown")
async def shutdown():

    logging.info(
        "Encerrando serviço..."
    )

    for username, data in list(
        recordings.items()
    ):

        processo = data["process"]

        if processo.returncode is None:

            try:

                os.killpg(
                    processo.pid,
                    signal.SIGKILL
                )

            except Exception:
                pass

    await telegram_app.stop()

    await telegram_app.shutdown()


# =========================================================
# ROTAS
# =========================================================

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
