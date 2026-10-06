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

# Tempo entre verificações de lives
MONITOR_INTERVAL = 30

# Tempo entre tentativas de reconexão
RECONNECT_SECONDS = 10

app = FastAPI()

telegram_app = Application.builder().token(TOKEN).build()

# Gravações atualmente ativas
recordings = {}

# Usuários sendo monitorados
monitored_users = {}

# Tarefas de monitoramento
monitor_tasks = {}


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
        "/status - verificar gravações\n\n"
        "📡 Monitoramento automático:\n"
        "/monitorar usuario\n"
        "/desmonitorar usuario\n"
        "/monitorados"
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

        inicio = data.get("inicio")

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

        tentativas = data.get(
            "tentativas",
            1
        )

        mensagens.append(
            f"🔴 @{username}\n"
            f"⏱️ Tempo: {tempo}\n"
            f"🔄 Conexões: {tentativas}"
        )

    await update.message.reply_text(
        "📹 Gravações ativas:\n\n"
        + "\n\n".join(mensagens)
    )


# =========================================================
# VERIFICAR SE USUÁRIO ESTÁ AO VIVO
# =========================================================

async def verificar_live(username):

    url = (
        f"https://www.tiktok.com/"
        f"@{username}/live"
    )

    command = [
        "yt-dlp",

        "--dump-json",

        "--skip-download",

        "--no-warnings",

        "--no-color",

        url
    ]

    try:

        processo = await asyncio.create_subprocess_exec(
            *command,

            stdout=asyncio.subprocess.PIPE,

            stderr=asyncio.subprocess.PIPE
        )

        try:

            stdout, stderr = await asyncio.wait_for(
                processo.communicate(),
                timeout=30
            )

        except asyncio.TimeoutError:

            try:
                processo.kill()
            except Exception:
                pass

            await processo.wait()

            return False

        if processo.returncode != 0:
            return False

        texto = stdout.decode(
            "utf-8",
            errors="ignore"
        )

        if not texto.strip():
            return False

        # Se o yt-dlp encontrou a live,
        # normalmente haverá URL de transmissão.
        if '"is_live": true' in texto.lower():

            return True

        # Algumas versões podem não retornar
        # exatamente o campo esperado.
        # Procuramos também por protocolo/URL de transmissão.
        if (
            '"protocol"' in texto
            and '"url"' in texto
        ):

            return True

        return False

    except Exception as erro:

        logging.warning(
            "Erro verificando @%s: %s",
            username,
            erro
        )

        return False


# =========================================================
# PROCURAR ARQUIVOS FLV
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
# CONVERTER FLV -> MP4
# =========================================================

async def converter_para_mp4(
    arquivos,
    mp4_file
):

    if not arquivos:
        return False

    # -----------------------------------------------------
    # UM ARQUIVO
    # -----------------------------------------------------

    if len(arquivos) == 1:

        flv_file = arquivos[0]

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
            "Conversão direta falhou."
        )

        # -------------------------------------------------
        # RECODIFICAR
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

        return processo.returncode == 0

    # =====================================================
    # VÁRIOS ARQUIVOS
    # =====================================================

    lista_file = OUTPUT_DIR / "concat_list.txt"

    try:

        with open(
            lista_file,
            "w",
            encoding="utf-8"
        ) as lista:

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

                lista.write(
                    f"file '{caminho}'\n"
                )

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

        processo = await asyncio.create_subprocess_exec(
            *command,

            stdout=asyncio.subprocess.PIPE,

            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await processo.communicate()

        if processo.returncode == 0:

            try:
                lista_file.unlink()
            except Exception:
                pass

            return True

        # -------------------------------------------------
        # RECODIFICAÇÃO
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

            try:
                lista_file.unlink()
            except Exception:
                pass

            return True

        logging.error(
            stderr.decode(
                "utf-8",
                errors="ignore"
            )
        )

        return False

    except Exception as erro:

        logging.exception(
            "Erro na conversão: %s",
            erro
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

        tamanho_mb = (
            tamanho_total /
            (1024 * 1024)
        )

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                "✅ Gravação finalizada!\n\n"
                f"👤 @{username}\n"
                f"🎞️ Segmentos: {len(arquivos)}\n"
                f"📦 {tamanho_mb:.1f} MB\n\n"
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
                    "converter a gravação para MP4."
                )
            )

            return

        if not mp4_file.exists():

            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ O MP4 não foi encontrado "
                    "após a conversão."
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
        # VÍDEO
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
                "Falha enviando vídeo: %s",
                erro
            )

        # -------------------------------------------------
        # DOCUMENTO
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

                await telegram_app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "❌ Não consegui enviar "
                        "o MP4 para o Telegram.\n\n"
                        f"Erro: {erro}"
                    )
                )

        # -------------------------------------------------
        # LIMPEZA
        # -------------------------------------------------

        if enviado:

            for arquivo in arquivos:

                try:

                    if arquivo.exists():
                        arquivo.unlink()

                except Exception:
                    pass

            try:

                if mp4_file.exists():
                    mp4_file.unlink()

            except Exception:
                pass

            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    "✅ Vídeo enviado com sucesso! 🎥\n"
                    "🗑️ Arquivos temporários removidos."
                )
            )

    except Exception as erro:

        logging.exception(
            "Erro finalizando gravação."
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
                            "🎞️ FLV\n"
                            "🔄 Reconexão automática ativada"
                        )
                    )

                else:

                    await telegram_app.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"🔄 Conexão restabelecida!\n\n"
                            f"👤 @{username}"
                        )
                    )

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

            except Exception as erro:

                logging.exception(
                    "Erro no yt-dlp: %s",
                    erro
                )

            finally:

                recordings[username]["process"] = None

            if recordings[username]["parar"]:
                break

            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ Conexão com @{username} perdida.\n\n"
                    f"🔄 Reconectando em "
                    f"{RECONNECT_SECONDS} segundos..."
                )
            )

            for _ in range(
                RECONNECT_SECONDS
            ):

                if recordings[username]["parar"]:
                    break

                await asyncio.sleep(1)

        recordings.pop(
            username,
            None
        )

        await finalizar_gravacao(
            username,
            chat_id
        )

    except asyncio.CancelledError:

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

        data["parar"] = True

        processo = data.get(
            "process"
        )

        await update.message.reply_text(
            f"⏹️ Parando @{username}..."
        )

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
# MONITORAMENTO AUTOMÁTICO
# =========================================================

async def monitorar_usuario(
    username,
    chat_id
):

    logging.info(
        "Monitoramento iniciado: @%s",
        username
    )

    estava_ao_vivo = False

    while username in monitored_users:

        try:

            ao_vivo = await verificar_live(
                username
            )

            # -------------------------------------------------
            # LIVE COMEÇOU
            # -------------------------------------------------

            if ao_vivo and not estava_ao_vivo:

                logging.info(
                    "@%s entrou ao vivo!",
                    username
                )

                await telegram_app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "🔴 LIVE DETECTADA!\n\n"
                        f"👤 @{username}\n"
                        "🎥 Iniciando gravação automaticamente..."
                    )
                )

                if username not in recordings:

                    asyncio.create_task(
                        record_live(
                            username,
                            chat_id
                        )
                    )

                estava_ao_vivo = True

            # -------------------------------------------------
            # LIVE CONTINUA
            # -------------------------------------------------

            elif ao_vivo:

                estava_ao_vivo = True

            # -------------------------------------------------
            # LIVE TERMINOU
            # -------------------------------------------------

            elif not ao_vivo and estava_ao_vivo:

                logging.info(
                    "@%s saiu do ar.",
                    username
                )

                await telegram_app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⚫ A live de @{username} "
                        "parece ter terminado."
                    )
                )

                # Não forçamos o /parar imediatamente.
                # O yt-dlp vai encerrar sozinho e
                # finalizar o arquivo.
                estava_ao_vivo = False

            await asyncio.sleep(
                MONITOR_INTERVAL
            )

        except asyncio.CancelledError:

            break

        except Exception as erro:

            logging.exception(
                "Erro monitorando @%s: %s",
                username,
                erro
            )

            await asyncio.sleep(
                MONITOR_INTERVAL
            )

    logging.info(
        "Monitoramento encerrado: @%s",
        username
    )


# =========================================================
# /MONITORAR
# =========================================================

async def monitorar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.args:

        await update.message.reply_text(
            "Use:\n\n"
            "/monitorar joaobfilhoo1"
        )

        return

    username = (
        context.args[0]
        .replace("@", "")
        .strip()
        .lower()
    )

    chat_id = update.effective_chat.id

    if username in monitored_users:

        await update.message.reply_text(
            f"📡 @{username} "
            "já está sendo monitorado."
        )

        return

    monitored_users[username] = {
        "chat_id": chat_id,
        "inicio": datetime.now()
    }

    task = asyncio.create_task(
        monitorar_usuario(
            username,
            chat_id
        )
    )

    monitor_tasks[username] = task

    await update.message.reply_text(
        "📡 Monitoramento ativado!\n\n"
        f"👤 @{username}\n"
        f"⏱️ Verificação a cada "
        f"{MONITOR_INTERVAL} segundos.\n\n"
        "Quando a live começar, "
        "a gravação será iniciada automaticamente."
    )


# =========================================================
# /DESMONITORAR
# =========================================================

async def desmonitorar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.args:

        await update.message.reply_text(
            "Use:\n\n"
            "/desmonitorar joaobfilhoo1"
        )

        return

    username = (
        context.args[0]
        .replace("@", "")
        .strip()
        .lower()
    )

    if username not in monitored_users:

        await update.message.reply_text(
            f"⚠️ @{username} "
            "não está sendo monitorado."
        )

        return

    monitored_users.pop(
        username,
        None
    )

    task = monitor_tasks.pop(
        username,
        None
    )

    if task:

        task.cancel()

    await update.message.reply_text(
        f"🛑 Monitoramento de @{username} "
        "desativado."
    )


# =========================================================
# /MONITORADOS
# =========================================================

async def monitorados(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not monitored_users:

        await update.message.reply_text(
            "📡 Nenhum usuário sendo monitorado."
        )

        return

    lista = "\n".join(
        f"📡 @{username}"
        for username in monitored_users
    )

    await update.message.reply_text(
        "📡 Usuários monitorados:\n\n"
        + lista
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

telegram_app.add_handler(
    CommandHandler(
        "monitorar",
        monitorar
    )
)

telegram_app.add_handler(
    CommandHandler(
        "desmonitorar",
        desmonitorar
    )
)

telegram_app.add_handler(
    CommandHandler(
        "monitorados",
        monitorados
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

    for username, task in list(
        monitor_tasks.items()
    ):

        try:
            task.cancel()
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
