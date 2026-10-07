```python
import os
import re
import json
import asyncio
import logging
import signal
import subprocess
from pathlib import Path
from typing import Optional

import requests
import redis.asyncio as redis

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn


# ============================================================
# CONFIGURAÇÕES
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
REDIS_URL = os.getenv("REDIS_URL", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

PORT = int(os.getenv("PORT", "10000"))

BASE_DIR = Path("/tmp/gravacoes")
BASE_DIR.mkdir(parents=True, exist_ok=True)

TIKTOK_TIMEOUT = 25

INTERVALO_MONITORAMENTO = 30

MAX_TENTATIVAS_MANUAL = 3

# Guarda as tarefas de monitoramento
tarefas_monitoramento = {}

# Guarda as gravações em andamento
tarefas_gravacao = {}

# Controle de encerramento
shutting_down = False

# Redis
redis_client = None

# Sessão HTTP
http_session = requests.Session()


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


# ============================================================
# HEADERS TIKTOK
# ============================================================

def headers_tiktok():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.tiktok.com/",
        "Origin": "https://www.tiktok.com",
        "Connection": "keep-alive",
    }


def headers_api_tiktok():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.tiktok.com/",
        "Origin": "https://www.tiktok.com",
        "Connection": "keep-alive",
    }


# ============================================================
# TELEGRAM
# ============================================================

async def telegram_request(method, data=None):
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN não configurado.")
        return None

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    try:
        def request_sync():
            response = requests.post(
                url,
                json=data or {},
                timeout=30
            )

            try:
                return response.json()
            except Exception:
                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "text": response.text[:500]
                }

        return await asyncio.to_thread(request_sync)

    except Exception as e:
        logger.error(f"Erro Telegram: {e}")
        return None


async def enviar_mensagem(chat_id, texto):
    return await telegram_request(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": texto
        }
    )


# ============================================================
# REDIS
# ============================================================

async def conectar_redis():
    global redis_client

    if not REDIS_URL:
        logger.warning("REDIS_URL não configurado.")
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
        logger.error(f"❌ Erro conectando ao Redis: {e}")
        redis_client = None
        return False


async def salvar_monitorados():
    if not redis_client:
        return

    try:
        contas = sorted(tarefas_monitoramento.keys())

        await redis_client.delete("monitorados")

        if contas:
            await redis_client.sadd(
                "monitorados",
                *contas
            )

        logger.info(
            f"💾 Monitorados salvos: {contas}"
        )

    except Exception as e:
        logger.error(
            f"Erro salvando monitorados: {e}"
        )


async def carregar_monitorados():
    if not redis_client:
        return []

    try:
        contas = await redis_client.smembers(
            "monitorados"
        )

        return sorted(contas)

    except Exception as e:
        logger.error(
            f"Erro carregando monitorados: {e}"
        )

        return []


# ============================================================
# EXTRAÇÃO DO ROOM ID
# ============================================================

def extrair_room_id(texto):
    """
    Tenta encontrar o roomId dentro do HTML/JSON
    entregue pelo TikTok.
    """

    padroes = [
        r'"roomId"\s*:\s*"(\d+)"',
        r'"room_id"\s*:\s*"(\d+)"',
        r'"roomId"\s*:\s*(\d+)',
        r'"room_id"\s*:\s*(\d+)',
        r'roomId\\?"\s*:\s*\\?"(\d+)',
        r'room_id\\?"\s*:\s*\\?"(\d+)',
    ]

    for padrao in padroes:
        resultado = re.search(
            padrao,
            texto
        )

        if resultado:
            return resultado.group(1)

    return None


# ============================================================
# OBTÉM ROOM ID PELO TIKTOK
# ============================================================

async def obter_room_id(usuario):
    usuario = usuario.replace("@", "").strip()

    url = (
        f"https://www.tiktok.com/@{usuario}/live"
    )

    logger.info(
        f"[{usuario}] Buscando página do TikTok."
    )

    try:

        def request_sync():
            return http_session.get(
                url,
                headers=headers_tiktok(),
                timeout=TIKTOK_TIMEOUT,
                allow_redirects=True
            )

        response = await asyncio.to_thread(
            request_sync
        )

        logger.info(
            f"[{usuario}] TikTok HTTP {response.status_code}"
        )

        if response.status_code != 200:
            return None

        texto = response.text

        room_id = extrair_room_id(texto)

        if room_id:
            logger.info(
                f"[{usuario}] Room ID encontrado: {room_id}"
            )
            return room_id

        logger.warning(
            f"[{usuario}] Não encontramos roomId na página."
        )

        return None

    except Exception as e:
        logger.error(
            f"[{usuario}] Erro obtendo room ID: {e}"
        )
        return None


# ============================================================
# CONSULTA DIRETA À API DO TIKTOK
# ============================================================

async def obter_info_live_tiktok(
    usuario,
    room_id
):
    """
    Consulta diretamente:

    https://webcast.tiktok.com/webcast/room/info

    O próprio extractor atual do yt-dlp utiliza esse endpoint
    para obter as informações e URLs do LIVE.
    """

    url = (
        "https://webcast.tiktok.com/"
        "webcast/room/info"
    )

    params = {
        "aid": "1988",
        "room_id": room_id
    }

    logger.info(
        f"[{usuario}] Consultando API direta do TikTok."
    )

    try:

        def request_sync():
            return http_session.get(
                url,
                params=params,
                headers=headers_api_tiktok(),
                timeout=TIKTOK_TIMEOUT
            )

        response = await asyncio.to_thread(
            request_sync
        )

        logger.info(
            f"[{usuario}] API room/info HTTP "
            f"{response.status_code}"
        )

        if response.status_code != 200:
            logger.warning(
                f"[{usuario}] API respondeu "
                f"HTTP {response.status_code}"
            )
            return None

        try:
            data = response.json()
        except Exception as e:
            logger.error(
                f"[{usuario}] Resposta não é JSON: {e}"
            )
            return None

        status = data.get("status")

        logger.info(
            f"[{usuario}] TikTok API status={status}"
        )

        if str(status) != "2":
            logger.warning(
                f"[{usuario}] TikTok não confirmou LIVE "
                f"pela API."
            )
            return None

        return data

    except Exception as e:
        logger.error(
            f"[{usuario}] Erro na API direta: {e}"
        )

        return None


# ============================================================
# ENCONTRAR URL DE STREAM
# ============================================================

def encontrar_urls_stream(data):
    urls = []

    if not isinstance(data, dict):
        return urls

    # --------------------------------------------------------
    # Procura recursivamente URLs dentro do JSON
    # --------------------------------------------------------

    def percorrer(obj):

        if isinstance(obj, dict):

            for chave, valor in obj.items():

                if isinstance(valor, str):

                    valor_lower = valor.lower()

                    if (
                        valor.startswith("http")
                        and (
                            ".flv" in valor_lower
                            or ".m3u8" in valor_lower
                            or "pull-" in valor_lower
                            or "live" in valor_lower
                        )
                    ):
                        urls.append(valor)

                elif isinstance(valor, (dict, list)):
                    percorrer(valor)

        elif isinstance(obj, list):

            for item in obj:
                percorrer(item)

    percorrer(data)

    # remove duplicados
    resultado = []

    for url in urls:
        if url not in resultado:
            resultado.append(url)

    return resultado


# ============================================================
# ESCOLHER MELHOR URL
# ============================================================

def escolher_stream_url(urls):

    # Primeiro FLV
    for url in urls:
        if ".flv" in url.lower():
            return url

    # Depois HLS
    for url in urls:
        if ".m3u8" in url.lower():
            return url

    # Qualquer URL
    if urls:
        return urls[0]

    return None


# ============================================================
# DESCOBRIR STREAM DIRETO
# ============================================================

async def descobrir_stream_direto(usuario):

    usuario = usuario.replace("@", "").strip()

    # --------------------------------------------------------
    # 1 - ROOM ID
    # --------------------------------------------------------

    room_id = await obter_room_id(
        usuario
    )

    if not room_id:
        logger.warning(
            f"[{usuario}] Não foi possível obter room ID."
        )
        return None

    # --------------------------------------------------------
    # 2 - API
    # --------------------------------------------------------

    info = await obter_info_live_tiktok(
        usuario,
        room_id
    )

    if not info:
        return None

    # --------------------------------------------------------
    # 3 - URL
    # --------------------------------------------------------

    urls = encontrar_urls_stream(
        info
    )

    logger.info(
        f"[{usuario}] URLs de stream encontradas: "
        f"{len(urls)}"
    )

    if not urls:
        return None

    stream_url = escolher_stream_url(
        urls
    )

    if not stream_url:
        return None

    tipo = (
        "FLV"
        if ".flv" in stream_url.lower()
        else "HLS"
        if ".m3u8" in stream_url.lower()
        else "STREAM"
    )

    logger.info(
        f"[{usuario}] Stream direto encontrado: {tipo}"
    )

    return {
        "room_id": room_id,
        "url": stream_url,
        "tipo": tipo,
        "info": info
    }


# ============================================================
# VERIFICAR LIVE
# ============================================================

async def verificar_live_direto(usuario):

    resultado = await descobrir_stream_direto(
        usuario
    )

    if resultado:
        return resultado

    return None


# ============================================================
# EXECUTAR FFMPEG
# ============================================================

async def executar_ffmpeg_stream(
    usuario,
    stream_url,
    arquivo_saida
):
    """
    Grava diretamente a URL FLV/HLS usando FFmpeg.

    Não passamos a URL para o yt-dlp.
    """

    logger.info(
        f"[{usuario}] Iniciando FFmpeg direto."
    )

    arquivo_saida.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    comando = [
        "ffmpeg",

        "-hide_banner",
        "-loglevel",
        "warning",

        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "10",

        "-rw_timeout",
        "30000000",

        "-i",
        stream_url,

        "-c",
        "copy",

        "-f",
        "flv",

        "-y",
        str(arquivo_saida)
    ]

    logger.info(
        f"[{usuario}] FFmpeg iniciado."
    )

    processo = None

    try:

        processo = await asyncio.create_subprocess_exec(
            *comando,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await processo.communicate()

        if processo.returncode != 0:

            erro = stderr.decode(
                "utf-8",
                errors="ignore"
            )

            logger.error(
                f"[{usuario}] FFmpeg terminou com erro "
                f"{processo.returncode}: "
                f"{erro[-2000:]}"
            )

            return False

        if not arquivo_saida.exists():
            logger.error(
                f"[{usuario}] FFmpeg terminou mas "
                f"arquivo não existe."
            )
            return False

        tamanho = arquivo_saida.stat().st_size

        if tamanho < 100000:
            logger.warning(
                f"[{usuario}] Arquivo muito pequeno: "
                f"{tamanho} bytes"
            )

        logger.info(
            f"[{usuario}] Gravação FLV concluída: "
            f"{tamanho / 1024 / 1024:.2f} MB"
        )

        return True

    except asyncio.CancelledError:

        if processo:

            try:
                processo.terminate()
            except Exception:
                pass

        raise

    except Exception as e:

        logger.error(
            f"[{usuario}] Erro executando FFmpeg: {e}"
        )

        return False


# ============================================================
# CONVERTER FLV PARA MP4
# ============================================================

async def converter_para_mp4(
    usuario,
    arquivo_flv,
    arquivo_mp4
):

    logger.info(
        f"[{usuario}] Convertendo FLV para MP4."
    )

    comando = [
        "ffmpeg",

        "-hide_banner",
        "-loglevel",
        "warning",

        "-fflags",
        "+genpts+discardcorrupt",

        "-err_detect",
        "ignore_err",

        "-i",
        str(arquivo_flv),

        "-map",
        "0:v?",
        "-map",
        "0:a?",

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

        "-y",
        str(arquivo_mp4)
    ]

    try:

        processo = await asyncio.create_subprocess_exec(
            *comando,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await processo.communicate()

        if processo.returncode == 0:

            if arquivo_mp4.exists():

                tamanho = arquivo_mp4.stat().st_size

                logger.info(
                    f"[{usuario}] MP4 criado: "
                    f"{tamanho / 1024 / 1024:.2f} MB"
                )

                return True

        erro = stderr.decode(
            "utf-8",
            errors="ignore"
        )

        logger.warning(
            f"[{usuario}] Conversão principal falhou: "
            f"{erro[-1500:]}"
        )

    except Exception as e:

        logger.error(
            f"[{usuario}] Erro conversão MP4: {e}"
        )

    # --------------------------------------------------------
    # FALLBACK
    # --------------------------------------------------------

    logger.info(
        f"[{usuario}] Tentando conversão fallback."
    )

    comando_fallback = [
        "ffmpeg",

        "-hide_banner",
        "-loglevel",
        "warning",

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

        "-y",
        str(arquivo_mp4)
    ]

    try:

        processo = await asyncio.create_subprocess_exec(
            *comando_fallback,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await processo.communicate()

        if processo.returncode == 0:

            if arquivo_mp4.exists():

                tamanho = arquivo_mp4.stat().st_size

                logger.info(
                    f"[{usuario}] MP4 criado pelo fallback: "
                    f"{tamanho / 1024 / 1024:.2f} MB"
                )

                return True

    except Exception as e:

        logger.error(
            f"[{usuario}] Erro fallback MP4: {e}"
        )

    return False


# ============================================================
# ENVIAR ARQUIVO TELEGRAM
# ============================================================

async def enviar_video_telegram(
    chat_id,
    arquivo,
    usuario
):

    if not arquivo.exists():
        return False

    if arquivo.stat().st_size == 0:
        return False

    logger.info(
        f"[{usuario}] Enviando vídeo para Telegram."
    )

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendVideo"
    )

    try:

        def upload_sync():

            with open(
                arquivo,
                "rb"
            ) as video:

                response = requests.post(
                    url,
                    data={
                        "chat_id": str(chat_id),
                        "caption": (
                            f"🎥 Gravação de "
                            f"@{usuario}"
                        )
                    },
                    files={
                        "video": (
                            arquivo.name,
                            video,
                            "video/mp4"
                        )
                    },
                    timeout=300
                )

            return response.json()

        resultado = await asyncio.to_thread(
            upload_sync
        )

        if resultado.get("ok"):
            logger.info(
                f"[{usuario}] Vídeo enviado."
            )
            return True

        logger.error(
            f"[{usuario}] Erro Telegram: "
            f"{resultado}"
        )

        return False

    except Exception as e:

        logger.error(
            f"[{usuario}] Erro enviando vídeo: {e}"
        )

        return False


# ============================================================
# GRAVAR LIVE
# ============================================================

async def gravar_live(
    usuario,
    chat_id,
    automatico=False
):

    usuario = usuario.replace(
        "@",
        ""
    ).strip()

    if usuario in tarefas_gravacao:

        logger.info(
            f"[{usuario}] Já existe gravação em andamento."
        )

        return

    tarefas_gravacao[usuario] = asyncio.current_task()

    arquivo_flv = None
    arquivo_mp4 = None

    try:

        logger.info(
            f"[{usuario}] Iniciando sistema de gravação."
        )

        # ----------------------------------------------------
        # TENTAR DIRETO PELO TIKTOK
        # ----------------------------------------------------

        stream = None

        tentativas = 0

        while not shutting_down:

            tentativas += 1

            logger.info(
                f"[{usuario}] Procurando stream "
                f"(tentativa {tentativas})."
            )

            stream = await verificar_live_direto(
                usuario
            )

            if stream:
                break

            if not automatico:

                if tentativas >= MAX_TENTATIVAS_MANUAL:
                    break

            await asyncio.sleep(10)

        # ----------------------------------------------------
        # SE NÃO ENCONTROU
        # ----------------------------------------------------

        if not stream:

            logger.warning(
                f"[{usuario}] Não foi possível obter "
                f"o stream diretamente."
            )

            await enviar_mensagem(
                chat_id,
                (
                    f"⚠️ Não consegui obter o stream "
                    f"de @{usuario}.\n\n"
                    f"O TikTok está respondendo que a "
                    f"LIVE não está disponível para a API."
                )
            )

            return

        # ----------------------------------------------------
        # AVISO
        # ----------------------------------------------------

        await enviar_mensagem(
            chat_id,
            (
                f"🔴 LIVE encontrada!\n"
                f"@{usuario}\n\n"
                f"🎥 Iniciando gravação..."
            )
        )

        # ----------------------------------------------------
        # NOMES
        # ----------------------------------------------------

        timestamp = (
            asyncio.get_running_loop().time()
        )

        nome_base = (
            f"{usuario}_{int(timestamp)}"
        )

        arquivo_flv = (
            BASE_DIR /
            f"{nome_base}.flv"
        )

        arquivo_mp4 = (
            BASE_DIR /
            f"{nome_base}.mp4"
        )

        # ----------------------------------------------------
        # GRAVAR DIRETO
        # ----------------------------------------------------

        sucesso = await executar_ffmpeg_stream(
            usuario,
            stream["url"],
            arquivo_flv
        )

        if not sucesso:

            await enviar_mensagem(
                chat_id,
                (
                    f"❌ Erro ao gravar "
                    f"@{usuario}."
                )
            )

            return

        # ----------------------------------------------------
        # CONVERTER
        # ----------------------------------------------------

        sucesso_mp4 = await converter_para_mp4(
            usuario,
            arquivo_flv,
            arquivo_mp4
        )

        if not sucesso_mp4:

            await enviar_mensagem(
                chat_id,
                (
                    f"⚠️ A gravação de "
                    f"@{usuario} terminou, "
                    f"mas não consegui converter "
                    f"para MP4."
                )
            )

            return

        # ----------------------------------------------------
        # ENVIAR
        # ----------------------------------------------------

        await enviar_video_telegram(
            chat_id,
            arquivo_mp4,
            usuario
        )

    except asyncio.CancelledError:

        logger.info(
            f"[{usuario}] Tarefa de gravação cancelada."
        )

        raise

    except Exception as e:

        logger.exception(
            f"[{usuario}] Erro na gravação: {e}"
        )

        try:

            await enviar_mensagem(
                chat_id,
                (
                    f"❌ Erro inesperado ao gravar "
                    f"@{usuario}:\n{e}"
                )
            )

        except Exception:
            pass

    finally:

        # ----------------------------------------------------
        # LIMPEZA
        # ----------------------------------------------------

        if arquivo_flv:

            try:
                if arquivo_flv.exists():
                    arquivo_flv.unlink()
            except Exception:
                pass

        if arquivo_mp4:

            try:
                if arquivo_mp4.exists():
                    arquivo_mp4.unlink()
            except Exception:
                pass

        tarefas_gravacao.pop(
            usuario,
            None
        )

        logger.info(
            f"[{usuario}] Sistema de gravação finalizado."
        )


# ============================================================
# MONITORAMENTO
# ============================================================

async def monitorar_conta(
    usuario,
    chat_id
):

    usuario = usuario.replace(
        "@",
        ""
    ).strip()

    logger.info(
        f"[{usuario}] Monitoramento iniciado."
    )

    while not shutting_down:

        try:

            # Se já está gravando, não procura outra
            if usuario in tarefas_gravacao:

                await asyncio.sleep(
                    INTERVALO_MONITORAMENTO
                )

                continue

            stream = await verificar_live_direto(
                usuario
            )

            if stream:

                logger.info(
                    f"[{usuario}] 🔴 LIVE DETECTADA!"
                )

                # inicia gravação
                asyncio.create_task(
                    gravar_live(
                        usuario,
                        chat_id,
                        automatico=True
                    )
                )

                # espera um pouco para evitar
                # disparos duplicados
                await asyncio.sleep(60)

            else:

                logger.info(
                    f"[{usuario}] Offline."
                )

                await asyncio.sleep(
                    INTERVALO_MONITORAMENTO
                )

        except asyncio.CancelledError:

            logger.info(
                f"[{usuario}] Monitoramento cancelado."
            )

            break

        except Exception as e:

            logger.error(
                f"[{usuario}] Erro monitoramento: {e}"
            )

            await asyncio.sleep(
                INTERVALO_MONITORAMENTO
            )

    logger.info(
        f"[{usuario}] Monitoramento encerrado."
    )


# ============================================================
# INICIAR MONITORAMENTO
# ============================================================

async def iniciar_monitoramento(
    usuario,
    chat_id
):

    usuario = usuario.replace(
        "@",
        ""
    ).strip()

    if usuario in tarefas_monitoramento:

        return False

    tarefa = asyncio.create_task(
        monitorar_conta(
            usuario,
            chat_id
        )
    )

    tarefas_monitoramento[
        usuario
    ] = tarefa

    await salvar_monitorados()

    return True


# ============================================================
# PARAR MONITORAMENTO
# ============================================================

async def parar_monitoramento(usuario):

    usuario = usuario.replace(
        "@",
        ""
    ).strip()

    tarefa = tarefas_monitoramento.pop(
        usuario,
        None
    )

    if tarefa:

        tarefa.cancel()

        try:
            await tarefa
        except asyncio.CancelledError:
            pass

    await salvar_monitorados()

    return tarefa is not None


# ============================================================
# RESTAURAR MONITORAMENTOS
# ============================================================

async def iniciar_monitores_salvos():

    contas = await carregar_monitorados()

    if not contas:

        logger.info(
            "📭 Nenhuma conta monitorada salva."
        )

        return

    logger.info(
        f"🔄 Restaurando monitoramentos: {contas}"
    )

    # Não temos chat_id salvo na estrutura antiga.
    # Por isso procuramos um chat padrão salvo.
    chat_id = None

    if redis_client:

        try:
            chat_id = await redis_client.get(
                "monitor_chat_id"
            )
        except Exception:
            pass

    if not chat_id:

        logger.warning(
            "⚠️ Contas monitoradas existem, "
            "mas não há chat_id salvo."
        )

        return

    for usuario in contas:

        if shutting_down:
            break

        await iniciar_monitoramento(
            usuario,
            chat_id
        )

        logger.info(
            f"👁️ Monitoramento restaurado: "
            f"@{usuario}"
        )


# ============================================================
# PROCESSAR COMANDOS TELEGRAM
# ============================================================

async def processar_update(update):

    if not update:
        return

    mensagem = update.get("message")

    if not mensagem:
        return

    texto = mensagem.get(
        "text",
        ""
    ).strip()

    chat = mensagem.get(
        "chat",
        {}
    )

    chat_id = chat.get(
        "id"
    )

    if not chat_id:
        return

    if not texto:
        return

    partes = texto.split()

    comando = partes[0].lower()

    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

    if comando == "/start":

        await enviar_mensagem(
            chat_id,
            (
                "🤖 Bot de gravação TikTok LIVE ativo.\n\n"
                "/gravar usuario\n"
                "/monitorar usuario\n"
                "/desmonitorar usuario\n"
                "/monitorados"
            )
        )

        return

    # --------------------------------------------------------
    # GRAVAR
    # --------------------------------------------------------

    if comando == "/gravar":

        if len(partes) < 2:

            await enviar_mensagem(
                chat_id,
                "Use: /gravar usuario"
            )

            return

        usuario = partes[1].replace(
            "@",
            ""
        ).strip()

        if usuario in tarefas_gravacao:

            await enviar_mensagem(
                chat_id,
                (
                    f"⚠️ Já existe uma gravação "
                    f"em andamento para @{usuario}."
                )
            )

            return

        await enviar_mensagem(
            chat_id,
            (
                f"🔎 Procurando a LIVE de "
                f"@{usuario}..."
            )
        )

        asyncio.create_task(
            gravar_live(
                usuario,
                chat_id,
                automatico=False
            )
        )

        return

    # --------------------------------------------------------
    # MONITORAR
    # --------------------------------------------------------

    if comando == "/monitorar":

        if len(partes) < 2:

            await enviar_mensagem(
                chat_id,
                "Use: /monitorar usuario"
            )

            return

        usuario = partes[1].replace(
            "@",
            ""
        ).strip()

        # salva chat usado para restaurar depois
        if redis_client:

            try:
                await redis_client.set(
                    "monitor_chat_id",
                    str(chat_id)
                )
            except Exception as e:
                logger.error(
                    f"Erro salvando chat_id: {e}"
                )

        criado = await iniciar_monitoramento(
            usuario,
            chat_id
        )

        if criado:

            await enviar_mensagem(
                chat_id,
                (
                    f"👁️ Monitoramento ativado "
                    f"para @{usuario}.\n\n"
                    f"💾 Salvo permanentemente.\n"
                    f"🔄 Verificação a cada "
                    f"{INTERVALO_MONITORAMENTO} segundos."
                )
            )

        else:

            await enviar_mensagem(
                chat_id,
                (
                    f"⚠️ @{usuario} já está "
                    f"sendo monitorado."
                )
            )

        return

    # --------------------------------------------------------
    # DESMONITORAR
    # --------------------------------------------------------

    if comando == "/desmonitorar":

        if len(partes) < 2:

            await enviar_mensagem(
                chat_id,
                "Use: /desmonitorar usuario"
            )

            return

        usuario = partes[1].replace(
            "@",
            ""
        ).strip()

        removido = await parar_monitoramento(
            usuario
        )

        if removido:

            await enviar_mensagem(
                chat_id,
                (
                    f"🛑 Monitoramento removido "
                    f"para @{usuario}."
                )
            )

        else:

            await enviar_mensagem(
                chat_id,
                (
                    f"⚠️ @{usuario} não estava "
                    f"sendo monitorado."
                )
            )

        return

    # --------------------------------------------------------
    # MONITORADOS
    # --------------------------------------------------------

    if comando == "/monitorados":

        contas = sorted(
            tarefas_monitoramento.keys()
        )

        if not contas:

            await enviar_mensagem(
                chat_id,
                "📭 Nenhuma conta sendo monitorada."
            )

            return

        lista = "\n".join(
            f"• @{usuario}"
            for usuario in contas
        )

        await enviar_mensagem(
            chat_id,
            (
                "👁️ Contas monitoradas:\n\n"
                f"{lista}"
            )
        )

        return


# ============================================================
# WEBHOOK
# ============================================================

@app.post("/webhook")
async def webhook(request: Request):

    try:

        update = await request.json()

        asyncio.create_task(
            processar_update(update)
        )

        return JSONResponse(
            {
                "ok": True
            }
        )

    except Exception as e:

        logger.error(
            f"Erro webhook: {e}"
        )

        return JSONResponse(
            {
                "ok": False
            }
        )


# ============================================================
# HEALTH
# ============================================================

@app.get("/")
async def root():

    return {
        "status": "online",
        "service": "TikTok Live Recorder"
    }


@app.get("/health")
async def health():

    redis_ok = False

    if redis_client:

        try:
            await redis_client.ping()
            redis_ok = True
        except Exception:
            redis_ok = False

    return {
        "status": "ok",
        "redis": redis_ok,
        "monitorados": list(
            tarefas_monitoramento.keys()
        ),
        "gravando": list(
            tarefas_gravacao.keys()
        )
    }


# ============================================================
# CONFIGURAR WEBHOOK
# ============================================================

async def configurar_webhook():

    if not WEBHOOK_URL:
        logger.warning(
            "WEBHOOK_URL não configurado."
        )
        return

    url = (
        f"{WEBHOOK_URL.rstrip('/')}"
        f"/webhook"
    )

    logger.info(
        f"Configurando webhook: {url}"
    )

    resultado = await telegram_request(
        "setWebhook",
        {
            "url": url
        }
    )

    logger.info(
        f"Resultado webhook: {resultado}"
    )


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup():

    global shutting_down

    shutting_down = False

    logger.info(
        "🚀 Iniciando bot..."
    )

    # Redis
    await conectar_redis()

    # Webhook
    await configurar_webhook()

    # Restaurar monitoramentos
    await iniciar_monitores_salvos()

    logger.info(
        "✅ Bot iniciado."
    )


# ============================================================
# SHUTDOWN
# ============================================================

@app.on_event("shutdown")
async def shutdown():

    global shutting_down

    logger.info(
        "🛑 Iniciando shutdown..."
    )

    shutting_down = True

    # --------------------------------------------------------
    # CANCELAR MONITORES
    # --------------------------------------------------------

    tarefas = list(
        tarefas_monitoramento.values()
    )

    for tarefa in tarefas:

        if not tarefa.done():
            tarefa.cancel()

    if tarefas:

        await asyncio.gather(
            *tarefas,
            return_exceptions=True
        )

    tarefas_monitoramento.clear()

    # --------------------------------------------------------
    # SALVAR ESTADO
    # --------------------------------------------------------

    if redis_client:

        try:

            contas = await carregar_monitorados()

            await redis_client.delete(
                "monitorados"
            )

            if contas:

                await redis_client.sadd(
                    "monitorados",
                    *contas
                )

            logger.info(
                "💾 Estado salvo no Redis."
            )

        except Exception as e:

            logger.error(
                f"Erro salvando estado: {e}"
            )

        try:

            await redis_client.close()

        except Exception:
            pass

    logger.info(
        "✅ Shutdown concluído."
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT
    )
```
