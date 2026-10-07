import asyncio
import json
import logging
import os
import re
import signal
from datetime import datetime
from pathlib import Path
from typing import Optional

import redis.asyncio as redis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ============================================================
# CONFIGURAÇÃO
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN não configurado.")

BASE_DIR = Path(__file__).resolve().parent

GRAVACOES_DIR = BASE_DIR / "gravacoes"
GRAVACOES_DIR.mkdir(parents=True, exist_ok=True)

REDIS_URL = os.getenv("REDIS_URL")

WEBHOOK_URL = os.getenv(
    "WEBHOOK_URL",
    "https://telegram-live-recorder.onrender.com/telegram/webhook"
)

MONITOR_INTERVAL = 30

# Quantas verificações negativas consecutivas são necessárias
# para realmente considerar que a live terminou.
OFFLINE_CONFIRMATIONS_REQUIRED = 3

# Quantas tentativas o /gravar fará quando o TikTok responder
# incorretamente que não está ao vivo.
MANUAL_RECORD_ATTEMPTS = 4

# Tempo entre tentativas do /gravar
MANUAL_RETRY_DELAY = 10

# Tempo entre tentativas de reconexão de uma gravação monitorada
RECONNECT_DELAY = 10


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# FASTAPI / TELEGRAM
# ============================================================

app = FastAPI()

telegram_app: Optional[Application] = None
redis_client = None

# ============================================================
# ESTADO
# ============================================================

# Exemplo:
# {
#   "cmlykimberly": {
#       "live": True,
#       "recording": True,
#       "task": ...
#   }
# }
monitored_users = {}

# Tarefas de monitoramento
monitor_tasks = {}

# Processos de gravação
recording_processes = {}

# Contadores de confirmações de offline
offline_confirmations = {}

# Lock para evitar duas gravações do mesmo usuário
recording_locks = {}


# ============================================================
# REDIS
# ============================================================

async def conectar_redis():
    global redis_client

    if not REDIS_URL:
        logger.warning("⚠️ REDIS_URL não configurado.")
        return False

    try:
        redis_client = redis.from_url(
            REDIS_URL,
            decode_responses=True
        )

        await redis_client.ping()

        logger.info("✅ Conectado ao Render Key Value.")
        return True

    except Exception as e:
        logger.error(f"❌ Erro ao conectar ao Redis: {e}")
        redis_client = None
        return False


async def salvar_monitorados():
    if redis_client is None:
        logger.warning(
            "⚠️ Redis não disponível. Monitorados não foram salvos."
        )
        return

    try:
        dados = {}

        for username, info in monitored_users.items():
            dados[username] = {
                "live": bool(info.get("live", False))
            }

        await redis_client.set(
            "monitorados",
            json.dumps(dados)
        )

        logger.info(
            f"💾 {len(dados)} conta(s) monitorada(s) salva(s)."
        )

    except Exception as e:
        logger.error(
            f"❌ Erro ao salvar monitorados no Redis: {e}"
        )


async def carregar_monitorados():
    global monitored_users

    if redis_client is None:
        return

    try:
        dados = await redis_client.get("monitorados")

        if not dados:
            logger.info("📭 Nenhuma conta monitorada salva.")
            return

        carregados = json.loads(dados)

        monitored_users = {}

        for username, info in carregados.items():
            monitored_users[username] = {
                "live": bool(info.get("live", False)),
                "recording": False,
                "task": None
            }

            offline_confirmations[username] = 0

        logger.info(
            f"📂 {len(monitored_users)} conta(s) carregada(s) do Redis."
        )

    except Exception as e:
        logger.error(
            f"❌ Erro ao carregar monitorados: {e}"
        )


# ============================================================
# UTILITÁRIOS
# ============================================================

def normalizar_usuario(username: str) -> str:
    username = username.strip()

    if username.startswith("@"):
        username = username[1:]

    if username.startswith("https://www.tiktok.com/@"):
        username = username.split("@", 1)[1]

    username = username.split("/", 1)[0]
    username = username.strip()

    return username.lower()


def nome_seguro(username: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", username)


def url_tiktok(username: str) -> str:
    return f"https://www.tiktok.com/@{username}/live"


def encontrar_arquivos(username: str):
    prefixo = nome_seguro(username)

    arquivos = []

    for extensao in ("*.flv", "*.mp4"):
        arquivos.extend(GRAVACOES_DIR.glob(extensao))

    arquivos = [
        arquivo
        for arquivo in arquivos
        if prefixo in arquivo.name
    ]

    return sorted(
        arquivos,
        key=lambda x: x.stat().st_mtime if x.exists() else 0,
        reverse=True
    )


async def enviar_mensagem(chat_id, texto):
    try:
        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=texto
        )
    except Exception as e:
        logger.error(
            f"Erro ao enviar mensagem para {chat_id}: {e}"
        )


# ============================================================
# VERIFICAÇÃO DA LIVE
# ============================================================

async def verificar_live(username: str):
    """
    Retorno:

    True  = confirmou que está ao vivo
    False = recebeu confirmação razoavelmente clara de offline
    None  = resultado inconclusivo / erro / falso negativo possível

    IMPORTANTE:
    Não tratamos "The channel is not currently live" como
    confirmação definitiva de offline, pois o TikTok/yt-dlp
    apresenta falsos negativos.
    """

    username = normalizar_usuario(username)
    url = url_tiktok(username)

    comando = [
        "yt-dlp",
        "--dump-json",
        "--skip-download",
        "--no-warnings",
        "--no-color",
        "--socket-timeout",
        "15",
        url
    ]

    processo = None

    try:
        processo = await asyncio.create_subprocess_exec(
            *comando,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await asyncio.wait_for(
            processo.communicate(),
            timeout=45
        )

        saida = stdout.decode(
            "utf-8",
            errors="ignore"
        )

        erro = stderr.decode(
            "utf-8",
            errors="ignore"
        )

        texto = f"{saida}\n{erro}".lower()

        # ----------------------------------------------------
        # JSON: LIVE CONFIRMADA
        # ----------------------------------------------------

        for linha in saida.splitlines():
            linha = linha.strip()

            if not linha.startswith("{"):
                continue

            try:
                dados = json.loads(linha)

                if dados.get("is_live") is True:
                    return True

                # Mesmo que is_live venha False, não vamos
                # imediatamente considerar offline.
                if dados.get("is_live") is False:
                    return None

            except Exception:
                continue

        # ----------------------------------------------------
        # ERROS CONHECIDOS DE FALSO NEGATIVO
        # ----------------------------------------------------

        falsos_negativos = [
            "the channel is not currently live",
            "channel is not currently live",
            "not currently live",
            "user is not live",
            "the user is not live",
        ]

        for mensagem in falsos_negativos:
            if mensagem in texto:
                logger.info(
                    f"[{username}] yt-dlp informou que não está ao vivo "
                    f"(resultado inconclusivo)."
                )

                return None

        # ----------------------------------------------------
        # ALGUNS RESULTADOS QUE INDICAM LIVE
        # ----------------------------------------------------

        if '"is_live": true' in texto:
            return True

        if '"islive": true' in texto:
            return True

        # ----------------------------------------------------
        # ERRO / RESULTADO INCONCLUSIVO
        # ----------------------------------------------------

        if processo.returncode != 0:
            logger.info(
                f"[{username}] Verificação inconclusiva. "
                f"Código yt-dlp: {processo.returncode}"
            )

            return None

        return None

    except asyncio.TimeoutError:
        logger.warning(
            f"[{username}] Timeout na verificação da live."
        )

        if processo:
            try:
                processo.kill()
            except Exception:
                pass

        return None

    except Exception as e:
        logger.error(
            f"[{username}] Erro ao verificar live: {e}"
        )

        return None


async def verificar_live_com_tentativas(
    username: str,
    tentativas: int = 3,
    intervalo: int = 5
):
    """
    Faz várias verificações.

    Se qualquer uma confirmar live:
        True

    Se nenhuma confirmar:
        None

    Não transforma falso negativo em offline.
    """

    for tentativa in range(1, tentativas + 1):

        resultado = await verificar_live(username)

        if resultado is True:
            logger.info(
                f"[{username}] LIVE confirmada "
                f"na tentativa {tentativa}/{tentativas}."
            )

            return True

        logger.info(
            f"[{username}] Verificação inconclusiva "
            f"{tentativa}/{tentativas}."
        )

        if tentativa < tentativas:
            await asyncio.sleep(intervalo)

    return None


# ============================================================
# CONVERSÃO FLV -> MP4
# ============================================================

async def converter_para_mp4(arquivo_flv: Path):
    if not arquivo_flv.exists():
        return None

    arquivo_mp4 = arquivo_flv.with_suffix(".mp4")

    logger.info(
        f"[{arquivo_flv.stem}] Convertendo FLV para MP4."
    )

    comando = [
        "ffmpeg",
        "-y",
        "-fflags",
        "+genpts+discardcorrupt",
        "-err_detect",
        "ignore_err",
        "-i",
        str(arquivo_flv),
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(arquivo_mp4)
    ]

    try:
        processo = await asyncio.create_subprocess_exec(
            *comando,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await processo.communicate()

        if processo.returncode == 0 and arquivo_mp4.exists():
            logger.info(
                f"[{arquivo_flv.stem}] MP4 criado: "
                f"{arquivo_mp4.name}"
            )

            try:
                arquivo_flv.unlink()
            except Exception:
                pass

            return arquivo_mp4

        logger.warning(
            f"[{arquivo_flv.stem}] Primeira conversão falhou. "
            f"Tentando modo alternativo."
        )

    except Exception as e:
        logger.error(
            f"[{arquivo_flv.stem}] Erro no FFmpeg: {e}"
        )

    # --------------------------------------------------------
    # FALLBACK
    # --------------------------------------------------------

    comando_fallback = [
        "ffmpeg",
        "-y",
        "-err_detect",
        "ignore_err",
        "-i",
        str(arquivo_flv),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "25",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(arquivo_mp4)
    ]

    try:
        processo = await asyncio.create_subprocess_exec(
            *comando_fallback,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        await processo.communicate()

        if processo.returncode == 0 and arquivo_mp4.exists():
            logger.info(
                f"[{arquivo_flv.stem}] MP4 criado no fallback."
            )

            try:
                arquivo_flv.unlink()
            except Exception:
                pass

            return arquivo_mp4

    except Exception as e:
        logger.error(
            f"[{arquivo_flv.stem}] Fallback FFmpeg falhou: {e}"
        )

    return None


# ============================================================
# GRAVAÇÃO
# ============================================================

async def executar_yt_dlp(username: str):
    """
    Executa uma sessão de gravação.

    Retorna:
        (returncode, arquivo, log)
    """

    username = normalizar_usuario(username)
    url = url_tiktok(username)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    prefixo = nome_seguro(username)

    arquivo = (
        GRAVACOES_DIR
        / f"{prefixo}_{timestamp}.flv"
    )

    comando = [
        "yt-dlp",
        "-f",
        "best[ext=flv]/best",
        "--no-part",
        "--no-continue",
        "--no-overwrites",
        "--no-warnings",
        "--newline",
        "--socket-timeout",
        "30",
        "-o",
        str(arquivo),
        url
    ]

    logger.info(
        f"[{username}] Iniciando yt-dlp."
    )

    processo = None
    linhas = []

    try:
        processo = await asyncio.create_subprocess_exec(
            *comando,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        recording_processes[username] = processo

        while True:
            linha = await processo.stdout.readline()

            if not linha:
                break

            texto = linha.decode(
                "utf-8",
                errors="ignore"
            ).strip()

            if texto:
                linhas.append(texto)

                logger.info(
                    f"[{username}] {texto}"
                )

        codigo = await processo.wait()

        return codigo, arquivo, "\n".join(linhas)

    except asyncio.CancelledError:
        if processo:
            try:
                processo.terminate()
            except Exception:
                pass

        raise

    except Exception as e:
        logger.error(
            f"[{username}] Erro executando yt-dlp: {e}"
        )

        return -1, arquivo, str(e)

    finally:
        recording_processes.pop(username, None)


async def gravar_live(
    username: str,
    chat_id=None,
    manual=False
):
    """
    Grava uma live.

    manual=True:
        usado pelo /gravar

    manual=False:
        usado pelo monitoramento automático
    """

    username = normalizar_usuario(username)

    if username not in recording_locks:
        recording_locks[username] = asyncio.Lock()

    lock = recording_locks[username]

    if lock.locked():
        logger.info(
            f"[{username}] Já existe uma gravação em andamento."
        )
        return

    async with lock:

        logger.info(
            f"[{username}] Iniciando sistema de gravação."
        )

        if chat_id:
            await enviar_mensagem(
                chat_id,
                f"🎥 Iniciando gravação de @{username}..."
            )

        tentativas = (
            MANUAL_RECORD_ATTEMPTS
            if manual
            else 999999
        )

        tentativa = 0

        while tentativa < tentativas:

            tentativa += 1

            # ------------------------------------------------
            # Para monitoramento, se a conta foi removida,
            # encerramos.
            # ------------------------------------------------

            if not manual:
                if username not in monitored_users:
                    logger.info(
                        f"[{username}] Removido do monitoramento."
                    )
                    break

            # ------------------------------------------------
            # EXECUTA YT-DLP
            # ------------------------------------------------

            codigo, arquivo, log_gravacao = (
                await executar_yt_dlp(username)
            )

            logger.info(
                f"[{username}] yt-dlp terminou. "
                f"Código: {codigo}"
            )

            # ------------------------------------------------
            # VERIFICA SE GEROU ARQUIVO
            # ------------------------------------------------

            arquivos = encontrar_arquivos(username)

            arquivo_final = None

            if arquivo.exists():
                arquivo_final = arquivo

            elif arquivos:
                arquivo_final = arquivos[0]

            # ------------------------------------------------
            # SE EXISTIR GRAVAÇÃO, CONVERTE
            # ------------------------------------------------

            if arquivo_final and arquivo_final.exists():

                tamanho = arquivo_final.stat().st_size

                logger.info(
                    f"[{username}] Arquivo encontrado: "
                    f"{arquivo_final.name} "
                    f"({tamanho / 1024 / 1024:.2f} MB)"
                )

                # Só converte se for FLV
                if arquivo_final.suffix.lower() == ".flv":

                    mp4 = await converter_para_mp4(
                        arquivo_final
                    )

                    if mp4:
                        arquivo_final = mp4

                if chat_id and arquivo_final:
                    await enviar_mensagem(
                        chat_id,
                        f"✅ Gravação finalizada!\n\n"
                        f"👤 @{username}\n"
                        f"📁 {arquivo_final.name}"
                    )

                # ------------------------------------------------
                # Se for monitorada, verificamos se continua live.
                # ------------------------------------------------

                if not manual:

                    resultado = await verificar_live(
                        username
                    )

                    if resultado is True:
                        logger.info(
                            f"[{username}] Live ainda está ativa. "
                            f"Reconectando."
                        )

                        await asyncio.sleep(
                            RECONNECT_DELAY
                        )

                        continue

                    # Resultado inconclusivo NÃO significa offline.
                    logger.info(
                        f"[{username}] Estado da live "
                        f"não pôde ser confirmado."
                    )

                    await asyncio.sleep(
                        RECONNECT_DELAY
                    )

                    continue

                break

            # ------------------------------------------------
            # NÃO GEROU ARQUIVO
            # ------------------------------------------------

            log_lower = log_gravacao.lower()

            falso_negativo = (
                "not currently live" in log_lower
                or "channel is not currently live" in log_lower
                or "user is not live" in log_lower
            )

            if falso_negativo:
                logger.warning(
                    f"[{username}] TikTok/yt-dlp informou "
                    f"que não está ao vivo, mas isso pode "
                    f"ser falso negativo."
                )

            # ------------------------------------------------
            # MANUAL
            # ------------------------------------------------

            if manual:

                if tentativa < tentativas:

                    logger.info(
                        f"[{username}] Tentativa "
                        f"{tentativa}/{tentativas} falhou. "
                        f"Tentando novamente em "
                        f"{MANUAL_RETRY_DELAY}s."
                    )

                    if chat_id:
                        await enviar_mensagem(
                            chat_id,
                            f"⚠️ O TikTok não entregou a live "
                            f"na tentativa {tentativa}.\n\n"
                            f"🔄 Tentando novamente..."
                        )

                    await asyncio.sleep(
                        MANUAL_RETRY_DELAY
                    )

                    continue

                logger.warning(
                    f"[{username}] Todas as tentativas "
                    f"de gravação falharam."
                )

                if chat_id:
                    await enviar_mensagem(
                        chat_id,
                        f"❌ Não foi possível iniciar a "
                        f"gravação de @{username}.\n\n"
                        f"O TikTok/yt-dlp não entregou o "
                        f"stream após várias tentativas."
                    )

                break

            # ------------------------------------------------
            # MONITORAMENTO
            # ------------------------------------------------

            logger.info(
                f"[{username}] Nenhum arquivo gerado. "
                f"Verificando novamente."
            )

            await asyncio.sleep(
                RECONNECT_DELAY
            )


# ============================================================
# MONITORAMENTO
# ============================================================

async def monitorar_usuario(username: str):
    username = normalizar_usuario(username)

    logger.info(
        f"[{username}] Monitoramento iniciado."
    )

    offline_confirmations[username] = 0

    while username in monitored_users:

        try:

            resultado = await verificar_live(
                username
            )

            # =================================================
            # LIVE CONFIRMADA
            # =================================================

            if resultado is True:

                offline_confirmations[username] = 0

                estava_ao_vivo = monitored_users[
                    username
                ].get("live", False)

                monitored_users[
                    username
                ]["live"] = True

                # Se acabou de entrar ao vivo
                if not estava_ao_vivo:

                    logger.info(
                        f"[{username}] 🔴 LIVE DETECTADA."
                    )

                    # Inicia gravação
                    asyncio.create_task(
                        gravar_live(
                            username,
                            manual=False
                        )
                    )

            # =================================================
            # RESULTADO INCONCLUSIVO
            # =================================================

            else:

                offline_confirmations[username] = (
                    offline_confirmations.get(
                        username,
                        0
                    ) + 1
                )

                contador = offline_confirmations[
                    username
                ]

                logger.info(
                    f"[{username}] Verificação "
                    f"inconclusiva/offline "
                    f"{contador}/"
                    f"{OFFLINE_CONFIRMATIONS_REQUIRED}."
                )

                # Só consideramos encerrada depois de
                # várias verificações consecutivas.
                if contador >= OFFLINE_CONFIRMATIONS_REQUIRED:

                    estava_ao_vivo = monitored_users[
                        username
                    ].get("live", False)

                    if estava_ao_vivo:

                        logger.info(
                            f"[{username}] 🔵 Live considerada "
                            f"encerrada após "
                            f"{OFFLINE_CONFIRMATIONS_REQUIRED} "
                            f"verificações."
                        )

                    monitored_users[
                        username
                    ]["live"] = False

                    offline_confirmations[
                        username
                    ] = 0

            await asyncio.sleep(
                MONITOR_INTERVAL
            )

        except asyncio.CancelledError:
            logger.info(
                f"[{username}] Monitoramento cancelado."
            )
            break

        except Exception as e:
            logger.error(
                f"[{username}] Erro no monitoramento: {e}"
            )

            await asyncio.sleep(
                MONITOR_INTERVAL
            )

    logger.info(
        f"[{username}] Monitoramento encerrado."
    )


async def iniciar_monitores_salvos():

    if not monitored_users:
        logger.info(
            "Nenhum monitor para restaurar."
        )
        return

    for username in list(monitored_users.keys()):

        if username in monitor_tasks:
            continue

        task = asyncio.create_task(
            monitorar_usuario(username)
        )

        monitor_tasks[username] = task

        monitored_users[
            username
        ]["task"] = task

        logger.info(
            f"🔄 Monitor restaurado: @{username}"
        )


# ============================================================
# COMANDOS TELEGRAM
# ============================================================

async def cmd_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "🤖 Bot de gravação TikTok LIVE\n\n"
        "/gravar usuario - grava uma live agora\n"
        "/monitorar usuario - monitora automaticamente\n"
        "/desmonitorar usuario - remove monitoramento\n"
        "/monitorados - lista contas monitoradas"
    )


async def cmd_gravar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.args:

        await update.message.reply_text(
            "Use:\n/gravar usuario"
        )

        return

    username = normalizar_usuario(
        context.args[0]
    )

    await update.message.reply_text(
        f"🎥 Solicitação de gravação enviada "
        f"para @{username}."
    )

    asyncio.create_task(
        gravar_live(
            username,
            chat_id=update.effective_chat.id,
            manual=True
        )
    )


async def cmd_monitorar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.args:

        await update.message.reply_text(
            "Use:\n/monitorar usuario"
        )

        return

    username = normalizar_usuario(
        context.args[0]
    )

    if username in monitored_users:

        await update.message.reply_text(
            f"👁️ @{username} já está sendo monitorado."
        )

        return

    monitored_users[username] = {
        "live": False,
        "recording": False,
        "task": None
    }

    offline_confirmations[username] = 0

    task = asyncio.create_task(
        monitorar_usuario(username)
    )

    monitor_tasks[username] = task

    monitored_users[
        username
    ]["task"] = task

    await salvar_monitorados()

    await update.message.reply_text(
        f"👁️ Monitoramento ativado para @{username}.\n\n"
        f"💾 Salvo permanentemente.\n"
        f"🔄 Verificação a cada {MONITOR_INTERVAL} segundos."
    )


async def cmd_desmonitorar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.args:

        await update.message.reply_text(
            "Use:\n/desmonitorar usuario"
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

    # Cancela monitor
    task = monitor_tasks.get(username)

    if task:

        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

    monitor_tasks.pop(username, None)

    # Se houver gravação ativa, tenta encerrar
    processo = recording_processes.get(username)

    if processo:

        try:
            processo.terminate()
        except Exception:
            pass

    monitored_users.pop(
        username,
        None
    )

    offline_confirmations.pop(
        username,
        None
    )

    await salvar_monitorados()

    await update.message.reply_text(
        f"🛑 Monitoramento desativado para @{username}.\n"
        f"💾 Removido do armazenamento permanente."
    )


async def cmd_monitorados(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    # Recarrega do Redis para garantir estado atualizado
    if redis_client is not None:

        try:

            dados = await redis_client.get(
                "monitorados"
            )

            if dados:

                salvos = json.loads(dados)

                for username, info in salvos.items():

                    if username not in monitored_users:

                        monitored_users[username] = {
                            "live": bool(
                                info.get(
                                    "live",
                                    False
                                )
                            ),
                            "recording": False,
                            "task": None
                        }

        except Exception as e:

            logger.error(
                f"Erro atualizando lista do Redis: {e}"
            )

    if not monitored_users:

        await update.message.reply_text(
            "📭 Nenhuma conta monitorada."
        )

        return

    linhas = [
        "👁️ CONTAS MONITORADAS",
        ""
    ]

    for username in sorted(
        monitored_users.keys()
    ):

        live = monitored_users[
            username
        ].get("live", False)

        status = (
            "🔴 AO VIVO"
            if live
            else "⚫ offline"
        )

        linhas.append(
            f"• @{username} — {status}"
        )

    await update.message.reply_text(
        "\n".join(linhas)
    )


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):

    try:

        data = await request.json()

        update = Update.de_json(
            data,
            telegram_app.bot
        )

        await telegram_app.process_update(
            update
        )

        return JSONResponse(
            {"ok": True}
        )

    except Exception as e:

        logger.error(
            f"Erro no webhook Telegram: {e}"
        )

        return JSONResponse(
            {
                "ok": False,
                "error": str(e)
            },
            status_code=500
        )


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/")
async def home():

    return {
        "status": "online",
        "service": "TikTok Live Recorder",
        "monitored": len(monitored_users)
    }


@app.get("/health")
async def health():

    return {
        "status": "ok",
        "telegram": telegram_app is not None,
        "redis": redis_client is not None,
        "monitored": len(monitored_users)
    }


# ============================================================
# INICIALIZAÇÃO
# ============================================================

async def inicializar():

    global telegram_app

    logger.info("🚀 Iniciando bot...")

    # --------------------------------------------------------
    # REDIS
    # --------------------------------------------------------

    await conectar_redis()

    await carregar_monitorados()

    # --------------------------------------------------------
    # TELEGRAM
    # --------------------------------------------------------

    telegram_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    telegram_app.add_handler(
        CommandHandler(
            "start",
            cmd_start
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "gravar",
            cmd_gravar
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "monitorar",
            cmd_monitorar
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "desmonitorar",
            cmd_desmonitorar
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "monitorados",
            cmd_monitorados
        )
    )

    await telegram_app.initialize()

    await telegram_app.start()

    # --------------------------------------------------------
    # COMANDOS
    # --------------------------------------------------------

    try:

        await telegram_app.bot.set_my_commands([
            ("start", "Iniciar bot"),
            ("gravar", "Gravar uma live"),
            ("monitorar", "Monitorar uma conta"),
            ("desmonitorar", "Parar monitoramento"),
            ("monitorados", "Listar monitorados"),
        ])

        logger.info(
            "Comandos do Telegram configurados."
        )

    except Exception as e:

        logger.error(
            f"Erro configurando comandos: {e}"
        )

    # --------------------------------------------------------
    # WEBHOOK
    # --------------------------------------------------------

    try:

        await telegram_app.bot.set_webhook(
            url=WEBHOOK_URL
        )

        logger.info(
            f"Webhook configurado: {WEBHOOK_URL}"
        )

    except Exception as e:

        logger.error(
            f"Erro configurando webhook: {e}"
        )

    # --------------------------------------------------------
    # RESTAURA MONITORES
    # --------------------------------------------------------

    await iniciar_monitores_salvos()

    logger.info(
        "🤖 Bot iniciado com sucesso."
    )


# ============================================================
# SHUTDOWN
# ============================================================

async def finalizar():

    logger.info(
        "🛑 Encerrando bot..."
    )

    # Salva antes de encerrar
    await salvar_monitorados()

    # Cancela monitores
    for username, task in list(
        monitor_tasks.items()
    ):

        if task:

            task.cancel()

    # Encerra gravações
    for username, processo in list(
        recording_processes.items()
    ):

        try:
            processo.terminate()
        except Exception:
            pass

    # Telegram
    if telegram_app:

        try:
            await telegram_app.stop()
        except Exception:
            pass

        try:
            await telegram_app.shutdown()
        except Exception:
            pass

    # Redis
    if redis_client:

        try:
            await redis_client.close()
        except Exception:
            pass

    logger.info(
        "✅ Bot encerrado."
    )


# ============================================================
# STARTUP / SHUTDOWN FASTAPI
# ============================================================

@app.on_event("startup")
async def startup_event():

    await inicializar()


@app.on_event("shutdown")
async def shutdown_event():

    await finalizar()
