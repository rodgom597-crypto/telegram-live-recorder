import os
import asyncio
import logging
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# CONFIGURAÇÕES
# ============================================================

TOKEN = os.environ["BOT_TOKEN"]

RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

OUTPUT_DIR = Path("/tmp/recordings")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MONITOR_INTERVAL = 30
RECONNECT_SECONDS = 10


# ============================================================
# VARIÁVEIS GLOBAIS
# ============================================================

app = FastAPI()

telegram_app = None

# username -> processo de gravação
recordings = {}

# username -> True
monitored_users = {}

# username -> asyncio.Task
monitor_tasks = {}


# ============================================================
# COMANDOS DO TELEGRAM
# ============================================================

BOT_COMMANDS = [
    BotCommand("start", "Iniciar o bot"),
    BotCommand("ajuda", "Mostrar todos os comandos"),
    BotCommand("status", "Ver status do bot"),
    BotCommand("gravar", "Gravar uma live"),
    BotCommand("parar", "Parar a gravação"),
    BotCommand("monitorar", "Monitorar uma conta"),
    BotCommand("desmonitorar", "Parar de monitorar"),
    BotCommand("monitorados", "Ver contas monitoradas"),
]


# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def normalizar_usuario(username: str) -> str:
    username = username.strip()

    if username.startswith("@"):
        username = username[1:]

    return username.lower()


def url_live(username: str) -> str:
    return f"https://www.tiktok.com/@{username}/live"


def procurar_arquivos(username: str):
    arquivos = []

    for arquivo in OUTPUT_DIR.glob("*.flv"):
        if username.lower() in arquivo.name.lower():
            arquivos.append(arquivo)

    return sorted(arquivos)


# ============================================================
# VERIFICAR SE ESTÁ AO VIVO
# ============================================================

async def verificar_live(username: str) -> bool:
    username = normalizar_usuario(username)

    comando = [
        "yt-dlp",
        "--dump-json",
        "--skip-download",
        "--no-warnings",
        "--no-color",
        url_live(username),
    ]

    try:
        processo = await asyncio.create_subprocess_exec(
            *comando,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(
            processo.communicate(),
            timeout=30,
        )

        texto = stdout.decode("utf-8", errors="ignore").lower()

        if '"is_live": true' in texto:
            return True

        if '"protocol"' in texto and '"url"' in texto:
            return True

        return False

    except asyncio.TimeoutError:
        logger.warning(
            f"[{username}] Timeout verificando live."
        )

        try:
            processo.kill()
        except Exception:
            pass

        return False

    except Exception as e:
        logger.error(
            f"[{username}] Erro verificando live: {e}"
        )

        return False


# ============================================================
# CONVERTER FLV PARA MP4
# ============================================================

async def converter_para_mp4(arquivos, username: str):
    if not arquivos:
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    mp4_final = OUTPUT_DIR / (
        f"{username}_{timestamp}.mp4"
    )

    # --------------------------------------------------------
    # APENAS UM ARQUIVO
    # --------------------------------------------------------

    if len(arquivos) == 1:

        arquivo = arquivos[0]

        comando = [
            "ffmpeg",
            "-y",
            "-i",
            str(arquivo),
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            str(mp4_final),
        ]

        try:
            processo = await asyncio.create_subprocess_exec(
                *comando,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await processo.communicate()

            if processo.returncode == 0 and mp4_final.exists():
                return mp4_final

            logger.warning(
                f"[{username}] Conversão direta falhou."
            )

        except Exception as e:
            logger.error(
                f"[{username}] Erro na conversão: {e}"
            )

        # ----------------------------------------------------
        # FALLBACK: REENCODING
        # ----------------------------------------------------

        comando = [
            "ffmpeg",
            "-y",
            "-i",
            str(arquivo),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            str(mp4_final),
        ]

        try:
            processo = await asyncio.create_subprocess_exec(
                *comando,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await processo.communicate()

            if processo.returncode == 0 and mp4_final.exists():
                return mp4_final

        except Exception as e:
            logger.error(
                f"[{username}] Erro no reencoding: {e}"
            )

        return None

    # ========================================================
    # VÁRIOS ARQUIVOS
    # ========================================================

    lista_concat = OUTPUT_DIR / (
        f"concat_{username}_{timestamp}.txt"
    )

    try:
        with open(lista_concat, "w", encoding="utf-8") as f:

            for arquivo in arquivos:
                caminho = str(arquivo).replace("\\", "/")
                f.write(f"file '{caminho}'\n")

        comando = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(lista_concat),
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            str(mp4_final),
        ]

        processo = await asyncio.create_subprocess_exec(
            *comando,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await processo.communicate()

        if processo.returncode == 0 and mp4_final.exists():
            try:
                lista_concat.unlink()
            except Exception:
                pass

            return mp4_final

        logger.warning(
            f"[{username}] Concatenação direta falhou."
        )

        # ----------------------------------------------------
        # FALLBACK COM REENCODING
        # ----------------------------------------------------

        if mp4_final.exists():
            mp4_final.unlink()

        comando = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(lista_concat),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            str(mp4_final),
        ]

        processo = await asyncio.create_subprocess_exec(
            *comando,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await processo.communicate()

        if processo.returncode == 0 and mp4_final.exists():
            try:
                lista_concat.unlink()
            except Exception:
                pass

            return mp4_final

    except Exception as e:
        logger.error(
            f"[{username}] Erro juntando arquivos: {e}"
        )

    try:
        lista_concat.unlink()
    except Exception:
        pass

    return None


# ============================================================
# FINALIZAR GRAVAÇÃO
# ============================================================

async def finalizar_gravacao(
    username: str,
    chat_id: int,
    bot,
):
    username = normalizar_usuario(username)

    arquivos = procurar_arquivos(username)

    if not arquivos:
        logger.info(
            f"[{username}] Nenhum arquivo encontrado."
        )

        return

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"🎬 Gravação de @{username} finalizada.\n\n"
            f"📦 Preparando vídeo..."
        ),
    )

    mp4 = await converter_para_mp4(
        arquivos,
        username,
    )

    if not mp4 or not mp4.exists():

        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"❌ Não foi possível converter "
                f"a gravação de @{username}."
            ),
        )

        return

    try:

        tamanho_mb = mp4.stat().st_size / (
            1024 * 1024
        )

        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"📤 Enviando vídeo...\n"
                f"👤 @{username}\n"
                f"💾 {tamanho_mb:.1f} MB"
            ),
        )

        try:

            with open(mp4, "rb") as video:
                await bot.send_video(
                    chat_id=chat_id,
                    video=video,
                    supports_streaming=True,
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=60,
                )

        except Exception as e:

            logger.warning(
                f"[{username}] send_video falhou: {e}"
            )

            with open(mp4, "rb") as documento:
                await bot.send_document(
                    chat_id=chat_id,
                    document=documento,
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=60,
                )

        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ Gravação de @{username} "
                f"enviada com sucesso!"
            ),
        )

    except Exception as e:

        logger.error(
            f"[{username}] Erro enviando vídeo: {e}"
        )

        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"❌ Erro ao enviar a gravação "
                f"de @{username}:\n{e}"
            ),
        )

    finally:

        # ----------------------------------------------------
        # LIMPAR ARQUIVOS
        # ----------------------------------------------------

        for arquivo in arquivos:

            try:
                arquivo.unlink()
            except Exception:
                pass

        try:
            mp4.unlink()
        except Exception:
            pass


# ============================================================
# GRAVAÇÃO DA LIVE
# ============================================================

async def record_live(
    username: str,
    chat_id: int,
    bot,
):
    username = normalizar_usuario(username)

    if username in recordings:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ @{username} já está sendo gravado."
            ),
        )

        return

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"🔴 Iniciando gravação de @{username}..."
        ),
    )

    recordings[username] = None

    try:

        while True:

            # ------------------------------------------------
            # VERIFICAR SE O USUÁRIO AINDA ESTÁ SENDO GRAVADO
            # ------------------------------------------------

            if username not in recordings:
                break

            timestamp = datetime.now().strftime(
                "%Y%m%d_%H%M%S"
            )

            arquivo = OUTPUT_DIR / (
                f"{username}_{timestamp}.flv"
            )

            comando = [
                "yt-dlp",
                "-f",
                "best[ext=flv]/best",
                "--live-from-start",
                "--no-part",
                "--no-warnings",
                "--no-color",
                "-o",
                str(arquivo),
                url_live(username),
            ]

            logger.info(
                f"[{username}] Iniciando yt-dlp."
            )

            try:

                processo = await asyncio.create_subprocess_exec(
                    *comando,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )

                recordings[username] = processo

                # --------------------------------------------
                # LER LOG DO YT-DLP
                # --------------------------------------------

                while True:

                    linha = await processo.stdout.readline()

                    if not linha:
                        break

                    texto = linha.decode(
                        "utf-8",
                        errors="ignore",
                    ).strip()

                    if texto:
                        logger.info(
                            f"[{username}] {texto}"
                        )

                await processo.wait()

                logger.warning(
                    f"[{username}] yt-dlp terminou. "
                    f"Código: {processo.returncode}"
                )

            except Exception as e:

                logger.error(
                    f"[{username}] Erro no processo: {e}"
                )

            finally:

                if recordings.get(username) is processo:
                    recordings[username] = None

            # ------------------------------------------------
            # SE FOI PARADO MANUALMENTE
            # ------------------------------------------------

            if username not in recordings:
                break

            # ------------------------------------------------
            # RECONEXÃO
            # ------------------------------------------------

            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ Conexão da live de @{username} "
                    f"foi perdida.\n\n"
                    f"🔄 Tentando reconectar em "
                    f"{RECONNECT_SECONDS} segundos..."
                ),
            )

            await asyncio.sleep(RECONNECT_SECONDS)

    except asyncio.CancelledError:

        logger.info(
            f"[{username}] Tarefa de gravação cancelada."
        )

    except Exception as e:

        logger.error(
            f"[{username}] Erro geral na gravação: {e}"
        )

    finally:

        processo = recordings.get(username)

        if processo:

            try:
                processo.kill()
            except Exception:
                pass

        recordings.pop(username, None)

        # ----------------------------------------------------
        # FINALIZAR E ENVIAR
        # ----------------------------------------------------

        await finalizar_gravacao(
            username,
            chat_id,
            bot,
        )


# ============================================================
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "🤖 Bot de gravação TikTok LIVE ativo!\n\n"
        "Use /ajuda para ver todos os comandos."
    )


# ============================================================
# /AJUDA
# ============================================================

async def ajuda_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    texto = (
        "🤖 COMANDOS DO BOT\n\n"

        "▶️ /gravar usuario\n"
        "Inicia a gravação de uma live.\n\n"

        "⏹️ /parar\n"
        "Para a gravação atual.\n\n"

        "📡 /monitorar usuario\n"
        "Monitora uma conta e inicia a gravação "
        "automaticamente quando entrar ao vivo.\n\n"

        "🛑 /desmonitorar usuario\n"
        "Remove uma conta do monitoramento.\n\n"

        "👀 /monitorados\n"
        "Mostra as contas que estão sendo monitoradas.\n\n"

        "📊 /status\n"
        "Mostra o status atual do bot.\n\n"

        "❓ /ajuda\n"
        "Mostra esta mensagem."
    )

    await update.message.reply_text(texto)


# ============================================================
# /STATUS
# ============================================================

async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    gravando = list(recordings.keys())
    monitorados = list(monitored_users.keys())

    texto = "📊 STATUS DO BOT\n\n"

    if gravando:
        texto += "🔴 GRAVANDO:\n"

        for usuario in gravando:
            texto += f"• @{usuario}\n"

    else:
        texto += "🟢 Nenhuma gravação ativa.\n"

    texto += "\n"

    if monitorados:
        texto += "📡 MONITORANDO:\n"

        for usuario in monitorados:
            texto += f"• @{usuario}\n"

    else:
        texto += "⚪ Nenhuma conta monitorada."

    await update.message.reply_text(texto)


# ============================================================
# /GRAVAR
# ============================================================

async def gravar_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not context.args:

        await update.message.reply_text(
            "❌ Informe o usuário.\n\n"
            "Exemplo:\n"
            "/gravar joaobfilhoo1"
        )

        return

    username = normalizar_usuario(
        context.args[0]
    )

    if username in recordings:

        await update.message.reply_text(
            f"⚠️ @{username} já está sendo gravado."
        )

        return

    asyncio.create_task(
        record_live(
            username,
            update.effective_chat.id,
            context.bot,
        )
    )


# ============================================================
# /PARAR
# ============================================================

async def parar_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not recordings:

        await update.message.reply_text(
            "ℹ️ Não existe nenhuma gravação ativa."
        )

        return

    processos = list(recordings.items())

    for username, processo in processos:

        if processo:

            try:
                processo.terminate()
            except Exception:
                try:
                    processo.kill()
                except Exception:
                    pass

    await update.message.reply_text(
        "⏹️ Solicitação de parada enviada.\n\n"
        "A gravação será finalizada e o vídeo "
        "será convertido/enviado."
    )


# ============================================================
# MONITORAMENTO
# ============================================================

async def monitorar_usuario(
    username: str,
    chat_id: int,
    bot,
):

    username = normalizar_usuario(username)

    estava_ao_vivo = False

    logger.info(
        f"[MONITOR] Monitorando @{username}"
    )

    try:

        while username in monitored_users:

            ao_vivo = await verificar_live(username)

            logger.info(
                f"[MONITOR] @{username} "
                f"ao_vivo={ao_vivo}"
            )

            # ------------------------------------------------
            # ENTROU AO VIVO
            # ------------------------------------------------

            if ao_vivo and not estava_ao_vivo:

                estava_ao_vivo = True

                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🔴 @{username} entrou ao vivo!\n\n"
                        f"🎥 Iniciando gravação automática..."
                    ),
                )

                if username not in recordings:

                    asyncio.create_task(
                        record_live(
                            username,
                            chat_id,
                            bot,
                        )
                    )

            # ------------------------------------------------
            # CONTINUA AO VIVO
            # ------------------------------------------------

            elif ao_vivo:

                estava_ao_vivo = True

            # ------------------------------------------------
            # SAIU DO AR
            # ------------------------------------------------

            elif estava_ao_vivo:

                estava_ao_vivo = False

                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⚫ @{username} aparentemente "
                        f"saiu do ar."
                    ),
                )

            await asyncio.sleep(
                MONITOR_INTERVAL
            )

    except asyncio.CancelledError:

        logger.info(
            f"[MONITOR] Monitoramento de "
            f"@{username} cancelado."
        )

    except Exception as e:

        logger.error(
            f"[MONITOR] Erro em @{username}: {e}"
        )

    finally:

        monitor_tasks.pop(username, None)


# ============================================================
# /MONITORAR
# ============================================================

async def monitorar_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not context.args:

        await update.message.reply_text(
            "❌ Informe o usuário.\n\n"
            "Exemplo:\n"
            "/monitorar joaobfilhoo1"
        )

        return

    username = normalizar_usuario(
        context.args[0]
    )

    chat_id = update.effective_chat.id

    if username in monitored_users:

        await update.message.reply_text(
            f"⚠️ @{username} já está sendo monitorado."
        )

        return

    monitored_users[username] = True

    task = asyncio.create_task(
        monitorar_usuario(
            username,
            chat_id,
            context.bot,
        )
    )

    monitor_tasks[username] = task

    await update.message.reply_text(
        f"📡 Monitoramento ativado para "
        f"@{username}.\n\n"
        f"⏱️ Verificação a cada "
        f"{MONITOR_INTERVAL} segundos.\n\n"
        f"🔴 Quando entrar ao vivo, "
        f"a gravação começará automaticamente."
    )


# ============================================================
# /DESMONITORAR
# ============================================================

async def desmonitorar_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not context.args:

        await update.message.reply_text(
            "❌ Informe o usuário.\n\n"
            "Exemplo:\n"
            "/desmonitorar joaobfilhoo1"
        )

        return

    username = normalizar_usuario(
        context.args[0]
    )

    if username not in monitored_users:

        await update.message.reply_text(
            f"⚠️ @{username} não está sendo monitorado."
        )

        return

    monitored_users.pop(username, None)

    task = monitor_tasks.get(username)

    if task:

        task.cancel()

    monitor_tasks.pop(username, None)

    await update.message.reply_text(
        f"🛑 Monitoramento de @{username} "
        f"desativado."
    )


# ============================================================
# /MONITORADOS
# ============================================================

async def monitorados_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not monitored_users:

        await update.message.reply_text(
            "📡 Nenhuma conta está sendo monitorada."
        )

        return

    texto = "📡 CONTAS MONITORADAS\n\n"

    for i, username in enumerate(
        monitored_users.keys(),
        start=1,
    ):

        texto += f"{i}. @{username}\n"

    await update.message.reply_text(texto)


# ============================================================
# REGISTRAR COMANDOS AUTOMATICAMENTE
# ============================================================

async def registrar_comandos(bot):

    try:

        await bot.set_my_commands(
            BOT_COMMANDS
        )

        logger.info(
            "✅ Comandos do Telegram registrados "
            "com sucesso."
        )

    except Exception as e:

        logger.error(
            f"❌ Erro registrando comandos: {e}"
        )


# ============================================================
# WEBHOOK
# ============================================================

@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
):

    global telegram_app

    data = await request.json()

    update = Update.de_json(
        data,
        telegram_app.bot,
    )

    await telegram_app.process_update(update)

    return {
        "ok": True
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/")
async def root():

    return PlainTextResponse(
        "Telegram TikTok Recorder funcionando!"
    )


@app.get("/health")
async def health():

    return {
        "status": "ok",
        "recordings": list(recordings.keys()),
        "monitored": list(monitored_users.keys()),
    }


# ============================================================
# INICIAR BOT
# ============================================================

@app.on_event("startup")
async def startup_event():

    global telegram_app

    logger.info(
        "🚀 Iniciando Telegram TikTok Recorder..."
    )

    telegram_app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # HANDLERS
    # --------------------------------------------------------

    telegram_app.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "ajuda",
            ajuda_command,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "status",
            status_command,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "gravar",
            gravar_command,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "parar",
            parar_command,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "monitorar",
            monitorar_command,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "desmonitorar",
            desmonitorar_command,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "monitorados",
            monitorados_command,
        )
    )

    # --------------------------------------------------------
    # INICIALIZAR APPLICATION
    # --------------------------------------------------------

    await telegram_app.initialize()

    await telegram_app.start()

    # --------------------------------------------------------
    # REGISTRAR COMANDOS NO TELEGRAM
    # --------------------------------------------------------

    await registrar_comandos(
        telegram_app.bot
    )

    # --------------------------------------------------------
    # CONFIGURAR WEBHOOK
    # --------------------------------------------------------

    if RENDER_EXTERNAL_URL:

        webhook_url = (
            f"{RENDER_EXTERNAL_URL}"
            f"/telegram/webhook"
        )

        try:

            await telegram_app.bot.set_webhook(
                url=webhook_url
            )

            logger.info(
                f"✅ Webhook configurado: "
                f"{webhook_url}"
            )

        except Exception as e:

            logger.error(
                f"❌ Erro configurando webhook: {e}"
            )

    else:

        logger.warning(
            "⚠️ RENDER_EXTERNAL_URL não configurada."
        )


# ============================================================
# DESLIGAR BOT
# ============================================================

@app.on_event("shutdown")
async def shutdown_event():

    global telegram_app

    logger.info(
        "🛑 Encerrando bot..."
    )

    # --------------------------------------------------------
    # PARAR GRAVAÇÕES
    # --------------------------------------------------------

    for username, processo in list(
        recordings.items()
    ):

        if processo:

            try:
                processo.kill()
            except Exception:
                pass

    recordings.clear()

    # --------------------------------------------------------
    # CANCELAR MONITORES
    # --------------------------------------------------------

    for username, task in list(
        monitor_tasks.items()
    ):

        try:
            task.cancel()
        except Exception:
            pass

    monitor_tasks.clear()
    monitored_users.clear()

    # --------------------------------------------------------
    # DESLIGAR TELEGRAM
    # --------------------------------------------------------

    if telegram_app:

        try:
            await telegram_app.stop()
        except Exception:
            pass

        try:
            await telegram_app.shutdown()
        except Exception:
            pass

    logger.info(
        "✅ Bot encerrado."
    )
