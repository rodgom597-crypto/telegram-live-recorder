import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import redis.asyncio as redis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


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

# Quantas vezes seguidas uma conta pode falhar
# antes de ser considerada offline.
OFFLINE_CONFIRMATIONS_REQUIRED = 3

# Tentativas do /gravar
MANUAL_RECORD_ATTEMPTS = 5

# Tempo entre tentativas manuais
MANUAL_RETRY_DELAY = 10

# Tempo entre tentativas do monitor
MONITOR_RETRY_DELAY = 10

# Tempo máximo de uma execução do yt-dlp
YTDLP_TIMEOUT = 120


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

shutting_down = False


# ============================================================
# ESTADO
# ============================================================

monitored_users = {}

monitor_tasks = {}

recording_tasks = {}

recording_processes = {}

offline_confirmations = {}

recording_locks = {}


# ============================================================
# REDIS
# ============================================================

async def conectar_redis():

    global redis_client

    if not REDIS_URL:
        logger.warning(
            "⚠️ REDIS_URL não configurado."
        )
        return False

    try:

        redis_client = redis.from_url(
            REDIS_URL,
            decode_responses=True
        )

        await redis_client.ping()

        logger.info(
            "✅ Conectado ao Render Key Value."
        )

        return True

    except Exception as e:

        logger.error(
            f"❌ Erro ao conectar ao Redis: {e}"
        )

        redis_client = None

        return False


async def salvar_monitorados():

    if redis_client is None:

        logger.warning(
            "⚠️ Redis não disponível. "
            "Monitorados não foram salvos."
        )

        return

    try:

        dados = {}

        for username, info in monitored_users.items():

            dados[username] = {
                "live": bool(
                    info.get("live", False)
                )
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
            f"❌ Erro ao salvar monitorados: {e}"
        )


async def carregar_monitorados():

    global monitored_users

    if redis_client is None:
        return

    try:

        dados = await redis_client.get(
            "monitorados"
        )

        if not dados:

            logger.info(
                "📭 Nenhuma conta monitorada salva."
            )

            return

        carregados = json.loads(dados)

        monitored_users = {}

        for username, info in carregados.items():

            monitored_users[username] = {
                "live": bool(
                    info.get("live", False)
                ),
                "recording": False
            }

            offline_confirmations[
                username
            ] = 0

        logger.info(
            f"📂 {len(monitored_users)} conta(s) "
            f"carregada(s) do Redis."
        )

    except Exception as e:

        logger.error(
            f"❌ Erro ao carregar monitorados: {e}"
        )


# ============================================================
# UTILITÁRIOS
# ============================================================

def normalizar_usuario(username):

    username = username.strip()

    if username.startswith("@"):
        username = username[1:]

    if username.startswith(
        "https://www.tiktok.com/@"
    ):
        username = username.split(
            "@",
            1
        )[1]

    username = username.split(
        "/",
        1
    )[0]

    return username.lower()


def nome_seguro(username):

    return re.sub(
        r"[^a-zA-Z0-9_.-]",
        "_",
        username
    )


def url_tiktok(username):

    return (
        f"https://www.tiktok.com/"
        f"@{username}/live"
    )


async def enviar_mensagem(
    chat_id,
    texto
):

    if telegram_app is None:
        return

    try:

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=texto
        )

    except Exception as e:

        logger.error(
            f"Erro enviando mensagem: {e}"
        )


def encontrar_arquivos(username):

    prefixo = nome_seguro(
        username
    )

    arquivos = []

    for extensao in (
        "*.flv",
        "*.mp4"
    ):

        arquivos.extend(
            GRAVACOES_DIR.glob(
                extensao
            )
        )

    arquivos = [
        arquivo
        for arquivo in arquivos
        if prefixo in arquivo.name
    ]

    return sorted(
        arquivos,
        key=lambda x: (
            x.stat().st_mtime
            if x.exists()
            else 0
        ),
        reverse=True
    )


# ============================================================
# DETECÇÃO AUXILIAR
# ============================================================

async def verificar_live(username):

    """
    Essa função NÃO toma uma decisão definitiva de offline.

    True:
        live confirmada.

    None:
        resultado inconclusivo.

    O TikTok/yt-dlp pode informar falsamente
    "not currently live", portanto essa resposta
    nunca será tratada como offline imediato.
    """

    username = normalizar_usuario(
        username
    )

    url = url_tiktok(
        username
    )

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

        processo = (
            await asyncio.create_subprocess_exec(
                *comando,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
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

        texto = (
            f"{saida}\n{erro}"
        ).lower()

        # ----------------------------------------------------
        # JSON
        # ----------------------------------------------------

        for linha in saida.splitlines():

            linha = linha.strip()

            if not linha.startswith("{"):
                continue

            try:

                dados = json.loads(
                    linha
                )

                if dados.get(
                    "is_live"
                ) is True:

                    return True

            except Exception:
                pass

        # ----------------------------------------------------
        # CONFIRMAÇÃO ALTERNATIVA
        # ----------------------------------------------------

        if '"is_live": true' in texto:
            return True

        if '"islive": true' in texto:
            return True

        # ----------------------------------------------------
        # FALSO NEGATIVO
        # ----------------------------------------------------

        mensagens_falso_negativo = [
            "not currently live",
            "channel is not currently live",
            "the channel is not currently live",
            "user is not live",
            "the user is not live"
        ]

        for mensagem in mensagens_falso_negativo:

            if mensagem in texto:

                logger.info(
                    f"[{username}] yt-dlp informou "
                    f"que não está ao vivo "
                    f"(resultado inconclusivo)."
                )

                return None

        # ----------------------------------------------------
        # QUALQUER OUTRO ERRO
        # ----------------------------------------------------

        if processo.returncode != 0:

            logger.info(
                f"[{username}] Verificação "
                f"inconclusiva. Código: "
                f"{processo.returncode}"
            )

            return None

        return None

    except asyncio.TimeoutError:

        logger.warning(
            f"[{username}] Timeout na verificação."
        )

        if processo:

            try:
                processo.kill()
            except Exception:
                pass

        return None

    except asyncio.CancelledError:

        if processo:

            try:
                processo.kill()
            except Exception:
                pass

        raise

    except Exception as e:

        logger.error(
            f"[{username}] Erro verificando live: {e}"
        )

        return None


# ============================================================
# EXECUTAR YT-DLP
# ============================================================

async def executar_yt_dlp(
    username
):

    username = normalizar_usuario(
        username
    )

    url = url_tiktok(
        username
    )

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    arquivo = (
        GRAVACOES_DIR
        / f"{nome_seguro(username)}_"
          f"{timestamp}.flv"
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

        processo = (
            await asyncio.create_subprocess_exec(
                *comando,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
        )

        recording_processes[
            username
        ] = processo

        while True:

            linha = (
                await processo.stdout.readline()
            )

            if not linha:
                break

            texto = linha.decode(
                "utf-8",
                errors="ignore"
            ).strip()

            if texto:

                linhas.append(
                    texto
                )

                logger.info(
                    f"[{username}] {texto}"
                )

        codigo = await processo.wait()

        return (
            codigo,
            arquivo,
            "\n".join(linhas)
        )

    except asyncio.CancelledError:

        if processo:

            try:
                processo.kill()
            except Exception:
                pass

        raise

    except Exception as e:

        logger.error(
            f"[{username}] Erro no yt-dlp: {e}"
        )

        return (
            -1,
            arquivo,
            str(e)
        )

    finally:

        recording_processes.pop(
            username,
            None
        )


# ============================================================
# CONVERSÃO FLV -> MP4
# ============================================================

async def converter_para_mp4(
    arquivo_flv
):

    if not arquivo_flv.exists():
        return None

    arquivo_mp4 = (
        arquivo_flv.with_suffix(
            ".mp4"
        )
    )

    logger.info(
        f"[{arquivo_flv.stem}] "
        f"Convertendo para MP4."
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

        processo = (
            await asyncio.create_subprocess_exec(
                *comando,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
        )

        await processo.communicate()

        if (
            processo.returncode == 0
            and arquivo_mp4.exists()
        ):

            logger.info(
                f"[{arquivo_flv.stem}] "
                f"MP4 criado."
            )

            try:
                arquivo_flv.unlink()
            except Exception:
                pass

            return arquivo_mp4

    except asyncio.CancelledError:
        raise

    except Exception as e:

        logger.error(
            f"[{arquivo_flv.stem}] "
            f"Erro FFmpeg: {e}"
        )

    # --------------------------------------------------------
    # FALLBACK
    # --------------------------------------------------------

    logger.warning(
        f"[{arquivo_flv.stem}] "
        f"Tentando conversão alternativa."
    )

    comando = [
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

        processo = (
            await asyncio.create_subprocess_exec(
                *comando,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
        )

        await processo.communicate()

        if (
            processo.returncode == 0
            and arquivo_mp4.exists()
        ):

            try:
                arquivo_flv.unlink()
            except Exception:
                pass

            return arquivo_mp4

    except asyncio.CancelledError:
        raise

    except Exception as e:

        logger.error(
            f"[{arquivo_flv.stem}] "
            f"Fallback falhou: {e}"
        )

    return None


# ============================================================
# GRAVAÇÃO MANUAL
# ============================================================

async def gravar_manual(
    username,
    chat_id
):

    username = normalizar_usuario(
        username
    )

    if username not in recording_locks:

        recording_locks[
            username
        ] = asyncio.Lock()

    lock = recording_locks[
        username
    ]

    if lock.locked():

        await enviar_mensagem(
            chat_id,
            f"⚠️ Já existe uma gravação "
            f"em andamento para @{username}."
        )

        return

    async with lock:

        logger.info(
            f"[{username}] "
            f"Iniciando sistema de gravação."
        )

        tentativas = (
            MANUAL_RECORD_ATTEMPTS
        )

        for tentativa in range(
            1,
            tentativas + 1
        ):

            if shutting_down:
                return

            if tentativa == 1:

                await enviar_mensagem(
                    chat_id,
                    f"🎥 Iniciando gravação "
                    f"de @{username}..."
                )

            else:

                await enviar_mensagem(
                    chat_id,
                    f"🔄 Tentativa "
                    f"{tentativa}/{tentativas} "
                    f"para @{username}..."
                )

            (
                codigo,
                arquivo,
                log_gravacao
            ) = await executar_yt_dlp(
                username
            )

            # ------------------------------------------------
            # ARQUIVO ENCONTRADO
            # ------------------------------------------------

            arquivo_gravado = None

            if arquivo.exists():

                arquivo_gravado = arquivo

            else:

                arquivos = encontrar_arquivos(
                    username
                )

                if arquivos:

                    arquivo_gravado = (
                        arquivos[0]
                    )

            if arquivo_gravado:

                tamanho = (
                    arquivo_gravado.stat().st_size
                )

                logger.info(
                    f"[{username}] "
                    f"Arquivo encontrado: "
                    f"{arquivo_gravado.name} "
                    f"({tamanho / 1024 / 1024:.2f} MB)"
                )

                arquivo_final = (
                    arquivo_gravado
                )

                if (
                    arquivo_gravado.suffix.lower()
                    == ".flv"
                ):

                    convertido = (
                        await converter_para_mp4(
                            arquivo_gravado
                        )
                    )

                    if convertido:
                        arquivo_final = convertido

                await enviar_mensagem(
                    chat_id,
                    f"✅ Gravação finalizada!\n\n"
                    f"👤 @{username}\n"
                    f"📁 {arquivo_final.name}"
                )

                return

            # ------------------------------------------------
            # NÃO GEROU ARQUIVO
            # ------------------------------------------------

            texto = (
                log_gravacao.lower()
            )

            if (
                "not currently live"
                in texto
            ):

                logger.warning(
                    f"[{username}] "
                    f"yt-dlp informou que não "
                    f"está ao vivo. "
                    f"Não vamos confiar nessa "
                    f"resposta."
                )

            if tentativa < tentativas:

                await asyncio.sleep(
                    MANUAL_RETRY_DELAY
                )

                continue

            await enviar_mensagem(
                chat_id,
                f"❌ Não foi possível iniciar "
                f"a gravação de @{username}.\n\n"
                f"O TikTok não entregou o "
                f"stream após {tentativas} tentativas."
            )


# ============================================================
# GRAVAÇÃO AUTOMÁTICA
# ============================================================

async def gravar_automatico(
    username
):

    username = normalizar_usuario(
        username
    )

    if username not in recording_locks:

        recording_locks[
            username
        ] = asyncio.Lock()

    lock = recording_locks[
        username
    ]

    if lock.locked():

        logger.info(
            f"[{username}] "
            f"Gravação automática "
            f"já está em andamento."
        )

        return

    async with lock:

        logger.info(
            f"[{username}] "
            f"Iniciando gravação automática."
        )

        while (
            not shutting_down
            and username in monitored_users
        ):

            (
                codigo,
                arquivo,
                log_gravacao
            ) = await executar_yt_dlp(
                username
            )

            if shutting_down:
                return

            arquivo_gravado = None

            if arquivo.exists():

                arquivo_gravado = arquivo

            else:

                arquivos = encontrar_arquivos(
                    username
                )

                if arquivos:

                    arquivo_gravado = (
                        arquivos[0]
                    )

            # ------------------------------------------------
            # CONSEGUIU GRAVAR
            # ------------------------------------------------

            if arquivo_gravado:

                logger.info(
                    f"[{username}] "
                    f"Gravação automática "
                    f"produziu arquivo."
                )

                arquivo_final = (
                    arquivo_gravado
                )

                if (
                    arquivo_gravado.suffix.lower()
                    == ".flv"
                ):

                    convertido = (
                        await converter_para_mp4(
                            arquivo_gravado
                        )
                    )

                    if convertido:

                        arquivo_final = (
                            convertido
                        )

                # Depois que uma sessão termina,
                # NÃO assumimos que a live acabou.
                #
                # O monitor fará nova verificação.
                logger.info(
                    f"[{username}] "
                    f"Sessão de gravação encerrada. "
                    f"Voltando para monitoramento."
                )

                await asyncio.sleep(
                    MONITOR_RETRY_DELAY
                )

                continue

            # ------------------------------------------------
            # NÃO CONSEGUIU GRAVAR
            # ------------------------------------------------

            logger.warning(
                f"[{username}] "
                f"yt-dlp não gerou arquivo."
            )

            texto = (
                log_gravacao.lower()
            )

            if (
                "not currently live"
                in texto
            ):

                logger.info(
                    f"[{username}] "
                    f"Resposta de offline "
                    f"tratada como inconclusiva."
                )

            await asyncio.sleep(
                MONITOR_RETRY_DELAY
            )


# ============================================================
# MONITORAMENTO
# ============================================================

async def monitorar_usuario(
    username
):

    username = normalizar_usuario(
        username
    )

    logger.info(
        f"[{username}] "
        f"Monitoramento iniciado."
    )

    offline_confirmations[
        username
    ] = 0

    try:

        while (
            not shutting_down
            and username in monitored_users
        ):

            resultado = await verificar_live(
                username
            )

            if shutting_down:
                break

            # =================================================
            # LIVE CONFIRMADA
            # =================================================

            if resultado is True:

                offline_confirmations[
                    username
                ] = 0

                estava_ao_vivo = (
                    monitored_users[
                        username
                    ].get(
                        "live",
                        False
                    )
                )

                monitored_users[
                    username
                ]["live"] = True

                if not estava_ao_vivo:

                    logger.info(
                        f"[{username}] "
                        f"🔴 LIVE DETECTADA."
                    )

                    # Inicia gravação automática
                    if (
                        username
                        not in recording_tasks
                        or recording_tasks[
                            username
                        ].done()
                    ):

                        task = asyncio.create_task(
                            gravar_automatico(
                                username
                            )
                        )

                        recording_tasks[
                            username
                        ] = task

            # =================================================
            # RESULTADO INCONCLUSIVO
            # =================================================

            else:

                offline_confirmations[
                    username
                ] = (
                    offline_confirmations.get(
                        username,
                        0
                    ) + 1
                )

                contador = (
                    offline_confirmations[
                        username
                    ]
                )

                logger.info(
                    f"[{username}] "
                    f"Verificação inconclusiva/offline "
                    f"{contador}/"
                    f"{OFFLINE_CONFIRMATIONS_REQUIRED}."
                )

                if (
                    contador
                    >= OFFLINE_CONFIRMATIONS_REQUIRED
                ):

                    estava_ao_vivo = (
                        monitored_users[
                            username
                        ].get(
                            "live",
                            False
                        )
                    )

                    if estava_ao_vivo:

                        logger.info(
                            f"[{username}] "
                            f"🔵 Live considerada "
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
            f"[{username}] "
            f"Monitoramento cancelado."
        )

        raise

    finally:

        logger.info(
            f"[{username}] "
            f"Monitoramento encerrado."
        )


# ============================================================
# RESTAURAR MONITORES
# ============================================================

async def iniciar_monitores_salvos():

    if not monitored_users:

        logger.info(
            "Nenhum monitor para restaurar."
        )

        return

    for username in list(
        monitored_users.keys()
    ):

        if shutting_down:
            break

        if username in monitor_tasks:
            continue

        task = asyncio.create_task(
            monitorar_usuario(
                username
            )
        )

        monitor_tasks[
            username
        ] = task

        logger.info(
            f"🔄 Monitor restaurado: "
            f"@{username}"
        )


# ============================================================
# TELEGRAM /START
# ============================================================

async def cmd_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "🤖 Bot de gravação TikTok LIVE\n\n"
        "/gravar usuario - grava uma live agora\n"
        "/monitorar usuario - monitora automaticamente\n"
        "/desmonitorar usuario - para o monitoramento\n"
        "/monitorados - lista contas monitoradas"
    )


# ============================================================
# /GRAVAR
# ============================================================

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

    chat_id = (
        update.effective_chat.id
    )

    await update.message.reply_text(
        f"🎥 Solicitação de gravação "
        f"enviada para @{username}."
    )

    task = asyncio.create_task(
        gravar_manual(
            username,
            chat_id
        )
    )

    # Não usamos esse task no monitoramento.
    # É uma gravação manual independente.


# ============================================================
# /MONITORAR
# ============================================================

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
            f"👁️ @{username} "
            f"já está sendo monitorado."
        )

        return

    monitored_users[
        username
    ] = {
        "live": False,
        "recording": False
    }

    offline_confirmations[
        username
    ] = 0

    task = asyncio.create_task(
        monitorar_usuario(
            username
        )
    )

    monitor_tasks[
        username
    ] = task

    await salvar_monitorados()

    await update.message.reply_text(
        f"👁️ Monitoramento ativado "
        f"para @{username}.\n\n"
        f"💾 Salvo permanentemente.\n"
        f"🔄 Verificação a cada "
        f"{MONITOR_INTERVAL} segundos."
    )


# ============================================================
# /DESMONITORAR
# ============================================================

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
            f"⚠️ @{username} "
            f"não está sendo monitorado."
        )

        return

    # --------------------------------------------------------
    # Remove primeiro do dicionário.
    # Isso impede que o monitor volte.
    # --------------------------------------------------------

    monitored_users.pop(
        username,
        None
    )

    offline_confirmations.pop(
        username,
        None
    )

    # --------------------------------------------------------
    # Cancela monitor
    # --------------------------------------------------------

    task = monitor_tasks.pop(
        username,
        None
    )

    if task:

        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(
                f"Erro encerrando monitor "
                f"{username}: {e}"
            )

    # --------------------------------------------------------
    # Cancela gravação automática
    # --------------------------------------------------------

    recording_task = (
        recording_tasks.pop(
            username,
            None
        )
    )

    if recording_task:

        recording_task.cancel()

        try:
            await recording_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    # --------------------------------------------------------
    # Mata processo yt-dlp se existir
    # --------------------------------------------------------

    processo = (
        recording_processes.get(
            username
        )
    )

    if processo:

        try:
            processo.kill()
        except Exception:
            pass

    recording_processes.pop(
        username,
        None
    )

    await salvar_monitorados()

    await update.message.reply_text(
        f"🛑 Monitoramento desativado "
        f"para @{username}.\n"
        f"💾 Removido do armazenamento permanente."
    )


# ============================================================
# /MONITORADOS
# ============================================================

async def cmd_monitorados(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

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

        live = (
            monitored_users[
                username
            ].get(
                "live",
                False
            )
        )

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
# WEBHOOK
# ============================================================

@app.post(
    "/telegram/webhook"
)
async def telegram_webhook(
    request: Request
):

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
            {
                "ok": True
            }
        )

    except Exception as e:

        logger.error(
            f"Erro no webhook: {e}"
        )

        return JSONResponse(
            {
                "ok": False,
                "error": str(e)
            },
            status_code=500
        )


# ============================================================
# HOME
# ============================================================

@app.get("/")
async def home():

    return {
        "status": "online",
        "service": "TikTok Live Recorder",
        "monitored": len(
            monitored_users
        )
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():

    return {
        "status": "ok",
        "telegram": (
            telegram_app is not None
        ),
        "redis": (
            redis_client is not None
        ),
        "monitored": len(
            monitored_users
        )
    }


# ============================================================
# STARTUP
# ============================================================

async def inicializar():

    global telegram_app

    logger.info(
        "🚀 Iniciando bot..."
    )

    await conectar_redis()

    await carregar_monitorados()

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

    try:

        await telegram_app.bot.set_my_commands([
            (
                "start",
                "Iniciar bot"
            ),
            (
                "gravar",
                "Gravar uma live"
            ),
            (
                "monitorar",
                "Monitorar uma conta"
            ),
            (
                "desmonitorar",
                "Parar monitoramento"
            ),
            (
                "monitorados",
                "Listar monitorados"
            )
        ])

        logger.info(
            "Comandos do Telegram configurados."
        )

    except Exception as e:

        logger.error(
            f"Erro configurando comandos: {e}"
        )

    try:

        await telegram_app.bot.set_webhook(
            url=WEBHOOK_URL
        )

        logger.info(
            f"Webhook configurado: "
            f"{WEBHOOK_URL}"
        )

    except Exception as e:

        logger.error(
            f"Erro configurando webhook: {e}"
        )

    await iniciar_monitores_salvos()

    logger.info(
        "🤖 Bot iniciado com sucesso."
    )


# ============================================================
# SHUTDOWN
# ============================================================

async def finalizar():

    global shutting_down

    if shutting_down:
        return

    shutting_down = True

    logger.info(
        "🛑 Encerrando bot..."
    )

    # --------------------------------------------------------
    # Salva estado
    # --------------------------------------------------------

    await salvar_monitorados()

    # --------------------------------------------------------
    # Cancela todos os monitores
    # --------------------------------------------------------

    tarefas_monitores = list(
        monitor_tasks.values()
    )

    for task in tarefas_monitores:

        if task and not task.done():

            task.cancel()

    # --------------------------------------------------------
    # Aguarda todos os monitores terminarem
    # --------------------------------------------------------

    if tarefas_monitores:

        await asyncio.gather(
            *tarefas_monitores,
            return_exceptions=True
        )

    monitor_tasks.clear()

    # --------------------------------------------------------
    # Cancela gravações automáticas
    # --------------------------------------------------------

    tarefas_gravacao = list(
        recording_tasks.values()
    )

    for task in tarefas_gravacao:

        if task and not task.done():

            task.cancel()

    if tarefas_gravacao:

        await asyncio.gather(
            *tarefas_gravacao,
            return_exceptions=True
        )

    recording_tasks.clear()

    # --------------------------------------------------------
    # Encerra processos yt-dlp
    # --------------------------------------------------------

    processos = list(
        recording_processes.items()
    )

    for username, processo in processos:

        try:

            logger.info(
                f"[{username}] "
                f"Encerrando processo yt-dlp."
            )

            processo.kill()

        except Exception:
            pass

    recording_processes.clear()

    # --------------------------------------------------------
    # Telegram
    # --------------------------------------------------------

    if telegram_app:

        try:
            await telegram_app.stop()
        except Exception as e:
            logger.error(
                f"Erro no Telegram stop: {e}"
            )

        try:
            await telegram_app.shutdown()
        except Exception as e:
            logger.error(
                f"Erro no Telegram shutdown: {e}"
            )

    # --------------------------------------------------------
    # Redis
    # --------------------------------------------------------

    if redis_client:

        try:
            await redis_client.aclose()
        except Exception as e:
            logger.error(
                f"Erro fechando Redis: {e}"
            )

    logger.info(
        "✅ Bot encerrado."
    )


# ============================================================
# EVENTOS FASTAPI
# ============================================================

@app.on_event(
    "startup"
)
async def startup_event():

    await inicializar()


@app.on_event(
    "shutdown"
)
async def shutdown_event():

    await finalizar()
