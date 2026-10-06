import os
import asyncio
import logging
import signal
from pathlib import Path
from datetime import datetime

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

RECONNECT_SECONDS = 10

app = FastAPI()

telegram_app = Application.builder().token(TOKEN).build()

recordings = {}


# =========================================================
# /START
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
# /STATUS
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

    mensagens = []

    for username, data in recordings.items():

        tentativas = data.get(
            "tentativas",
            1
        )

        inicio = data.get(
            "inicio"
        )

        if inicio:

            segundos = int(
                (datetime.now() - inicio).total_seconds()
            )

            horas = segundos // 3600
            minutos = (segundos % 3600) // 60
            segundos_restantes = segundos % 60

            tempo = (
                f"{horas:02d}:"
                f"{minutos:02d}:"
                f"{segundos_restantes:02d}"
            )

        else:

            tempo = "00:00:00"

        mensagens.append(
            f"🔴 @{username}\n"
            f"⏱️ Tempo: {tempo}\n"
            f"🔄 Tentativas: {tentativas}"
        )

    await update.message.reply_text(
        "📹 Gravações ativas:\n\n"
        + "\n\n".join(mensagens)
    )


# =========================================================
# PROCURAR ARQUIVOS DA GRAVAÇÃO
# =========================================================

def procurar_arquivos(username):

    if not OUTPUT_DIR.exists():
        return []

    arquivos = []

    for arquivo in OUTPUT_DIR.iterdir():

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
                arquivos.append(arquivo)

        except Exception:
            pass

    return sorted(
        arquivos,
        key=lambda arquivo: arquivo.stat().st_mtime
    )


# =========================================================
# CONVERTER / JUNTAR FLV -> MP4
# =========================================================

async def converter_para_mp4(
    arquivos,
    mp4_file
):

    if not arquivos:
        return False

    logging.info(
        "Preparando %s arquivo(s) FLV.",
        len(arquivos)
    )

    # -----------------------------------------------------
    # SE EXISTIR APENAS UM FLV
    # -----------------------------------------------------

    if len(arquivos) == 1:

        flv_file = arquivos[0]

        logging.info(
            "Convertendo arquivo único: %s",
            flv_file
        )

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

        processo = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await processo.communicate()

        if processo.returncode == 0:
            return True

        logging.warning(
            "Conversão copy falhou."
        )

        logging.warning(
            stderr.decode(
                "utf-8",
                errors="ignore"
            )
        )

        # -------------------------------------------------
        # TENTATIVA COM RECODIFICAÇÃO
        # -------------------------------------------------

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

        processo = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await processo.communicate()

        if processo.returncode == 0:
            return True

        logging.error(
            stderr.decode(
                "utf-8",
                errors="ignore"
            )
        )

        return False

    # =====================================================
    # VÁRIOS FLV
    # =====================================================

    logging.info(
        "Vários segmentos encontrados."
    )

    lista_file = (
        OUTPUT_DIR /
        "concat_list.txt"
    )

    try:

        with open(
            lista_file,
            "w",
            encoding="utf-8"
        ) as arquivo_lista:

            for arquivo in arquivos:

                caminho = str(
                    arquivo.resolve()
                ).replace(
                    "\\",
                    "/"
                )

                caminho = caminho.replace(
                    "'",
                    "'\\''"
                )

                arquivo_lista.write(
                    f"file '{caminho}'\n"
                )

        # -------------------------------------------------
        # TENTATIVA DE CONCATENAÇÃO SEM RECODIFICAR
        # -------------------------------------------------

        command = [
            "ffmpeg",
            "-y",

            "-f",
            "concat",

            "-safe",
            "0",

            "-i",
            str(lista_file),

            "-c",
            "copy",

            "-bsf:a",
            "aac_adtstoasc",

            str(mp4_file)
        ]

        logging.info(
            "Juntando segmentos..."
        )

        processo = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await processo.communicate()

        if processo.returncode == 0:

            logging.info(
                "Segmentos unidos com sucesso."
            )

            try:
                lista_file.unlink()
            except Exception:
                pass

            return True

        logging.warning(
            "Concatenação sem recodificação falhou."
        )

        logging.warning(
            stderr.decode(
                "utf-8",
                errors="ignore"
            )
        )

        # -------------------------------------------------
        # TENTATIVA COM RECODIFICAÇÃO
        # -------------------------------------------------

        if mp4_file.exists():

            try:
                mp4_file.unlink()
            except Exception:
                pass

        command = [
            "ffmpeg",
            "-y",

            "-f",
            "concat",

            "-safe",
            "0",

            "-i",
            str(lista_file),

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

        processo = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await processo.communicate()

        if processo.returncode == 0:

            logging.info(
                "Segmentos recodificados com sucesso."
            )

            try:
                lista_file.unlink()
            except Exception:
                pass

            return True

        logging.error(
            "Falha definitiva na conversão."
        )

        logging.error(
            stderr.decode(
                "utf-8",
                errors="ignore"
            )
        )

        return False

    except Exception as erro:

        logging.exception(
            "Erro ao montar lista de segmentos."
        )

        return False


# =========================================================
# FINALIZAR GRAVAÇÃO
# =========================================================

async def finalizar_gravacao(
    username,
    chat_id
):

    try:

        await asyncio.sleep(3)

        arquivos = procurar_arquivos(
            username
        )

        if not arquivos:

            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ A gravação de @{username} "
                    "terminou, mas nenhum arquivo foi encontrado."
                )
            )

            return

        tamanho_total = sum(
            arquivo.stat().st_size
            for arquivo in arquivos
        )

        tamanho_total_mb = (
            tamanho_total /
            (1024 * 1024)
        )

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                "✅ Gravação finalizada!\n\n"
                f"👤 @{username}\n"
                f"🎞️ Segmentos: {len(arquivos)}\n"
                f"📦 {tamanho_total_mb:.1f} MB\n\n"
                "🔄 Convertendo para MP4..."
            )
        )

        timestamp = datetime.now().strftime(
            "%Y%m%d_%H%M%S"
        )

        mp4_file = (
            OUTPUT_DIR /
            f"{username}_{timestamp}.mp4"
        )

        convertido = await converter_para_mp4(
            arquivos,
            mp4_file
        )

        if not convertido:

            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ Não foi possível "
                    "converter os arquivos para MP4."
                )
            )

            return

        if not mp4_file.exists():

            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ O FFmpeg terminou, "
                    "mas o MP4 não foi encontrado."
                )
            )

            return

        tamanho_mp4 = (
            mp4_file.stat().st_size /
            (1024 * 1024)
        )

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                "✅ MP4 pronto!\n\n"
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
                        f"🎥 Gravação de @{username}\n"
                        f"📦 {tamanho_mp4:.1f} MB"
                    ),
                    supports_streaming=True
                )

            enviado = True

        except Exception as erro:

            logging.warning(
                "Falha ao enviar como vídeo: %s",
                erro
            )

        # -------------------------------------------------
        # FALLBACK DOCUMENTO
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

            except Exception as erro:

                logging.exception(
                    "Erro ao enviar MP4."
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

            for arquivo in arquivos:

                try:

                    if arquivo.exists():
                        arquivo.unlink()

                except Exception as erro:

                    logging.warning(
                        "Erro apagando %s: %s",
                        arquivo,
                        erro
                    )

            try:

                if mp4_file.exists():
                    mp4_file.unlink()

            except Exception as erro:

                logging.warning(
                    "Erro apagando MP4: %s",
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
# GRAVAÇÃO COM RECONEXÃO AUTOMÁTICA
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

    inicio = datetime.now()

    recordings[username] = {
        "process": None,
        "chat_id": chat_id,
        "inicio": inicio,
        "tentativas": 0,
        "parar": False
    }

    try:

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔎 Procurando a live de "
                f"@{username}..."
            )
        )

        # =================================================
        # LOOP DE RECONEXÃO
        # =================================================

        while not recordings[username]["parar"]:

            data = recordings.get(
                username
            )

            if not data:
                break

            data["tentativas"] += 1

            tentativa = data["tentativas"]

            timestamp = datetime.now().strftime(
                "%Y%m%d_%H%M%S"
            )

            filename = (
                OUTPUT_DIR /
                f"{username}_{timestamp}.%(ext)s"
            )

            command = [
                "yt-dlp",

                "--newline",

                "--no-warnings",

                "--no-color",

                "-f",
                "best[ext=flv]/best",

                "--fragment-retries",
                "20",

                "--retries",
                "10",

                "--retry-sleep",
                "2",

                "--no-part",

                "-o",
                str(filename),

                url
            ]

            logging.info(
                "========================================"
            )

            logging.info(
                "TENTATIVA DE GRAVAÇÃO: %s",
                tentativa
            )

            logging.info(
                "Usuário: @%s",
                username
            )

            logging.info(
                "========================================"
            )

            processo = None

            try:

                processo = await asyncio.create_subprocess_exec(
                    *command,

                    stdout=asyncio.subprocess.PIPE,

                    stderr=asyncio.subprocess.STDOUT,

                    start_new_session=True
                )

                recordings[username]["process"] = processo

                if tentativa == 1:

                    await telegram_app.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "🔴 Gravação iniciada!\n\n"
                            f"👤 @{username}\n"
                            "🎞️ Formato: FLV\n"
                            "🔄 Reconexão automática: ativada"
                        )
                    )

                else:

                    await telegram_app.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"🔄 Conexão restabelecida!\n\n"
                            f"👤 @{username}\n"
                            f"🔢 Tentativa: {tentativa}"
                        )
                    )

                # -----------------------------------------
                # LER LOG DO YT-DLP
                # -----------------------------------------

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

                codigo = processo.returncode

                logging.warning(
                    "yt-dlp terminou. Código: %s",
                    codigo
                )

            except Exception as erro:

                logging.exception(
                    "Erro executando yt-dlp: %s",
                    erro
                )

            finally:

                recordings[username]["process"] = None

            # =================================================
            # VERIFICAR SE O USUÁRIO MANDOU /PARAR
            # =================================================

            if recordings[username]["parar"]:

                break

            # =================================================
            # CONEXÃO CAIU
            # =================================================

            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ Conexão com @{username} perdida.\n\n"
                    f"🔄 Tentando reconectar em "
                    f"{RECONNECT_SECONDS} segundos..."
                )
            )

            logging.warning(
                "Conexão perdida. "
                "Reconectando em %s segundos.",
                RECONNECT_SECONDS
            )

            # ---------------------------------------------
            # CONTAGEM REGRESSIVA
            # ---------------------------------------------

            for _ in range(
                RECONNECT_SECONDS
            ):

                if recordings[username]["parar"]:
                    break

                await asyncio.sleep(1)

        # =================================================
        # FINALIZAÇÃO
        # =================================================

        recordings.pop(
            username,
            None
        )

        await finalizar_gravacao(
            username,
            chat_id
        )

    except asyncio.CancelledError:

        logging.warning(
            "Gravação cancelada."
        )

        data = recordings.get(
            username
        )

        if data:

            processo = data.get(
                "process"
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
            "Erro geral na gravação."
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

        # ---------------------------------------------
        # MARCAR PARA NÃO RECONECTAR
        # ---------------------------------------------

        data["parar"] = True

        processo = data.get(
            "process"
        )

        await update.message.reply_text(
            f"⏹️ Parando @{username}..."
        )

        # ---------------------------------------------
        # PARAR YT-DLP
        # ---------------------------------------------

        if processo:

            try:

                if processo.returncode is None:

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
                            "yt-dlp não encerrou. "
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

        data["parar"] = True

        processo = data.get(
            "process"
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
