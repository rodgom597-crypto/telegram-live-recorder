import os
import json
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

# ============================================================
# CONFIGURAÇÃO
# ============================================================

TOKEN = os.environ["BOT_TOKEN"]

RENDER_EXTERNAL_URL = os.environ.get(
    "RENDER_EXTERNAL_URL",
    ""
).rstrip("/")

OUTPUT_DIR = Path("/tmp/recordings")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MONITOR_INTERVAL = 30
RECONNECT_SECONDS = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger(__name__)

app = FastAPI()

telegram_app = None


# ============================================================
# ESTADOS
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
    BotCommand("start", "Iniciar o bot"),
    BotCommand("ajuda", "Mostrar comandos"),
    BotCommand("status", "Ver gravações ativas"),
    BotCommand("gravar", "Gravar uma live"),
    BotCommand("parar", "Parar gravações"),
    BotCommand("monitorar", "Monitorar uma conta"),
    BotCommand("desmonitorar", "Parar monitoramento"),
    BotCommand("monitorados", "Listar contas monitoradas"),
]


# ============================================================
# UTILIDADES
# ============================================================

def normalizar_usuario(username: str) -> str:
    username = username.strip()
    username = username.replace("@", "")
    username = username.split()[0]
    return username.lower()


async def enviar_mensagem(chat_id, texto):
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
# VERIFICAR LIVE
# ============================================================

async def verificar_live(username: str):
    """
    Verifica o estado da live usando yt-dlp.

    IMPORTANTE:
    Não usamos mais 'protocol' ou 'url' como falso positivo.

    O resultado é considerado ao vivo somente quando
    o JSON contém is_live=True.

    Retorno:
        True  = provavelmente ao vivo
        False = não confirmado ao vivo
    """

    username = normalizar_usuario(username)

    url = f"https://www.tiktok.com/@{username}/live"

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

        # ----------------------------------------------------
        # Tenta interpretar JSON
        # ----------------------------------------------------

        linhas = [
            linha.strip()
            for linha in texto.splitlines()
            if linha.strip()
        ]

        for linha in reversed(linhas):

            try:
                dados = json.loads(linha)

                if isinstance(dados, dict):

                    is_live = dados.get("is_live")

                    if is_live is True:
                        logger.info(
                            "[%s] VERIFICAÇÃO: AO VIVO",
                            username
                        )
                        return True

                    if is_live is False:
                        logger.info(
                            "[%s] VERIFICAÇÃO: OFFLINE",
                            username
                        )
                        return False

            except json.JSONDecodeError:
                continue

        # ----------------------------------------------------
        # Alguns retornos do TikTok podem não entregar
        # is_live claramente.
        #
        # Se o yt-dlp retornar código 1 com "not currently live",
        # NÃO usamos isso para matar uma gravação que já estava
        # funcionando.
        # ----------------------------------------------------

        if "not currently live" in erro.lower():
            logger.info(
                "[%s] yt-dlp informou que não está ao vivo.",
                username
            )
            return False

        if processo.returncode == 0:
            logger.info(
                "[%s] yt-dlp retornou sem confirmação de live.",
                username
            )

        return False

    except Exception as e:
        logger.error(
            "[%s] Erro ao verificar live: %s",
            username,
            e
        )

        return False


# ============================================================
# LOCALIZAR ARQUIVOS
# ============================================================

def procurar_arquivos(username: str):
    username = normalizar_usuario(username)

    arquivos = []

    for arquivo in OUTPUT_DIR.glob("*.flv"):

        if username in arquivo.name.lower():
            arquivos.append(arquivo)

    arquivos.sort(
        key=lambda x: x.stat().st_mtime
    )

    return arquivos


# ============================================================
# CONVERSÃO FLV -> MP4
# ============================================================

async def converter_para_mp4(
    username: str,
    arquivos,
):
    if not arquivos:
        return None

    username = normalizar_usuario(username)

    mp4_final = OUTPUT_DIR / (
        f"{username}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    )

    # --------------------------------------------------------
    # APENAS UM FLV
    # --------------------------------------------------------

    if len(arquivos) == 1:

        flv = arquivos[0]

        comando = [
            "ffmpeg",
            "-y",
            "-i",
            str(flv),
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            str(mp4_final),
        ]

        logger.info(
            "[%s] Convertendo FLV para MP4.",
            username
        )

        processo = await asyncio.create_subprocess_exec(
            *comando,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        _, stderr = await processo.communicate()

        if processo.returncode == 0 and mp4_final.exists():
            return mp4_final

        logger.warning(
            "[%s] Conversão direta falhou. "
            "Tentando reencodificação.",
            username
        )

        if mp4_final.exists():
            mp4_final.unlink()

        comando = [
            "ffmpeg",
            "-y",
            "-i",
            str(flv),
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

        _, stderr = await processo.communicate()

        if processo.returncode == 0 and mp4_final.exists():
            return mp4_final

        logger.error(
            "[%s] Falha na conversão: %s",
            username,
            stderr.decode(
                "utf-8",
                errors="ignore"
            )[-3000:]
        )

        return None

    # --------------------------------------------------------
    # VÁRIOS FLV
    # --------------------------------------------------------

    lista = OUTPUT_DIR / (
        f"concat_{username}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    )

    try:

        with open(lista, "w", encoding="utf-8") as f:

            for arquivo in arquivos:

                caminho = str(
                    arquivo.resolve()
                ).replace("'", "'\\''")

                f.write(
                    f"file '{caminho}'\n"
                )

        comando = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(lista),
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
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

        if processo.returncode == 0 and mp4_final.exists():
            return mp4_final

        logger.warning(
            "[%s] Concatenação direta falhou. "
            "Tentando reencodificação.",
            username
        )

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
            str(lista),
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

        _, stderr = await processo.communicate()

        if processo.returncode == 0 and mp4_final.exists():
            return mp4_final

        logger.error(
            "[%s] Falha ao juntar gravações: %s",
            username,
            stderr.decode(
                "utf-8",
                errors="ignore"
            )[-3000:]
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
    chat_id,
):
    username = normalizar_usuario(username)

    arquivos = procurar_arquivos(username)

    if not arquivos:

        await enviar_mensagem(
            chat_id,
            f"⚠️ Nenhum arquivo encontrado para @{username}."
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
            mp4.stat().st_size / 1024 / 1024
        )

        logger.info(
            "[%s] MP4 pronto: %.2f MB",
            username,
            tamanho_mb
        )

        await telegram_app.bot.send_video(
            chat_id=chat_id,
            video=mp4.open("rb"),
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

            await telegram_app.bot.send_document(
                chat_id=chat_id,
                document=mp4.open("rb"),
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

    # --------------------------------------------------------
    # LIMPEZA
    # --------------------------------------------------------

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
    chat_id,
):
    username = normalizar_usuario(username)

    # Remove pedido de parada anterior
    stop_requests.discard(username)

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

            # ------------------------------------------------
            # PARADA MANUAL
            # ------------------------------------------------

            if username in stop_requests:

                logger.info(
                    "[%s] Parada manual solicitada.",
                    username
                )

                break

            # ------------------------------------------------
            # URL
            # ------------------------------------------------

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

            # ------------------------------------------------
            # IMPORTANTE:
            #
            # NÃO verificamos "está ao vivo?" aqui antes
            # de cada tentativa.
            #
            # O próprio yt-dlp é usado para tentar conectar.
            # Isso evita o problema que aconteceu com a live
            # que estava ativa mas foi reportada incorretamente.
            # ------------------------------------------------

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

                    linha = await processo.stdout.readline()

                    if not linha:
                        break

                    texto = linha.decode(
                        "utf-8",
                        errors="ignore"
                    ).strip()

                    if texto:

                        saida.append(texto)

                        logger.info(
                            "[%s] %s",
                            username,
                            texto
                        )

                codigo = await processo.wait()

                recordings.pop(username, None)

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

                    if recordings.get(username):

                        recordings[username].terminate()

                except Exception:
                    pass

                raise

            except Exception as e:

                logger.error(
                    "[%s] Erro executando yt-dlp: %s",
                    username,
                    e
                )

                recordings.pop(username, None)

                texto_completo = str(e)
                codigo = -1

            # ------------------------------------------------
            # PARADA MANUAL
            # ------------------------------------------------

            if username in stop_requests:

                logger.info(
                    "[%s] Não reconectar: parada manual.",
                    username
                )

                break

            # ------------------------------------------------
            # ANALISAR ARQUIVO GERADO
            # ------------------------------------------------

            arquivo_gerado = (
                arquivo.exists()
                and arquivo.stat().st_size > 0
            )

            # ------------------------------------------------
            # ERRO "NOT CURRENTLY LIVE"
            #
            # Este ponto é delicado.
            #
            # Se não existe nenhum arquivo de gravação,
            # significa que a conexão nem chegou a começar.
            #
            # Nesse caso não ficamos em loop infinito.
            #
            # Se já existem arquivos anteriores, significa que
            # houve uma gravação e a conexão caiu. Nesse caso
            # verificamos se a live ainda está acontecendo.
            # ------------------------------------------------

            erro_offline = (
                "not currently live"
                in texto_completo.lower()
            )

            arquivos_existentes = procurar_arquivos(
                username
            )

            if erro_offline and not arquivos_existentes:

                await enviar_mensagem(
                    chat_id,
                    f"⚠️ @{username} não está disponível "
                    f"para gravação no momento."
                )

                break

            # ------------------------------------------------
            # SE GEROU ARQUIVO, CONTINUAMOS.
            # ------------------------------------------------

            if arquivo_gerado:

                logger.info(
                    "[%s] Arquivo FLV gerado.",
                    username
                )

            # ------------------------------------------------
            # VERIFICAR SE LIVE AINDA ESTÁ ATIVA
            #
            # Só fazemos isso DEPOIS de uma tentativa de
            # gravação ter terminado.
            #
            # Se o monitoramento estiver ativo, ele também
            # ajuda a determinar o estado.
            # ------------------------------------------------

            ainda_ativa = False

            if username in monitored_users:

                info = monitored_users.get(
                    username,
                    {}
                )

                ainda_ativa = info.get(
                    "live",
                    False
                )

            # Se não temos certeza pelo monitor, tentamos
            # uma verificação direta.
            if not ainda_ativa:

                ainda_ativa = await verificar_live(
                    username
                )

            # ------------------------------------------------
            # LIVE TERMINOU
            # ------------------------------------------------

            if not ainda_ativa:

                logger.info(
                    "[%s] Live não está mais confirmada.",
                    username
                )

                break

            # ------------------------------------------------
            # LIVE AINDA ESTÁ ATIVA
            # ------------------------------------------------

            reconnect_counts[username] = (
                reconnect_counts.get(username, 0) + 1
            )

            tentativa = reconnect_counts[username]

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

        recordings.pop(username, None)

        reconnect_counts.pop(username, None)

        stop_requests.discard(username)

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
    context: ContextTypes.DEFAULT_TYPE,
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
    context: ContextTypes.DEFAULT_TYPE,
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
        "Remove uma conta do monitoramento.\n\n"

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
    context: ContextTypes.DEFAULT_TYPE,
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

    # Não verifica a live antes.
    # Deixa o yt-dlp fazer a tentativa real.
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
    context: ContextTypes.DEFAULT_TYPE,
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
    context: ContextTypes.DEFAULT_TYPE,
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
    context: ContextTypes.DEFAULT_TYPE,
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
    }

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
        f"Vou verificar a cada "
        f"{MONITOR_INTERVAL} segundos."
    )


# ============================================================
# MONITORAMENTO
# ============================================================

async def monitorar_usuario(
    username: str,
    chat_id: int,
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

            # ------------------------------------------------
            # ENTROU AO VIVO
            # ------------------------------------------------

            if ao_vivo and not estava_ao_vivo:

                estava_ao_vivo = True

                monitored_users[username][
                    "live"
                ] = True

                monitored_users[username][
                    "started_at"
                ] = datetime.now().isoformat()

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

            # ------------------------------------------------
            # CONTINUA AO VIVO
            # ------------------------------------------------

            elif ao_vivo:

                monitored_users[username][
                    "live"
                ] = True

            # ------------------------------------------------
            # SAIU DO AR
            # ------------------------------------------------

            elif not ao_vivo and estava_ao_vivo:

                estava_ao_vivo = False

                monitored_users[username][
                    "live"
                ] = False

                monitored_users[username][
                    "started_at"
                ] = None

                await enviar_mensagem(
                    chat_id,
                    f"⚫ @{username} "
                    f"não está mais sendo detectada como ao vivo."
                )

                # Não damos terminate imediatamente.
                #
                # O record_live vai perceber a queda,
                # verificar novamente e finalizar.
                #
                # Isso evita cortar o último pedaço do vídeo.

            # ------------------------------------------------
            # OFFLINE
            # ------------------------------------------------

            else:

                monitored_users[username][
                    "live"
                ] = False

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
    context: ContextTypes.DEFAULT_TYPE,
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

        await enviar_mensagem(
            chat_id,
            f"🛑 Monitoramento de @{username} "
            f"desativado."
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
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = update.effective_chat.id

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

    telegram_app = (
        Application.builder()
        .token(TOKEN)
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

    await telegram_app.initialize()

    await telegram_app.start()

    await configurar_comandos()

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

    # --------------------------------------------------------
    # Para gravações
    # --------------------------------------------------------

    for username, processo in list(
        recordings.items()
    ):

        try:

            if processo:

                processo.terminate()

        except Exception:
            pass

    # --------------------------------------------------------
    # Cancela monitores
    # --------------------------------------------------------

    for username, task in list(
        monitor_tasks.items()
    ):

        try:

            task.cancel()

        except Exception:
            pass

    monitor_tasks.clear()

    # --------------------------------------------------------
    # Telegram
    # --------------------------------------------------------

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

    logger.info(
        "Bot encerrado."
    )


# ============================================================
# WEBHOOK TELEGRAM
# ============================================================

@app.post("/telegram/webhook")
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
