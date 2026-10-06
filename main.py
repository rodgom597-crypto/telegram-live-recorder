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
# TELEGRAM
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
# LOCALIZAR ARQUIVO
# =========================================================

def procurar_arquivo(username):

    if not OUTPUT_DIR.exists():
        return None

    arquivos = list(
        OUTPUT_DIR.iterdir()
    )

    logging.info(
        "Arquivos existentes: %s",
        [arquivo.name for arquivo in arquivos]
    )

    candidatos = []

    extensoes = {
        ".mp4",
        ".flv",
        ".mkv",
        ".ts",
        ".webm",
        ".m4v",
        ".mov"
    }

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

        if arquivo.suffix.lower() not in extensoes:
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
# FINALIZAR E ENVIAR
# =========================================================

async def finalizar_arquivo(
    username,
    chat_id
):

    try:

        logging.info(
            "Esperando arquivo de @%s...",
            username
        )

        await asyncio.sleep(3)

        arquivo = procurar_arquivo(
            username
        )

        if not arquivo:

            logging.error(
                "Arquivo não encontrado para @%s",
                username
            )

            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ A gravação de @{username} "
                    "terminou, mas nenhum arquivo foi encontrado."
                )
            )

            return

        tamanho = arquivo.stat().st_size

        tamanho_mb = (
            tamanho /
            (1024 * 1024)
        )

        logging.info(
            "Arquivo encontrado: %s",
            arquivo
        )

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                "✅ Gravação finalizada!\n\n"
                f"👤 @{username}\n"
                f"📁 {arquivo.name}\n"
                f"📦 {tamanho_mb:.1f} MB\n\n"
                "📤 Enviando para o Telegram..."
            )
        )

        enviado = False

        # -------------------------------------------------
        # TENTA ENVIAR COMO VÍDEO
        # -------------------------------------------------

        try:

            with open(
                arquivo,
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
                "Vídeo enviado como vídeo."
            )

        except Exception as erro:

            logging.warning(
                "Falha ao enviar como vídeo: %s",
                erro
            )

        # -------------------------------------------------
        # SE FALHAR, ENVIA COMO DOCUMENTO
        # -------------------------------------------------

        if not enviado:

            try:

                with open(
                    arquivo,
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

            except Exception as erro:

                logging.exception(
                    "Falha ao enviar arquivo."
                )

                await telegram_app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "❌ Não consegui enviar o vídeo "
                        "para o Telegram.\n\n"
                        f"Erro: {erro}"
                    )
                )

        # -------------------------------------------------
        # APAGA APENAS DEPOIS DO ENVIO
        # -------------------------------------------------

        if enviado:

            try:

                arquivo.unlink()

                logging.info(
                    "Arquivo removido do Render."
                )

            except Exception as erro:

                logging.warning(
                    "Erro ao apagar arquivo: %s",
                    erro
                )

            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    "✅ Vídeo enviado com sucesso! 🎥"
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
# GRAVAÇÃO
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

        # -------------------------------------------------
        # NOME DO ARQUIVO
        # -------------------------------------------------

        filename = (
            OUTPUT_DIR /
            f"{username}.%(ext)s"
        )

        # -------------------------------------------------
        # YT-DLP
        #
        # FLV é priorizado quando disponível.
        # HLS continua como fallback.
        # -------------------------------------------------

        command = [
            "yt-dlp",

            "--newline",

            "--no-warnings",

            "--no-color",

            # Tenta FLV primeiro.
            # Se não existir, usa o melhor formato.
            "-f",
            "best[protocol*=http][ext=flv]/best",

            # Tenta novamente segmentos HLS
            "--fragment-retries",
            "20",

            "--retries",
            "10",

            "--retry-sleep",
            "2",

            # Mantém o arquivo final.
            "--no-part",

            # Nome do arquivo.
            "-o",
            str(filename),

            url
        ]

        logging.info(
            "========================================"
        )

        logging.info(
            "INICIANDO GRAVAÇÃO: @%s",
            username
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

            # Cria grupo de processos.
            # Assim conseguimos parar yt-dlp + FFmpeg.
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
                f"👤 @{username}"
            )
        )

        # -------------------------------------------------
        # LER LOG DO YT-DLP
        # -------------------------------------------------

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
            "yt-dlp terminou."
        )

        logging.info(
            "Código de saída: %s",
            processo.returncode
        )

        recordings.pop(
            username,
            None
        )

        # -------------------------------------------------
        # PROCESSAR ARQUIVO
        # -------------------------------------------------

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

                # Primeiro tenta finalizar
                # de forma normal.
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
                        "yt-dlp não encerrou "
                        "em 20 segundos."
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
                    "Erro ao parar."
                )

                await update.message.reply_text(
                    f"❌ Erro ao parar "
                    f"@{username}:\n\n{erro}"
                )


# =========================================================
# TELEGRAM HANDLERS
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
