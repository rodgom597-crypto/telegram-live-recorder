```python
import os
import re
import asyncio
import logging
import subprocess
from pathlib import Path

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

INTERVALO_MONITORAMENTO = 30
TENTATIVAS_MANUAL = 3

TIKTOK_TIMEOUT = 25

tarefas_monitoramento = {}
tarefas_gravacao = {}

redis_client = None

shutting_down = False

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
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,image/avif,"
            "image/webp,*/*;q=0.8"
        ),
        "Accept-Language": (
            "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"
        ),
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
        "Accept": (
            "application/json, text/plain, */*"
        ),
        "Accept-Language": (
            "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"
        ),
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

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )

    try:

        def enviar():

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
                    "status": response.status_code,
                    "text": response.text[:500]
                }

        return await asyncio.to_thread(enviar)

    except Exception as e:

        logger.error(
            f"Erro Telegram: {e}"
        )

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

        logger.warning(
            "REDIS_URL não configurado."
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
            f"❌ Erro conectando ao Redis: {e}"
        )

        redis_client = None

        return False


async def salvar_monitorados():

    if not redis_client:
        return

    try:

        contas = sorted(
            tarefas_monitoramento.keys()
        )

        await redis_client.delete(
            "monitorados"
        )

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
# ROOM ID
# ============================================================

def extrair_room_id(texto):

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
# PEGAR ROOM ID
# ============================================================

async def obter_room_id(usuario):

    usuario = usuario.replace(
        "@",
        ""
    ).strip()

    url = (
        f"https://www.tiktok.com/"
        f"@{usuario}/live"
    )

    logger.info(
        f"[{usuario}] 🔎 Buscando página do TikTok..."
    )

    try:

        def buscar():

            return http_session.get(
                url,
                headers=headers_tiktok(),
                timeout=TIKTOK_TIMEOUT,
                allow_redirects=True
            )

        response = await asyncio.to_thread(
            buscar
        )

        logger.info(
            f"[{usuario}] TikTok respondeu "
            f"HTTP {response.status_code}"
        )

        if response.status_code != 200:

            return None

        texto = response.text

        room_id = extrair_room_id(
            texto
        )

        if room_id:

            logger.info(
                f"[{usuario}] ✅ Room ID encontrado: "
                f"{room_id}"
            )

            return room_id

        logger.warning(
            f"[{usuario}] ❌ Room ID não encontrado."
        )

        return None

    except Exception as e:

        logger.error(
            f"[{usuario}] Erro buscando Room ID: {e}"
        )

        return None


# ============================================================
# API ROOM INFO
# ============================================================

async def consultar_room_info(
    usuario,
    room_id
):

    url = (
        "https://webcast.tiktok.com/"
        "webcast/room/info"
    )

    params = {
        "aid": "1988",
        "room_id": room_id
    }

    logger.info(
        f"[{usuario}] 🌐 Consultando API direta "
        f"do TikTok..."
    )

    try:

        def consultar():

            return http_session.get(
                url,
                params=params,
                headers=headers_api_tiktok(),
                timeout=TIKTOK_TIMEOUT
            )

        response = await asyncio.to_thread(
            consultar
        )

        logger.info(
            f"[{usuario}] API room/info respondeu "
            f"HTTP {response.status_code}"
        )

        if response.status_code != 200:

            logger.warning(
                f"[{usuario}] API retornou "
                f"HTTP {response.status_code}"
            )

            return None

        try:

            data = response.json()

        except Exception:

            logger.error(
                f"[{usuario}] API não retornou JSON."
            )

            return None

        return data

    except Exception as e:

        logger.error(
            f"[{usuario}] Erro na API: {e}"
        )

        return None


# ============================================================
# ENCONTRAR URLS
# ============================================================

def encontrar_urls_stream(data):

    urls = []

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
                        )
                    ):

                        urls.append(valor)

                elif isinstance(
                    valor,
                    (dict, list)
                ):

                    percorrer(valor)

        elif isinstance(obj, list):

            for item in obj:

                percorrer(item)

    percorrer(data)

    resultado = []

    for url in urls:

        if url not in resultado:

            resultado.append(url)

    return resultado


# ============================================================
# ESCOLHER STREAM
# ============================================================

def escolher_stream(urls):

    for url in urls:

        if ".flv" in url.lower():

            return url

    for url in urls:

        if ".m3u8" in url.lower():

            return url

    if urls:

        return urls[0]

    return None


# ============================================================
# DESCOBRIR STREAM
# ============================================================

async def descobrir_stream(usuario):

    usuario = usuario.replace(
        "@",
        ""
    ).strip()

    # --------------------------------------------------------
    # ROOM ID
    # --------------------------------------------------------

    room_id = await obter_room_id(
        usuario
    )

    if not room_id:

        return None

    # --------------------------------------------------------
    # API
    # --------------------------------------------------------

    data = await consultar_room_info(
        usuario,
        room_id
    )

    if not data:

        return None

    # --------------------------------------------------------
    # LOG DO STATUS
    # --------------------------------------------------------

    status = data.get(
        "status"
    )

    logger.info(
        f"[{usuario}] API status: {status}"
    )

    # --------------------------------------------------------
    # URLS
    # --------------------------------------------------------

    urls = encontrar_urls_stream(
        data
    )

    logger.info(
        f"[{usuario}] 🔗 URLs encontradas: "
        f"{len(urls)}"
    )

    if not urls:

        logger.warning(
            f"[{usuario}] Nenhuma URL de stream encontrada."
        )

        return None

    stream_url = escolher_stream(
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
        f"[{usuario}] 🎥 Stream encontrado: {tipo}"
    )

    return {
        "room_id": room_id,
        "url": stream_url,
        "tipo": tipo
    }


# ============================================================
# FFMPEG
# ============================================================

async def gravar_stream(
    usuario,
    stream_url,
    arquivo
):

    logger.info(
        f"[{usuario}] 🎬 Iniciando FFmpeg direto."
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
        str(arquivo)
    ]

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
                f"[{usuario}] FFmpeg erro "
                f"{processo.returncode}: "
                f"{erro[-2000:]}"
            )

            return False

        if not arquivo.exists():

            logger.error(
                f"[{usuario}] Arquivo não foi criado."
            )

            return False

        tamanho = arquivo.stat().st_size

        logger.info(
            f"[{usuario}] ✅ FLV gravado: "
            f"{tamanho / 1024 / 1024:.2f} MB"
        )

        return tamanho > 100000

    except asyncio.CancelledError:

        if processo:

            try:
                processo.terminate()
            except Exception:
                pass

        raise

    except Exception as e:

        logger.error(
            f"[{usuario}] Erro FFmpeg: {e}"
        )

        return False


# ============================================================
# CONVERTER MP4
# ============================================================

async def converter_mp4(
    usuario,
    flv,
    mp4
):

    logger.info(
        f"[{usuario}] 🔄 Convertendo para MP4..."
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
        str(flv),

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
        str(mp4)
    ]

    try:

        processo = await asyncio.create_subprocess_exec(
            *comando,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await processo.communicate()

        if processo.returncode == 0:

            if mp4.exists():

                tamanho = mp4.stat().st_size

                logger.info(
                    f"[{usuario}] ✅ MP4 criado: "
                    f"{tamanho / 1024 / 1024:.2f} MB"
                )

                return True

        erro = stderr.decode(
            "utf-8",
            errors="ignore"
        )

        logger.error(
            f"[{usuario}] Erro conversão: "
            f"{erro[-2000:]}"
        )

    except Exception as e:

        logger.error(
            f"[{usuario}] Erro convertendo: {e}"
        )

    return False


# ============================================================
# ENVIAR VÍDEO
# ============================================================

async def enviar_video(
    chat_id,
    arquivo,
    usuario
):

    if not arquivo.exists():

        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendVideo"
    )

    try:

        def enviar():

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
                    timeout=600
                )

            return response.json()

        resultado = await asyncio.to_thread(
            enviar
        )

        if resultado.get("ok"):

            logger.info(
                f"[{usuario}] ✅ Vídeo enviado ao Telegram."
            )

            return True

        logger.error(
            f"[{usuario}] Telegram recusou vídeo: "
            f"{resultado}"
        )

        return False

    except Exception as e:

        logger.error(
            f"[{usuario}] Erro enviando vídeo: {e}"
        )

        return False


# ============================================================
# GRAVAÇÃO
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
            f"[{usuario}] Já está gravando."
        )

        return

    tarefas_gravacao[
        usuario
    ] = asyncio.current_task()

    arquivo_flv = None
```
