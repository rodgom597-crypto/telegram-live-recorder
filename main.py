import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime

import redis.asyncio as redis

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)


# ============================================================
# CONFIGURAÇÃO
# ============================================================

TOKEN = os.environ["BOT_TOKEN"]

RENDER_EXTERNAL_URL = os.environ.get(
    "RENDER_EXTERNAL_URL",
    ""
).rstrip("/")

REDIS_URL = os.environ.get(
    "REDIS_URL",
    ""
).strip()

OUTPUT_DIR = Path("/tmp/recordings")
OUTPUT_DIR.mkdir(
    parents=True,
    exist_ok=True
)

# Intervalo normal do monitoramento
MONITOR_INTERVAL = 30

# Tempo entre tentativas de recuperação
RECONNECT_SECONDS = 10

# Quantidade de verificações negativas consecutivas
# antes de considerar que realmente saiu do ar.
OFFLINE_CONFIRMATIONS = 3

# Intervalo entre tentativas de verificar uma LIVE
VERIFICATION_RETRY_SECONDS = 10

# Tentativas para confirmar uma resposta negativa
VERIFICATION_RETRIES = 3


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI()

telegram_app = None


# ============================================================
# REDIS / KEY VALUE
# ============================================================

redis_client = None

MONITORED_KEY = "tiktok:monitored_users"


# ============================================================
# ESTADOS EM MEMÓRIA
# ============================================================

# username -> processo yt-dlp
recordings = {}

# username -> informações do monitoramento
monitored_users = {}

# username -> asyncio.Task do monitor
monitor_tasks = {}

# username -> pedido explícito para parar
stop_requests = set()

# username -> quantidade de reconexões
reconnect_counts = {}


# ============================================================
# COMANDOS
# ============================================================

BOT_COMMANDS = [
    BotCommand(
        "start",
        "Iniciar o bot"
    ),

    BotCommand(
        "ajuda",
        "Mostrar comandos"
    ),

    BotCommand(
        "status",
        "Ver gravações ativas"
    ),

    BotCommand(
        "gravar",
        "Gravar uma live"
    ),

    BotCommand(
        "parar",
        "Parar gravações"
    ),

    BotCommand(
        "monitorar",
        "Monitorar uma conta"
    ),

    BotCommand(
        "desmonitorar",
        "Parar monitoramento"
    ),

    BotCommand(
        "monitorados",
        "Listar contas monitoradas"
    ),
]


# ============================================================
# UTILIDADES
# ============================================================

def normalizar_usuario(username: str) -> str:

    username = username.strip()

    username = username.replace(
        "@",
        ""
    )

    username = username.split()[0]

    return username.lower()


async def enviar_mensagem(
    chat_id,
    texto
):

    try:

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=texto
        )

    except Exception as e:

        logger.error(
            "Erro ao enviar mensagem para %s: %s",
            chat_id,
            e
        )


# ============================================================
# REDIS - CONEXÃO
# ============================================================

async def conectar_redis():

    global redis_client

    if not REDIS_URL:

        logger.warning(
            "⚠️ REDIS_URL não configurada."
        )

        logger.warning(
            "⚠️ Monitorados NÃO serão persistidos."
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
            "❌ Erro conectando ao Key Value: %s",
            e
        )

        redis_client = None

        return False


# ============================================================
# SALVAR MONITORADOS
# ============================================================

async def salvar_monitorados():

    if not redis_client:

        logger.warning(
            "Redis não disponível. "
            "Monitorados não foram salvos."
        )

        return False

    try:

        dados = {}

        for username, info in monitored_users.items():

            dados[username] = {
                "chat_id": info.get(
                    "chat_id"
                ),

                "live": info.get(
                    "live",
                    False
                ),

                "started_at": info.get(
                    "started_at"
                ),
            }

        await redis_client.set(
            MONITORED_KEY,
            json.dumps(
                dados,
                ensure_ascii=False
            )
        )

        logger.info(
            "💾 %d monitorado(s) salvo(s) no Key Value.",
            len(dados)
        )

        return True

    except Exception as e:

        logger.error(
            "Erro salvando monitorados: %s",
            e
        )

        return False


# ============================================================
# CARREGAR MONITORADOS
# ============================================================

async def carregar_monitorados():

    global monitored_users

    if not redis_client:

        logger.warning(
            "Redis não disponível. "
            "Nenhum monitorado foi carregado."
        )

        return

    try:

        dados = await redis_client.get(
            MONITORED_KEY
        )

        if not dados:

            logger.info(
                "📭 Nenhuma conta monitorada salva."
            )

            return

        lista = json.loads(
            dados
        )

        if not isinstance(
            lista,
            dict
        ):

            logger.warning(
                "Dados dos monitorados inválidos."
            )

            return

        monitored_users.clear()

        for username, info in lista.items():

            username = normalizar_usuario(
                username
            )

            monitored_users[username] = {
                "chat_id": info.get(
                    "chat_id"
                ),

                "live": False,

                "started_at": None,

                # Contador para evitar falso offline
                "offline_checks": 0,
            }

        logger.info(
            "📂 %d conta(s) monitorada(s) carregada(s).",
            len(monitored_users)
        )

    except Exception as e:

        logger.error(
            "Erro carregando monitorados: %s",
            e
        )


# ============================================================
# INICIAR MONITORES SALVOS
# ============================================================

async def iniciar_monitores_salvos():

    if not monitored_users:

        logger.info(
            "Nenhum monitor para restaurar."
        )

        return

    logger.info(
        "🔄 Restaurando monitoramentos..."
    )

    for username, info in list(
        monitored_users.items()
    ):

        chat_id = info.get(
            "chat_id"
        )

        if not chat_id:

            logger.warning(
                "[%s] Monitor sem chat_id. Ignorando.",
                username
            )

            continue

        if username in monitor_tasks:

            continue

        task = asyncio.create_task(
            monitorar_usuario(
                username,
                chat_id
            )
        )

        monitor_tasks[username] = task

        logger.info(
            "[%s] Monitor restaurado.",
            username
        )

    logger.info(
        "✅ Monitoramentos restaurados."
    )


# ============================================================
# VERIFICAR LIVE - BAIXO NÍVEL
# ============================================================

async def verificar_live_once(
    username: str
):
    """
    Retorna:

    True  = confirmou LIVE
    False = recebeu resposta de offline
    None  = não conseguiu confirmar
    """

    username = normalizar_usuario(
        username
    )

    url = (
        f"https://www.tiktok.com/"
        f"@{username}/live"
    )

    comando = [
        "yt-dlp",

        "--dump-json",

        "--skip-download",

        "--no-warnings",

        "--no-color",

        url,
    ]

    try:

        processo = await asyncio.create_subprocess_exec(
            *comando,

            stdout=asyncio.subprocess.PIPE,

            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await processo.communicate()

        texto = stdout.decode(
            "utf-8",
            errors="ignore"
        )

        erro = stderr.decode(
            "utf-8",
            errors="ignore"
        )

        linhas = [
            linha.strip()
            for linha in texto.splitlines()
            if linha.strip()
        ]

        for linha in reversed(
            linhas
        ):

            try:

                dados = json.loads(
                    linha
                )

                if not isinstance(
                    dados,
                    dict
                ):
                    continue

                # Alguns retornos do yt-dlp podem trazer
                # live_status em vez de is_live.
                live_status = dados.get(
                    "live_status"
                )

                if live_status == "is_live":

                    logger.info(
                        "[%s] VERIFICAÇÃO: AO VIVO "
                        "(live_status)",
                        username
                    )

                    return True

                is_live = dados.get(
                    "is_live"
                )

                if is_live is True:

                    logger.info(
                        "[%s] VERIFICAÇÃO: AO VIVO",
                        username
                    )

                    return True

                if is_live is False:

                    logger.info(
                        "[%s] VERIFICAÇÃO: OFFLINE "
                        "(is_live=False)",
                        username
                    )

                    return False

                if live_status in (
                    "post_live",
                    "was_live",
                    "not_live",
                    "is_upcoming"
                ):

                    logger.info(
                        "[%s] VERIFICAÇÃO: OFFLINE "
                        "(live_status=%s)",
                        username,
                        live_status
                    )

                    return False

            except json.JSONDecodeError:

                continue

        erro_lower = erro.lower()

        if "not currently live" in erro_lower:

            logger.warning(
                "[%s] yt-dlp informou "
                "'not currently live'.",
                username
            )

            # IMPORTANTE:
            # Não tratamos isso como certeza absoluta.
            # O TikTok/yt-dlp possui histórico desse falso negativo.
            return False

        if processo.returncode != 0:

            logger.warning(
                "[%s] yt-dlp terminou com código %s "
                "sem confirmação de LIVE.",
                username,
                processo.returncode
            )

            return None

        logger.warning(
            "[%s] yt-dlp retornou sem confirmação.",
            username
        )

        return None

    except Exception as e:

        logger.error(
            "[%s] Erro verificando live: %s",
            username,
            e
        )

        return None


# ============================================================
# VERIFICAR LIVE - COM RETENTATIVAS
# ============================================================

async def verificar_live(
    username: str,
    tentativas: int = VERIFICATION_RETRIES
):
    """
    Faz várias verificações antes de aceitar
    uma resposta negativa.

    Retorna:

    True  = confirmou ao vivo
    False = confirmou offline após tentativas
    None  = não conseguiu determinar
    """

    username = normalizar_usuario(
        username
    )

    resultado_indefinido = False

    for tentativa in range(
        1,
        tentativas + 1
    ):

        resultado = await verificar_live_once(
            username
        )

        if resultado is True:

            return True

        if resultado is None:

            resultado_indefinido = True

        if tentativa < tentativas:

            logger.info(
                "[%s] Verificação negativa/indefinida "
                "(%s/%s). Nova tentativa em %ss.",
                username,
                tentativa,
                tentativas,
                VERIFICATION_RETRY_SECONDS
            )

            await asyncio.sleep(
                VERIFICATION_RETRY_SECONDS
            )

    if resultado_indefinido:

        logger.warning(
            "[%s] Não foi possível determinar "
            "com segurança se está ao vivo.",
            username
        )

        return None

    logger.warning(
        "[%s] OFFLINE confirmado após %s tentativas.",
        username,
        tentativas
    )

    return False


# ============================================================
# LOCALIZAR ARQUIVOS
# ============================================================

def procurar_arquivos(
    username: str
):

    username = normalizar_usuario(
        username
    )

    arquivos = []

    for arquivo in OUTPUT_DIR.glob(
        "*.flv"
    ):

        if username in arquivo.name.lower():

            arquivos.append(
                arquivo
            )

    arquivos.sort(
        key=lambda x: x.stat().st_mtime
    )

    return arquivos


# ============================================================
# CONVERSÃO FLV -> MP4
# ============================================================

async def converter_para_mp4(
    username: str,
    arquivos
):

    if not arquivos:

        return None

    username = normalizar_usuario(
        username
    )

    mp4_final = OUTPUT_DIR / (
        f"{username}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        f".mp4"
    )

    # ========================================================
    # UM FLV
    # ========================================================

    if len(arquivos) == 1:

        flv = arquivos[0]

        comando = [
            "ffmpeg",

            "-y",

            "-fflags",
            "+genpts+discardcorrupt",

            "-err_detect",
            "ignore_err",

            "-i",
            str(flv),

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

            str(mp4_final),
        ]

        logger.info(
            "[%s] Reencodando FLV para MP4.",
            username
        )

        processo = await asyncio.create_subprocess_exec(
            *comando,

            stdout=asyncio.subprocess.PIPE,

            stderr=asyncio.subprocess.PIPE,
        )

        _, stderr = await processo.communicate()

        if (
            processo.returncode == 0
            and mp4_final.exists()
        ):

            logger.info(
                "[%s] MP4 criado com sucesso.",
                username
            )

            return mp4_final

        logger.warning(
            "[%s] Conversão principal falhou. "
            "Tentando recuperação.",
            username
        )

        if mp4_final.exists():

            mp4_final.unlink()

        comando = [
            "ffmpeg",

            "-y",

            "-fflags",
            "+genpts+discardcorrupt",

            "-err_detect",
            "ignore_err",

            "-i",
            str(flv),

            "-map",
            "0:v:0?",

            "-map",
            "0:a:0?",

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
            "96k",

            "-movflags",
            "+faststart",

            str(mp4_final),
        ]

        processo = await asyncio.create_subprocess_exec(
            *comando,

            stdout=asyncio.subprocess.PIPE,

            stderr=asyncio.subprocess.PIPE,
        )

        _, stderr = await processo.communicate()

        if (
            processo.returncode == 0
            and mp4_final.exists()
        ):

            logger.info(
                "[%s] MP4 criado pelo modo de recuperação.",
                username
            )

            return mp4_final

        logger.error(
            "[%s] Falha na conversão: %s",
            username,
            stderr.decode(
                "utf-8",
                errors="ignore"
            )[-5000:]
        )

        return None

    # ========================================================
    # VÁRIOS FLV
    # ========================================================

    lista = OUTPUT_DIR / (
        f"concat_{username}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        f".txt"
    )

    try:

        with open(
            lista,
            "w",
            encoding="utf-8"
        ) as f:

            for arquivo in arquivos:

                caminho = str(
                    arquivo.resolve()
                ).replace(
                    "'",
                    "'\\''"
                )

                f.write(
                    f"file '{caminho}'\n"
                )

        comando = [
            "ffmpeg",

            "-y",

            "-fflags",
            "+genpts+discardcorrupt",

            "-err_detect",
            "ignore_err",

            "-f",
            "concat",

            "-safe",
            "0",

            "-i",
            str(lista),

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

            str(mp4_final),
        ]

        logger.info(
            "[%s] Juntando %d arquivos FLV.",
            username,
            len(arquivos)
        )

        processo = await asyncio.create_subprocess_exec(
            *comando,

            stdout=asyncio.subprocess.PIPE,

            stderr=asyncio.subprocess.PIPE,
        )

        _, stderr = await processo.communicate()

        if (
            processo.returncode == 0
            and mp4_final.exists()
        ):

            return mp4_final

        logger.warning(
            "[%s] Concatenação falhou. "
            "Tentando recuperação.",
            username
        )

        if mp4_final.exists():

            mp4_final.unlink()

        comando = [
            "ffmpeg",

            "-y",

            "-fflags",
            "+genpts+discardcorrupt",

            "-err_detect",
            "ignore_err",

            "-f",
            "concat",

            "-safe",
            "0",

            "-i",
            str(lista),

            "-map",
            "0:v:0?",

            "-map",
            "0:a:0?",

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
            "96k",

            "-movflags",
            "+faststart",

            str(mp4_final),
        ]

        processo = await asyncio.create_subprocess_exec(
            *comando,

            stdout=asyncio.subprocess.PIPE,

            stderr=asyncio.subprocess.PIPE,
        )

        _, stderr = await processo.communicate()

        if (
            processo.returncode == 0
            and mp4_final.exists()
        ):

            return mp4_final

        logger.error(
            "[%s] Falha juntando gravações: %s",
            username,
            stderr.decode(
                "utf-8",
                errors="ignore"
            )[-5000:]
        )

        return None

    finally:

        if lista.exists():

            lista.unlink()


# ============================================================
# FINALIZAR GRAVAÇÃO
# ============================================================

async def finalizar_gravacao(
    username: str,
    chat_id
):

    username = normalizar_usuario(
        username
    )

    arquivos = procurar_arquivos(
        username
    )

    if not arquivos:

        await enviar_mensagem(
            chat_id,
            f"⚠️ Nenhum arquivo encontrado para "
            f"@{username}."
        )

        return

    await enviar_mensagem(
        chat_id,
        f"⏹️ Gravação de @{username} encerrada.\n"
        f"📦 Preparando o MP4..."
    )

    mp4 = await converter_para_mp4(
        username,
        arquivos
    )

    if not mp4:

        await enviar_mensagem(
            chat_id,
            f"❌ Não foi possível converter "
            f"@{username} para MP4."
        )

        return

    try:

        tamanho_mb = (
            mp4.stat().st_size
            / 1024
            / 1024
        )

        logger.info(
            "[%s] MP4 pronto: %.2f MB",
            username,
            tamanho_mb
        )

        with mp4.open(
            "rb"
        ) as video:

            await telegram_app.bot.send_video(
                chat_id=chat_id,

                video=video,

                caption=(
                    f"🎥 Gravação finalizada\n"
                    f"👤 @{username}\n"
                    f"📦 {tamanho_mb:.2f} MB"
                ),

                supports_streaming=True,
            )

    except Exception as e:

        logger.warning(
            "[%s] send_video falhou: %s",
            username,
            e
        )

        try:

            with mp4.open(
                "rb"
            ) as documento:

                await telegram_app.bot.send_document(
                    chat_id=chat_id,

                    document=documento,

                    caption=(
                        f"🎥 Gravação de @{username}"
                    ),
                )

        except Exception as e2:

            logger.error(
                "[%s] send_document também falhou: %s",
                username,
                e2
            )

            await enviar_mensagem(
                chat_id,
                f"❌ Erro ao enviar o vídeo: {e2}"
            )

    # ========================================================
    # LIMPEZA
    # ========================================================

    try:

        if mp4.exists():

            mp4.unlink()

        for arquivo in arquivos:

            if arquivo.exists():

                arquivo.unlink()

    except Exception as e:

        logger.error(
            "[%s] Erro limpando arquivos: %s",
            username,
            e
        )


# ============================================================
# GRAVAÇÃO
# ============================================================

async def record_live(
    username: str,
    chat_id
):

    username = normalizar_usuario(
        username
    )

    stop_requests.discard(
        username
    )

    reconnect_counts[username] = 0

    recordings[username] = None

    logger.info(
        "[%s] Iniciando sistema de gravação.",
        username
    )

    await enviar_mensagem(
        chat_id,
        f"🔴 Iniciando gravação de @{username}..."
    )

    try:

        while True:

            # =================================================
            # PARADA MANUAL
            # =================================================

            if username in stop_requests:

                logger.info(
                    "[%s] Parada manual solicitada.",
                    username
                )

                break

            # =================================================
            # URL
            # =================================================

            url = (
                f"https://www.tiktok.com/"
                f"@{username}/live"
            )

            timestamp = datetime.now().strftime(
                "%Y%m%d_%H%M%S"
            )

            arquivo = OUTPUT_DIR / (
                f"{username}_{timestamp}.flv"
            )

            # =================================================
            # YT-DLP
            # =================================================

            comando = [
                "yt-dlp",

                "-f",
                "best[ext=flv]/best",

                "--no-part",

                "--no-continue",

                "--no-overwrites",

                "--no-warnings",

                "--newline",

                "-o",
                str(arquivo),

                url,
            ]

            logger.info(
                "[%s] Iniciando yt-dlp.",
                username
            )

            try:

                processo = await asyncio.create_subprocess_exec(
                    *comando,

                    stdout=asyncio.subprocess.PIPE,

                    stderr=asyncio.subprocess.STDOUT,
                )

                recordings[username] = processo

                saida = []

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

                        saida.append(
                            texto
                        )

                        logger.info(
                            "[%s] %s",
                            username,
                            texto
                        )

                codigo = await processo.wait()

                recordings.pop(
                    username,
                    None
                )

                texto_completo = "\n".join(
                    saida[-30:]
                )

                logger.info(
                    "[%s] yt-dlp terminou. Código: %s",
                    username,
                    codigo
                )

            except asyncio.CancelledError:

                logger.info(
                    "[%s] Tarefa cancelada.",
                    username
                )

                try:

                    if recordings.get(
                        username
                    ):

                        recordings[
                            username
                        ].terminate()

                except Exception:

                    pass

                raise

            except Exception as e:

                logger.error(
                    "[%s] Erro executando yt-dlp: %s",
                    username,
                    e
                )

                recordings.pop(
                    username,
                    None
                )

                texto_completo = str(e)

                codigo = -1

            # =================================================
            # PARADA MANUAL
            # =================================================

            if username in stop_requests:

                logger.info(
                    "[%s] Não reconectar: parada manual.",
                    username
                )

                break

            # =================================================
            # ARQUIVO GERADO
            # =================================================

            arquivo_gerado = (
                arquivo.exists()
                and arquivo.stat().st_size > 0
            )

            # =================================================
            # OFFLINE DO YT-DLP
            # =================================================

            erro_offline = (
                "not currently live"
                in texto_completo.lower()
            )

            arquivos_existentes = (
                procurar_arquivos(
                    username
                )
            )

            # =================================================
            # NÃO ENCERRA IMEDIATAMENTE
            #
            # O TikTok/yt-dlp pode informar falsamente
            # que a LIVE não está ativa.
            # =================================================

            if (
                erro_offline
                and not arquivo_gerado
                and not arquivos_existentes
            ):

                logger.warning(
                    "[%s] yt-dlp informou offline. "
                    "Não vamos desistir imediatamente.",
                    username
                )

                confirmou_offline = True

                for tentativa in range(
                    1,
                    VERIFICATION_RETRIES + 1
                ):

                    if username in stop_requests:

                        break

                    resultado = await verificar_live_once(
                        username
                    )

                    if resultado is True:

                        confirmou_offline = False

                        logger.info(
                            "[%s] LIVE confirmada após "
                            "falso negativo do yt-dlp.",
                            username
                        )

                        break

                    if tentativa < VERIFICATION_RETRIES:

                        logger.info(
                            "[%s] Ainda não confirmou LIVE. "
                            "Nova tentativa %s/%s em %ss.",
                            username,
                            tentativa + 1,
                            VERIFICATION_RETRIES,
                            VERIFICATION_RETRY_SECONDS
                        )

                        await asyncio.sleep(
                            VERIFICATION_RETRY_SECONDS
                        )

                if confirmou_offline:

                    await enviar_mensagem(
                        chat_id,
                        f"⚠️ Não consegui acessar a LIVE de "
                        f"@{username} após várias tentativas.\n"
                        f"🔄 Se ela estiver realmente ao vivo, "
                        f"tente novamente em alguns segundos."
                    )

                    break

            # =================================================
            # LOG DO ARQUIVO
            # =================================================

            if arquivo_gerado:

                logger.info(
                    "[%s] Arquivo FLV gerado.",
                    username
                )

            # =================================================
            # VERIFICAR LIVE
            # =================================================

            ainda_ativa = None

            if username in monitored_users:

                info = monitored_users.get(
                    username,
                    {}
                )

                if info.get(
                    "live",
                    False
                ):

                    ainda_ativa = True

            if ainda_ativa is not True:

                ainda_ativa = await verificar_live(
                    username
                )

            # =================================================
            # RESULTADO INDEFINIDO
            # =================================================

            if ainda_ativa is None:

                logger.warning(
                    "[%s] Não foi possível confirmar "
                    "se a LIVE terminou. "
                    "Não encerrando definitivamente.",
                    username
                )

                await asyncio.sleep(
                    RECONNECT_SECONDS
                )

                continue

            # =================================================
            # LIVE TERMINOU
            # =================================================

            if not ainda_ativa:

                # Para uma gravação manual, fazemos uma
                # última confirmação antes de encerrar.

                logger.warning(
                    "[%s] LIVE aparentemente offline. "
                    "Fazendo confirmação final.",
                    username
                )

                await asyncio.sleep(
                    VERIFICATION_RETRY_SECONDS
                )

                confirmacao_final = await verificar_live(
                    username,
                    tentativas=2
                )

                if confirmacao_final is True:

                    logger.info(
                        "[%s] Era um falso offline. "
                        "Continuando gravação.",
                        username
                    )

                    continue

                if confirmacao_final is None:

                    logger.warning(
                        "[%s] Não foi possível confirmar "
                        "o encerramento da LIVE. "
                        "Continuando.",
                        username
                    )

                    continue

                logger.info(
                    "[%s] Live confirmada como encerrada.",
                    username
                )

                break

            # =================================================
            # RECONEXÃO
            # =================================================

            reconnect_counts[username] = (
                reconnect_counts.get(
                    username,
                    0
                ) + 1
            )

            tentativa = reconnect_counts[
                username
            ]

            logger.warning(
                "[%s] Conexão caiu, mas a live "
                "continua ativa. Reconectando. "
                "Tentativa %s.",
                username,
                tentativa
            )

            await enviar_mensagem(
                chat_id,
                f"⚠️ Conexão com @{username} caiu.\n"
                f"🔄 Reconectando em "
                f"{RECONNECT_SECONDS}s...\n"
                f"🔁 Tentativa {tentativa}"
            )

            await asyncio.sleep(
                RECONNECT_SECONDS
            )

    finally:

        recordings.pop(
            username,
            None
        )

        reconnect_counts.pop(
            username,
            None
        )

        stop_requests.discard(
            username
        )

        logger.info(
            "[%s] Finalizando gravação.",
            username
        )

        await finalizar_gravacao(
            username,
            chat_id
        )


# ============================================================
# /START
# ============================================================

async def cmd_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = update.effective_chat.id

    await enviar_mensagem(
        chat_id,
        "🤖 Bot de gravação TikTok LIVE ativo!\n\n"
        "Use /ajuda para ver os comandos."
    )


# ============================================================
# /AJUDA
# ============================================================

async def cmd_ajuda(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = update.effective_chat.id

    texto = (
        "🤖 COMANDOS\n\n"

        "🎥 GRAVAÇÃO\n"
        "/gravar usuario\n"
        "Inicia uma gravação manual.\n\n"

        "/parar\n"
        "Para as gravações ativas.\n\n"

        "/status\n"
        "Mostra as gravações atuais.\n\n"

        "👁️ MONITORAMENTO\n"
        "/monitorar usuario\n"
        "Monitora automaticamente uma conta.\n\n"

        "/desmonitorar usuario\n"
        "Para o monitoramento.\n\n"

        "/monitorados\n"
        "Lista as contas monitoradas.\n\n"

        "Exemplo:\n"
        "/monitorar cmlykimberly"
    )

    await enviar_mensagem(
        chat_id,
        texto
    )


# ============================================================
# /GRAVAR
# ============================================================

async def cmd_gravar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = update.effective_chat.id

    if not context.args:

        await enviar_mensagem(
            chat_id,
            "Use:\n/gravar usuario"
        )

        return

    username = normalizar_usuario(
        context.args[0]
    )

    if username in recordings:

        await enviar_mensagem(
            chat_id,
            f"⚠️ @{username} já está sendo gravado."
        )

        return

    asyncio.create_task(
        record_live(
            username,
            chat_id
        )
    )

    await enviar_mensagem(
        chat_id,
        f"🎬 Solicitação de gravação enviada "
        f"para @{username}."
    )


# ============================================================
# /PARAR
# ============================================================

async def cmd_parar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = update.effective_chat.id

    ativos = list(
        recordings.keys()
    )

    if not ativos:

        await enviar_mensagem(
            chat_id,
            "ℹ️ Não há gravações ativas."
        )

        return

    for username in ativos:

        stop_requests.add(
            username
        )

        processo = recordings.get(
            username
        )

        if processo:

            try:

                processo.terminate()

            except Exception as e:

                logger.error(
                    "[%s] Erro ao parar processo: %s",
                    username,
                    e
                )

    await enviar_mensagem(
        chat_id,
        "🛑 Parada solicitada para:\n"
        + "\n".join(
            f"• @{u}"
            for u in ativos
        )
    )


# ============================================================
# /STATUS
# ============================================================

async def cmd_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = update.effective_chat.id

    if not recordings:

        await enviar_mensagem(
            chat_id,
            "📭 Nenhuma gravação ativa."
        )

        return

    linhas = [
        "🎥 GRAVAÇÕES ATIVAS\n"
    ]

    for username, processo in recordings.items():

        if processo is None:

            estado = "iniciando"

        elif processo.returncode is None:

            estado = "gravando"

        else:

            estado = "reconectando"

        linhas.append(
            f"• @{username} — {estado}"
        )

    await enviar_mensagem(
        chat_id,
        "\n".join(linhas)
    )


# ============================================================
# /MONITORAR
# ============================================================

async def cmd_monitorar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = update.effective_chat.id

    if not context.args:

        await enviar_mensagem(
            chat_id,
            "Use:\n/monitorar usuario"
        )

        return

    username = normalizar_usuario(
        context.args[0]
    )

    if username in monitored_users:

        await enviar_mensagem(
            chat_id,
            f"👁️ @{username} já está sendo monitorado."
        )

        return

    monitored_users[username] = {
        "chat_id": chat_id,
        "live": False,
        "started_at": None,
        "offline_checks": 0,
    }

    # ========================================================
    # SALVA IMEDIATAMENTE
    # ========================================================

    salvo = await salvar_monitorados()

    if not salvo:

        await enviar_mensagem(
            chat_id,
            f"⚠️ @{username} foi adicionado "
            f"temporariamente, mas NÃO consegui salvar "
            f"no banco do Render.\n\n"
            f"Verifique a variável REDIS_URL."
        )

    task = asyncio.create_task(
        monitorar_usuario(
            username,
            chat_id
        )
    )

    monitor_tasks[username] = task

    await enviar_mensagem(
        chat_id,
        f"👁️ Monitoramento ativado para "
        f"@{username}.\n\n"
        f"💾 Salvo permanentemente.\n"
        f"🔄 Verificação a cada "
        f"{MONITOR_INTERVAL} segundos."
    )


# ============================================================
# MONITORAMENTO
# ============================================================

async def monitorar_usuario(
    username: str,
    chat_id: int
):

    username = normalizar_usuario(
        username
    )

    estava_ao_vivo = False

    logger.info(
        "[%s] Monitoramento iniciado.",
        username
    )

    try:

        while username in monitored_users:

            ao_vivo = await verificar_live(
                username
            )

            info = monitored_users.get(
                username,
                {}
            )

            # =================================================
            # NÃO FOI POSSÍVEL DETERMINAR
            # =================================================

            if ao_vivo is None:

                logger.warning(
                    "[%s] Verificação inconclusiva. "
                    "Mantendo estado anterior.",
                    username
                )

                await asyncio.sleep(
                    MONITOR_INTERVAL
                )

                continue

            # =================================================
            # ENTROU AO VIVO
            # =================================================

            if (
                ao_vivo
                and not estava_ao_vivo
            ):

                estava_ao_vivo = True

                info["offline_checks"] = 0

                info["live"] = True

                info["started_at"] = (
                    datetime.now().isoformat()
                )

                await salvar_monitorados()

                await enviar_mensagem(
                    chat_id,
                    f"🔴 @{username} ESTÁ AO VIVO!\n"
                    f"🎥 Iniciando gravação automática..."
                )

                if username not in recordings:

                    asyncio.create_task(
                        record_live(
                            username,
                            chat_id
                        )
                    )

            # =================================================
            # CONTINUA AO VIVO
            # =================================================

            elif ao_vivo:

                estava_ao_vivo = True

                info["offline_checks"] = 0

                info["live"] = True

            # =================================================
            # POSSÍVEL SAÍDA DO AR
            # =================================================

            else:

                info["offline_checks"] = (
                    info.get(
                        "offline_checks",
                        0
                    ) + 1
                )

                contador = info[
                    "offline_checks"
                ]

                logger.warning(
                    "[%s] Offline detectado "
                    "(%s/%s confirmações).",
                    username,
                    contador,
                    OFFLINE_CONFIRMATIONS
                )

                # Não considera offline imediatamente.
                if contador < OFFLINE_CONFIRMATIONS:

                    await asyncio.sleep(
                        MONITOR_INTERVAL
                    )

                    continue

                # =================================================
                # SAIU DO AR CONFIRMADO
                # =================================================

                estava_ao_vivo = False

                info["live"] = False

                info["started_at"] = None

                info["offline_checks"] = 0

                await salvar_monitorados()

                await enviar_mensagem(
                    chat_id,
                    f"⚫ @{username} "
                    f"não está mais sendo detectada "
                    f"como ao vivo."
                )

            await asyncio.sleep(
                MONITOR_INTERVAL
            )

    except asyncio.CancelledError:

        logger.info(
            "[%s] Monitoramento cancelado.",
            username
        )

    except Exception as e:

        logger.error(
            "[%s] Erro no monitoramento: %s",
            username,
            e
        )

    finally:

        monitor_tasks.pop(
            username,
            None
        )

        logger.info(
            "[%s] Monitoramento encerrado.",
            username
        )


# ============================================================
# /DESMONITORAR
# ============================================================

async def cmd_desmonitorar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = update.effective_chat.id

    if not context.args:

        await enviar_mensagem(
            chat_id,
            "Use:\n/desmonitorar usuario"
        )

        return

    username = normalizar_usuario(
        context.args[0]
    )

    task = monitor_tasks.get(
        username
    )

    if task:

        task.cancel()

    monitor_tasks.pop(
        username,
        None
    )

    if username in monitored_users:

        monitored_users.pop(
            username,
            None
        )

        await salvar_monitorados()

        await enviar_mensagem(
            chat_id,
            f"🛑 Monitoramento de @{username} "
            f"desativado e removido da lista."
        )

    else:

        await enviar_mensagem(
            chat_id,
            f"ℹ️ @{username} não estava sendo monitorado."
        )


# ============================================================
# /MONITORADOS
# ============================================================

async def cmd_monitorados(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = update.effective_chat.id

    # ========================================================
    # Tenta atualizar a memória com o banco
    # ========================================================

    if redis_client:

        await carregar_monitorados()

    if not monitored_users:

        await enviar_mensagem(
            chat_id,
            "📭 Nenhuma conta monitorada."
        )

        return

    linhas = [
        "👁️ CONTAS MONITORADAS\n"
    ]

    for username, info in monitored_users.items():

        if info.get("live"):

            estado = "🔴 AO VIVO"

        else:

            estado = "⚫ offline"

        linhas.append(
            f"• @{username} — {estado}"
        )

    linhas.append(
        "\n💾 Lista salva permanentemente."
    )

    await enviar_mensagem(
        chat_id,
        "\n".join(linhas)
    )


# ============================================================
# CONFIGURAR COMANDOS
# ============================================================

async def configurar_comandos():

    try:

        await telegram_app.bot.set_my_commands(
            BOT_COMMANDS
        )

        logger.info(
            "Comandos do Telegram configurados."
        )

    except Exception as e:

        logger.error(
            "Erro configurando comandos: %s",
            e
        )


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup():

    global telegram_app

    logger.info(
        "🚀 Iniciando bot..."
    )

    # ========================================================
    # REDIS
    # ========================================================

    conectado = await conectar_redis()

    if conectado:

        await carregar_monitorados()

    # ========================================================
    # TELEGRAM
    # ========================================================

    telegram_app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    # ========================================================
    # HANDLERS
    # ========================================================

    telegram_app.add_handler(
        CommandHandler(
            "start",
            cmd_start
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "ajuda",
            cmd_ajuda
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
            "parar",
            cmd_parar
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "status",
            cmd_status
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

    # ========================================================
    # TELEGRAM START
    # ========================================================

    await telegram_app.initialize()

    await telegram_app.start()

    await configurar_comandos()

    # ========================================================
    # WEBHOOK
    # ========================================================

    if RENDER_EXTERNAL_URL:

        webhook_url = (
            f"{RENDER_EXTERNAL_URL}"
            f"/telegram/webhook"
        )

        await telegram_app.bot.set_webhook(
            url=webhook_url
        )

        logger.info(
            "Webhook configurado: %s",
            webhook_url
        )

    else:

        logger.warning(
            "RENDER_EXTERNAL_URL não configurada."
        )

    # ========================================================
    # RESTAURAR MONITORES
    # ========================================================

    await iniciar_monitores_salvos()

    logger.info(
        "🤖 Bot iniciado com sucesso."
    )


# ============================================================
# SHUTDOWN
# ============================================================

@app.on_event("shutdown")
async def shutdown():

    global telegram_app

    logger.info(
        "🛑 Encerrando bot..."
    )

    # ========================================================
    # SALVAR MONITORADOS
    # ========================================================

    try:

        await salvar_monitorados()

    except Exception as e:

        logger.error(
            "Erro salvando monitorados no shutdown: %s",
            e
        )

    # ========================================================
    # PARA GRAVAÇÕES
    # ========================================================

    for username, processo in list(
        recordings.items()
    ):

        try:

            if processo:

                processo.terminate()

        except Exception:

            pass

    # ========================================================
    # CANCELA MONITORES
    # ========================================================

    for username, task in list(
        monitor_tasks.items()
    ):

        try:

            task.cancel()

        except Exception:

            pass

    monitor_tasks.clear()

    # ========================================================
    # TELEGRAM
    # ========================================================

    if telegram_app:

        try:

            await telegram_app.stop()

        except Exception as e:

            logger.error(
                "Erro parando aplicação Telegram: %s",
                e
            )

        try:

            await telegram_app.shutdown()

        except Exception as e:

            logger.error(
                "Erro no shutdown Telegram: %s",
                e
            )

    # ========================================================
    # REDIS
    # ========================================================

    if redis_client:

        try:

            await redis_client.close()

        except Exception as e:

            logger.error(
                "Erro fechando Key Value: %s",
                e
            )

    logger.info(
        "Bot encerrado."
    )


# ============================================================
# WEBHOOK TELEGRAM
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

        return {
            "ok": True
        }

    except Exception as e:

        logger.error(
            "Erro no webhook: %s",
            e
        )

        return {
            "ok": False,
            "error": str(e)
        }


# ============================================================
# HEALTH
# ============================================================

@app.get("/")
async def root():

    return {
        "status": "online",
        "bot": "telegram-live-recorder"
    }


@app.get("/health")
async def health():

    return {
        "status": "ok",

        "redis": (
            redis_client is not None
        ),

        "recordings": list(
            recordings.keys()
        ),

        "monitored": list(
            monitored_users.keys()
        )
    }


# ============================================================
# FALLBACK
# ============================================================

@app.get(
    "/telegram/webhook",
    response_class=PlainTextResponse
)
async def webhook_get():

    return "Telegram Live Recorder OK"
