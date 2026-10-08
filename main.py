import os
import re
import asyncio
import logging
import subprocess
import time
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

# Cada arquivo terá aproximadamente 5 minutos
SEGMENTO_MINUTOS = 5

# Quantas vezes tentar reconectar uma gravação
MAX_RECONEXOES = 30

# Tempo entre tentativas de reconexão
INTERVALO_RECONEXAO = 10

redis_client = None

shutting_down = False

http_session = requests.Session()

tarefas_monitoramento = {}

gravacoes_ativas = {}


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
# TELEGRAM
# ============================================================

def telegram_url(endpoint):
    return (
        f"https://api.telegram.org/bot"
        f"{BOT_TOKEN}/{endpoint}"
    )


async def telegram_request(
    endpoint,
    payload=None,
    files=None
):
    url = telegram_url(endpoint)

    def request_sync():

        try:

            if files:

                return http_session.post(
                    url,
                    data=payload or {},
                    files=files,
                    timeout=180
                )

            return http_session.post(
                url,
                json=payload or {},
                timeout=60
            )

        except Exception as e:

            logger.error(
                f"Erro Telegram {endpoint}: {e}"
            )

            return None

    return await asyncio.to_thread(
        request_sync
    )


async def enviar_mensagem(
    chat_id,
    texto
):

    if not chat_id:
        return

    await telegram_request(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": texto
        }
    )


async def enviar_video(
    chat_id,
    arquivo,
    legenda=None
):

    if not chat_id:
        return False

    if not arquivo.exists():

        logger.error(
            f"Arquivo não encontrado: {arquivo}"
        )

        return False

    def upload_sync():

        try:

            with open(
                arquivo,
                "rb"
            ) as video:

                files = {
                    "video": (
                        arquivo.name,
                        video,
                        "video/mp4"
                    )
                }

                data = {
                    "chat_id": str(chat_id)
                }

                if legenda:
                    data["caption"] = legenda

                resposta = http_session.post(
                    telegram_url("sendVideo"),
                    data=data,
                    files=files,
                    timeout=600
                )

                return resposta

        except Exception as e:

            logger.error(
                f"Erro ao enviar vídeo: {e}"
            )

            return None

    resposta = await asyncio.to_thread(
        upload_sync
    )

    if resposta is None:
        return False

    if resposta.ok:

        logger.info(
            f"Vídeo enviado para chat {chat_id}."
        )

        return True

    logger.error(
        f"Telegram HTTP {resposta.status_code}: "
        f"{resposta.text[:500]}"
    )

    return False


# ============================================================
# REDIS
# ============================================================

async def conectar_redis():

    global redis_client

    if not REDIS_URL:

        logger.warning(
            "REDIS_URL não configurada."
        )

        return False

    try:

        redis_client = redis.from_url(
            REDIS_URL,
            decode_responses=True
        )

        await redis_client.ping()

        logger.info(
            "Redis conectado com sucesso."
        )

        return True

    except Exception as e:

        logger.error(
            f"Erro Redis: {e}"
        )

        redis_client = None

        return False


async def salvar_monitorados():

    if not redis_client:
        return

    try:

        await redis_client.delete(
            "tiktok_monitorados"
        )

        for usuario, dados in (
            tarefas_monitoramento.items()
        ):

            await redis_client.hset(
                "tiktok_monitorados",
                usuario,
                str(dados["chat_id"])
            )

        logger.info(
            "Monitorados salvos no Redis."
        )

    except Exception as e:

        logger.error(
            f"Erro ao salvar monitorados: {e}"
        )


async def carregar_monitorados():

    if not redis_client:
        return {}

    try:

        dados = await redis_client.hgetall(
            "tiktok_monitorados"
        )

        logger.info(
            f"Monitorados carregados: "
            f"{list(dados.keys())}"
        )

        return dados

    except Exception as e:

        logger.error(
            f"Erro ao carregar monitorados: {e}"
        )

        return {}


# ============================================================
# TIKTOK
# ============================================================

def headers_tiktok():

    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,image/avif,"
            "image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/",
        "Connection": "keep-alive"
    }


def obter_room_id(usuario):

    url = (
        f"https://www.tiktok.com/"
        f"@{usuario}/live"
    )

    try:

        resposta = http_session.get(
            url,
            headers=headers_tiktok(),
            timeout=TIKTOK_TIMEOUT
        )

        if resposta.status_code != 200:

            logger.info(
                f"[{usuario}] TikTok HTTP "
                f"{resposta.status_code}"
            )

            return None

        texto = resposta.text

        padroes = [
            r'"roomId":"(\d+)"',
            r'"room_id":"(\d+)"',
            r'"roomId":(\d+)',
            r'"room_id":(\d+)'
        ]

        for padrao in padroes:

            encontrado = re.search(
                padrao,
                texto
            )

            if encontrado:

                room_id = encontrado.group(1)

                logger.info(
                    f"[{usuario}] Room ID encontrado: "
                    f"{room_id}"
                )

                return room_id

        return None

    except Exception as e:

        logger.error(
            f"[{usuario}] Erro Room ID: {e}"
        )

        return None


def consultar_room_info(
    room_id,
    usuario
):

    url = (
        "https://webcast.tiktok.com/"
        "webcast/room/info"
    )

    params = {
        "aid": "1988",
        "room_id": room_id
    }

    try:

        resposta = http_session.get(
            url,
            params=params,
            headers=headers_tiktok(),
            timeout=TIKTOK_TIMEOUT
        )

        logger.info(
            f"[{usuario}] Room info HTTP "
            f"{resposta.status_code}"
        )

        if resposta.status_code != 200:
            return None

        return resposta.json()

    except Exception as e:

        logger.error(
            f"[{usuario}] Erro room info: {e}"
        )

        return None


def encontrar_urls_stream(obj):

    urls = []

    if isinstance(obj, dict):

        for valor in obj.values():

            if isinstance(valor, str):

                valor_lower = valor.lower()

                if (
                    ".flv" in valor_lower
                    or ".m3u8" in valor_lower
                    or "pull-" in valor_lower
                    or "pull." in valor_lower
                ):

                    urls.append(valor)

            elif isinstance(
                valor,
                (dict, list)
            ):

                urls.extend(
                    encontrar_urls_stream(
                        valor
                    )
                )

    elif isinstance(obj, list):

        for item in obj:

            urls.extend(
                encontrar_urls_stream(
                    item
                )
            )

    return urls


def escolher_stream(urls):

    if not urls:
        return None

    # Preferência FLV
    for url in urls:

        if ".flv" in url.lower():
            return url

    # Depois HLS
    for url in urls:

        if ".m3u8" in url.lower():
            return url

    for url in urls:

        if (
            "pull-" in url.lower()
            or "pull." in url.lower()
        ):

            return url

    return None


def descobrir_stream(usuario):

    room_id = obter_room_id(
        usuario
    )

    if not room_id:
        return None

    dados = consultar_room_info(
        room_id,
        usuario
    )

    if not dados:
        return None

    urls = encontrar_urls_stream(
        dados
    )

    urls = list(
        dict.fromkeys(urls)
    )

    logger.info(
        f"[{usuario}] Encontradas "
        f"{len(urls)} URLs de stream."
    )

    stream = escolher_stream(
        urls
    )

    if stream:

        logger.info(
            f"[{usuario}] Stream escolhida: "
            f"{stream[:180]}"
        )

    return stream


# ============================================================
# FFmpeg - GRAVAÇÃO EM SEGMENTOS
# ============================================================

async def gravar_segmento(
    usuario,
    stream_url,
    pasta,
    numero_segmento,
    controle
):

    arquivo = (
        pasta /
        f"segmento_{numero_segmento:05d}.flv"
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

        "-reconnect_at_eof",
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
        "segment",

        "-segment_time",
        str(SEGMENTO_MINUTOS * 60),

        "-reset_timestamps",
        "1",

        "-segment_format",
        "flv",

        "-y",

        str(
            pasta /
            "segmento_%05d.flv"
        )
    ]

    logger.info(
        f"[{usuario}] Iniciando segmento "
        f"{numero_segmento}."
    )

    try:

        processo = (
            await asyncio.create_subprocess_exec(
                *comando,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE
            )
        )

        controle["processo"] = processo

        while True:

            if controle.get("parar"):

                logger.info(
                    f"[{usuario}] Parada solicitada."
                )

                try:
                    processo.terminate()
                except Exception:
                    pass

                try:

                    await asyncio.wait_for(
                        processo.wait(),
                        timeout=10
                    )

                except asyncio.TimeoutError:

                    try:
                        processo.kill()
                    except Exception:
                        pass

                    try:
                        await processo.wait()
                    except Exception:
                        pass

                return "parado"

            if processo.returncode is not None:
                break

            try:

                linha = await asyncio.wait_for(
                    processo.stderr.readline(),
                    timeout=1
                )

                if linha:

                    texto = linha.decode(
                        errors="ignore"
                    ).strip()

                    if texto:

                        logger.info(
                            f"[{usuario}] {texto}"
                        )

            except asyncio.TimeoutError:
                pass

        codigo = processo.returncode

        logger.info(
            f"[{usuario}] FFmpeg terminou "
            f"segmento. Código: {codigo}"
        )

        if codigo == 0:
            return "normal"

        return "erro"

    except Exception as e:

        logger.error(
            f"[{usuario}] Erro FFmpeg: {e}"
        )

        return "erro"


# ============================================================
# RECONEXÃO DA GRAVAÇÃO
# ============================================================

async def gravar_com_reconexao(
    usuario,
    chat_id,
    pasta,
    controle
):

    numero = 0

    reconexoes = 0

    while not shutting_down:

        if controle.get("parar"):
            break

        logger.info(
            f"[{usuario}] Procurando stream "
            f"para gravação..."
        )

        stream_url = await asyncio.to_thread(
            descobrir_stream,
            usuario
        )

        if not stream_url:

            reconexoes += 1

            if reconexoes > MAX_RECONEXOES:

                logger.error(
                    f"[{usuario}] Limite de "
                    f"reconexões atingido."
                )

                break

            logger.warning(
                f"[{usuario}] Stream não encontrada. "
                f"Reconexão {reconexoes}/"
                f"{MAX_RECONEXOES}."
            )

            await asyncio.sleep(
                INTERVALO_RECONEXAO
            )

            continue

        reconexoes = 0

        resultado = await gravar_segmento(
            usuario,
            stream_url,
            pasta,
            numero,
            controle
        )

        if resultado == "parado":
            break

        # Descobre quantos arquivos existem
        arquivos = sorted(
            pasta.glob(
                "segmento_*.flv"
            )
        )

        if arquivos:

            ultimo = arquivos[-1]

            try:

                numero = (
                    int(
                        ultimo.stem
                        .split("_")[1]
                    ) + 1
                )

            except Exception:

                numero += 1

        else:

            numero += 1

        if resultado == "normal":

            logger.info(
                f"[{usuario}] Segmento terminou "
                f"normalmente."
            )

            # FFmpeg pode ter encerrado porque
            # a conexão acabou.
            # Verificamos novamente se a LIVE
            # ainda existe.

            await asyncio.sleep(2)

            if controle.get("parar"):
                break

            continue

        # Erro do FFmpeg
        reconexoes += 1

        logger.warning(
            f"[{usuario}] FFmpeg caiu. "
            f"Reconectando..."
        )

        if reconexoes > MAX_RECONEXOES:

            logger.error(
                f"[{usuario}] Limite de "
                f"reconexões atingido."
            )

            break

        await asyncio.sleep(
            INTERVALO_RECONEXAO
        )

    return


# ============================================================
# JUNTAR SEGMENTOS
# ============================================================

async def juntar_segmentos(
    pasta,
    arquivo_saida
):

    segmentos = sorted(
        pasta.glob(
            "segmento_*.flv"
        )
    )

    segmentos_validos = []

    for arquivo in segmentos:

        try:

            if (
                arquivo.exists()
                and arquivo.stat().st_size > 0
            ):

                segmentos_validos.append(
                    arquivo
                )

        except Exception:
            pass

    if not segmentos_validos:

        logger.error(
            "Nenhum segmento válido encontrado."
        )

        return False

    logger.info(
        f"Encontrados "
        f"{len(segmentos_validos)} segmentos."
    )

    # Arquivo concat
    lista = pasta / "lista.txt"

    try:

        with open(
            lista,
            "w",
            encoding="utf-8"
        ) as f:

            for arquivo in segmentos_validos:

                caminho = (
                    str(
                        arquivo.resolve()
                    )
                    .replace(
                        "\\",
                        "/"
                    )
                    .replace(
                        "'",
                        "'\\''"
                    )
                )

                f.write(
                    f"file '{caminho}'\n"
                )

    except Exception as e:

        logger.error(
            f"Erro criando lista: {e}"
        )

        return False

    comando = [
        "ffmpeg",

        "-hide_banner",

        "-loglevel",
        "warning",

        "-f",
        "concat",

        "-safe",
        "0",

        "-i",
        str(lista),

        "-c",
        "copy",

        "-movflags",
        "+faststart",

        "-y",

        str(arquivo_saida)
    ]

    logger.info(
        "Juntando segmentos em MP4..."
    )

    try:

        processo = (
            await asyncio.create_subprocess_exec(
                *comando,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
        )

        stdout, stderr = (
            await processo.communicate()
        )

        if processo.returncode == 0:

            if (
                arquivo_saida.exists()
                and arquivo_saida.stat().st_size > 0
            ):

                logger.info(
                    "MP4 criado com sucesso."
                )

                return True

        erro = stderr.decode(
            errors="ignore"
        )

        logger.warning(
            f"Concatenação falhou:\n"
            f"{erro[-4000:]}"
        )

        return False

    except Exception as e:

        logger.error(
            f"Erro juntando segmentos: {e}"
        )

        return False


# ============================================================
# FALLBACK DE REENCODE
# ============================================================

async def reencodar_segmentos(
    pasta,
    arquivo_saida
):

    segmentos = sorted(
        pasta.glob(
            "segmento_*.flv"
        )
    )

    segmentos_validos = [
        x for x in segmentos
        if x.exists()
        and x.stat().st_size > 0
    ]

    if not segmentos_validos:
        return False

    lista = pasta / "lista_reencode.txt"

    try:

        with open(
            lista,
            "w",
            encoding="utf-8"
        ) as f:

            for arquivo in segmentos_validos:

                caminho = (
                    str(
                        arquivo.resolve()
                    )
                    .replace(
                        "\\",
                        "/"
                    )
                    .replace(
                        "'",
                        "'\\''"
                    )
                )

                f.write(
                    f"file '{caminho}'\n"
                )

    except Exception as e:

        logger.error(
            f"Erro lista reencode: {e}"
        )

        return False

    comando = [
        "ffmpeg",

        "-hide_banner",

        "-loglevel",
        "warning",

        "-f",
        "concat",

        "-safe",
        "0",

        "-i",
        str(lista),

        "-fflags",
        "+genpts+discardcorrupt",

        "-err_detect",
        "ignore_err",

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

        str(arquivo_saida)
    ]

    logger.info(
        "Executando fallback com reencode..."
    )

    try:

        processo = (
            await asyncio.create_subprocess_exec(
                *comando,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
        )

        stdout, stderr = (
            await processo.communicate()
        )

        if processo.returncode == 0:

            if (
                arquivo_saida.exists()
                and arquivo_saida.stat().st_size > 0
            ):

                return True

        erro = stderr.decode(
            errors="ignore"
        )

        logger.error(
            f"Reencode falhou:\n"
            f"{erro[-5000:]}"
        )

        return False

    except Exception as e:

        logger.error(
            f"Erro reencode: {e}"
        )

        return False


# ============================================================
# FINALIZAÇÃO
# ============================================================

async def finalizar_gravacao(
    usuario,
    chat_id,
    pasta
):

    segmentos = sorted(
        pasta.glob(
            "segmento_*.flv"
        )
    )

    validos = [
        x for x in segmentos
        if x.exists()
        and x.stat().st_size > 0
    ]

    if not validos:

        await enviar_mensagem(
            chat_id,
            f"⚠️ Nenhum arquivo foi salvo "
            f"para @{usuario}."
        )

        return False

    tamanho_total = sum(
        x.stat().st_size
        for x in validos
    )

    logger.info(
        f"[{usuario}] Total gravado: "
        f"{tamanho_total / 1024 / 1024:.2f} MB"
    )

    await enviar_mensagem(
        chat_id,
        f"⏳ Gravação de @{usuario} "
        f"finalizada.\n"
        f"🎞️ {len(validos)} partes gravadas.\n"
        f"📦 {tamanho_total / 1024 / 1024:.2f} MB\n"
        f"🔧 Montando MP4..."
    )

    arquivo_mp4 = (
        pasta /
        f"{usuario}.mp4"
    )

    inicio = time.monotonic()

    sucesso = await juntar_segmentos(
        pasta,
        arquivo_mp4
    )

    if not sucesso:

        logger.warning(
            f"[{usuario}] Copy falhou. "
            f"Tentando reencode."
        )

        sucesso = await reencodar_segmentos(
            pasta,
            arquivo_mp4
        )

    tempo = (
        time.monotonic()
        - inicio
    )

    if not sucesso:

        await enviar_mensagem(
            chat_id,
            f"❌ Não consegui montar o MP4 "
            f"de @{usuario}.\n"
            f"Os arquivos temporários foram "
            f"mantidos no servidor durante esta sessão."
        )

        return False

    tamanho_mp4 = (
        arquivo_mp4.stat().st_size
    )

    logger.info(
        f"[{usuario}] MP4 pronto em "
        f"{tempo:.1f}s."
    )

    await enviar_mensagem(
        chat_id,
        f"✅ MP4 pronto!\n"
        f"⏱️ Montagem: {tempo:.1f}s\n"
        f"📦 {tamanho_mp4 / 1024 / 1024:.2f} MB\n"
        f"📤 Enviando..."
    )

    enviado = await enviar_video(
        chat_id,
        arquivo_mp4,
        f"🎥 Gravação @{usuario}"
    )

    if enviado:

        await enviar_mensagem(
            chat_id,
            f"✅ Gravação de @{usuario} "
            f"enviada com sucesso!"
        )

        # Só apaga depois do envio
        try:

            for arquivo in pasta.glob("*"):

                try:
                    arquivo.unlink()
                except Exception:
                    pass

            pasta.rmdir()

        except Exception as e:

            logger.warning(
                f"Erro limpando arquivos: {e}"
            )

        return True

    await enviar_mensagem(
        chat_id,
        f"⚠️ O MP4 foi criado, "
        f"mas o Telegram não confirmou "
        f"o envio.\n\n"
        f"Não vou apagar os arquivos."
    )

    return False


# ============================================================
# GRAVAÇÃO COMPLETA
# ============================================================

async def gravar_live(
    usuario,
    chat_id
):

    usuario = (
        usuario
        .strip()
        .lstrip("@")
        .lower()
    )

    if usuario in gravacoes_ativas:

        await enviar_mensagem(
            chat_id,
            f"⚠️ @{usuario} já está gravando."
        )

        return

    timestamp = int(
        time.time()
    )

    pasta = (
        BASE_DIR /
        f"{usuario}_{timestamp}"
    )

    pasta.mkdir(
        parents=True,
        exist_ok=True
    )

    controle = {
        "parar": False,
        "processo": None,
        "chat_id": chat_id,
        "pasta": pasta,
        "inicio": time.time()
    }

    gravacoes_ativas[
        usuario
    ] = controle

    try:

        await enviar_mensagem(
            chat_id,
            f"🔎 Procurando LIVE de "
            f"@{usuario}..."
        )

        stream_url = None

        for tentativa in range(
            1,
            TENTATIVAS_MANUAL + 1
        ):

            if controle["parar"]:
                return

            stream_url = await asyncio.to_thread(
                descobrir_stream,
                usuario
            )

            if stream_url:
                break

            if tentativa < TENTATIVAS_MANUAL:

                await asyncio.sleep(5)

        if not stream_url:

            await enviar_mensagem(
                chat_id,
                f"❌ Não consegui encontrar "
                f"a LIVE de @{usuario}."
            )

            return

        await enviar_mensagem(
            chat_id,
            f"🔴 LIVE encontrada!\n"
            f"🎥 Gravação iniciada.\n\n"
            f"🔄 O sistema ficará tentando "
            f"reconectar automaticamente "
            f"se o TikTok derrubar a conexão."
        )

        logger.info(
            f"[{usuario}] Gravação iniciada."
        )

        await gravar_com_reconexao(
            usuario,
            chat_id,
            pasta,
            controle
        )

        if shutting_down:

            logger.warning(
                f"[{usuario}] Aplicação "
                f"está sendo desligada. "
                f"Não iniciar conversão."
            )

            return

        logger.info(
            f"[{usuario}] Gravação encerrada."
        )

        await finalizar_gravacao(
            usuario,
            chat_id,
            pasta
        )

    except Exception as e:

        logger.exception(
            f"[{usuario}] Erro gravação."
        )

        await enviar_mensagem(
            chat_id,
            f"❌ Erro na gravação "
            f"de @{usuario}:\n"
            f"{str(e)[:500]}"
        )

    finally:

        gravacoes_ativas.pop(
            usuario,
            None
        )


# ============================================================
# PARAR
# ============================================================

async def parar_gravacao(
    usuario,
    chat_id
):

    usuario = (
        usuario
        .strip()
        .lstrip("@")
        .lower()
    )

    controle = gravacoes_ativas.get(
        usuario
    )

    if not controle:

        await enviar_mensagem(
            chat_id,
            f"⚠️ Não existe gravação ativa "
            f"de @{usuario}."
        )

        return

    controle["parar"] = True

    processo = controle.get(
        "processo"
    )

    if processo:

        try:

            processo.terminate()

        except Exception:
            pass

    await enviar_mensagem(
        chat_id,
        f"⏹️ Parando gravação de "
        f"@{usuario}..."
    )


# ============================================================
# MONITORAMENTO
# ============================================================

async def monitorar_usuario(
    usuario,
    chat_id
):

    usuario = (
        usuario
        .strip()
        .lstrip("@")
        .lower()
    )

    logger.info(
        f"[{usuario}] Monitoramento iniciado."
    )

    estava_online = False

    while not shutting_down:

        try:

            if usuario in gravacoes_ativas:

                estava_online = True

                await asyncio.sleep(
                    INTERVALO_MONITORAMENTO
                )

                continue

            stream_url = await asyncio.to_thread(
                descobrir_stream,
                usuario
            )

            online = bool(stream_url)

            if online and not estava_online:

                logger.info(
                    f"[{usuario}] LIVE detectada."
                )

                estava_online = True

                asyncio.create_task(
                    gravar_live(
                        usuario,
                        chat_id
                    )
                )

            elif not online:

                if estava_online:

                    logger.info(
                        f"[{usuario}] LIVE encerrada."
                    )

                estava_online = False

        except asyncio.CancelledError:

            break

        except Exception as e:

            logger.error(
                f"[{usuario}] Erro monitoramento: "
                f"{e}"
            )

        await asyncio.sleep(
            INTERVALO_MONITORAMENTO
        )

    logger.info(
        f"[{usuario}] Monitoramento encerrado."
    )


async def iniciar_monitores_salvos():

    dados = await carregar_monitorados()

    for usuario, chat_id in dados.items():

        if usuario in tarefas_monitoramento:
            continue

        tarefa = asyncio.create_task(
            monitorar_usuario(
                usuario,
                int(chat_id)
            )
        )

        tarefas_monitoramento[
            usuario
        ] = {
            "task": tarefa,
            "chat_id": int(chat_id)
        }

        logger.info(
            f"[{usuario}] Monitor restaurado."
        )


# ============================================================
# COMANDOS TELEGRAM
# ============================================================

async def processar_comando(
    chat_id,
    texto
):

    texto = texto.strip()

    if not texto:
        return

    partes = texto.split()

    comando = partes[0].lower()

    if comando.startswith("/"):

        comando = comando.split("@")[0]

    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

    if comando == "/start":

        await enviar_mensagem(
            chat_id,
            "🤖 Bot TikTok LIVE Recorder\n\n"
            "/gravar usuario\n"
            "/parar usuario\n"
            "/monitorar usuario\n"
            "/desmonitorar usuario\n"
            "/monitorados"
        )

        return

    # --------------------------------------------------------
    # GRAVAR
    # --------------------------------------------------------

    if comando == "/gravar":

        if len(partes) < 2:

            await enviar_mensagem(
                chat_id,
                "Use:\n"
                "/gravar usuario"
            )

            return

        usuario = (
            partes[1]
            .lstrip("@")
            .lower()
        )

        if usuario in gravacoes_ativas:

            await enviar_mensagem(
                chat_id,
                f"⚠️ @{usuario} "
                f"já está sendo gravado."
            )

            return

        await enviar_mensagem(
            chat_id,
            f"📥 Solicitação de gravação "
            f"enviada para @{usuario}."
        )

        asyncio.create_task(
            gravar_live(
                usuario,
                chat_id
            )
        )

        return

    # --------------------------------------------------------
    # PARAR
    # --------------------------------------------------------

    if comando == "/parar":

        if len(partes) < 2:

            await enviar_mensagem(
                chat_id,
                "Use:\n"
                "/parar usuario"
            )

            return

        await parar_gravacao(
            partes[1],
            chat_id
        )

        return

    # --------------------------------------------------------
    # MONITORAR
    # --------------------------------------------------------

    if comando == "/monitorar":

        if len(partes) < 2:

            await enviar_mensagem(
                chat_id,
                "Use:\n"
                "/monitorar usuario"
            )

            return

        usuario = (
            partes[1]
            .lstrip("@")
            .lower()
        )

        if usuario in tarefas_monitoramento:

            await enviar_mensagem(
                chat_id,
                f"⚠️ @{usuario} "
                f"já está sendo monitorado."
            )

            return

        tarefa = asyncio.create_task(
            monitorar_usuario(
                usuario,
                chat_id
            )
        )

        tarefas_monitoramento[
            usuario
        ] = {
            "task": tarefa,
            "chat_id": chat_id
        }

        await salvar_monitorados()

        await enviar_mensagem(
            chat_id,
            f"👁️ @{usuario} "
            f"agora está sendo monitorado."
        )

        return

    # --------------------------------------------------------
    # DESMONITORAR
    # --------------------------------------------------------

    if comando == "/desmonitorar":

        if len(partes) < 2:

            await enviar_mensagem(
                chat_id,
                "Use:\n"
                "/desmonitorar usuario"
            )

            return

        usuario = (
            partes[1]
            .lstrip("@")
            .lower()
        )

        dados = tarefas_monitoramento.pop(
            usuario,
            None
        )

        if not dados:

            await enviar_mensagem(
                chat_id,
                f"⚠️ @{usuario} "
                f"não está sendo monitorado."
            )

            return

        task = dados.get("task")

        if task:
            task.cancel()

        await salvar_monitorados()

        await enviar_mensagem(
            chat_id,
            f"🛑 @{usuario} "
            f"foi removido do monitoramento."
        )

        return

    # --------------------------------------------------------
    # MONITORADOS
    # --------------------------------------------------------

    if comando == "/monitorados":

        if not tarefas_monitoramento:

            await enviar_mensagem(
                chat_id,
                "📋 Nenhum usuário monitorado."
            )

            return

        linhas = [
            "📋 Usuários monitorados:"
        ]

        for usuario in sorted(
            tarefas_monitoramento.keys()
        ):

            if usuario in gravacoes_ativas:

                linhas.append(
                    f"🔴 @{usuario} — GRAVANDO"
                )

            else:

                linhas.append(
                    f"🟢 @{usuario} — MONITORANDO"
                )

        await enviar_mensagem(
            chat_id,
            "\n".join(linhas)
        )

        return

    await enviar_mensagem(
        chat_id,
        "❓ Comando não reconhecido.\n\n"
        "Use /start."
    )


# ============================================================
# WEBHOOK
# ============================================================

@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request
):

    try:

        update = await request.json()

        mensagem = update.get(
            "message"
        )

        if not mensagem:

            return JSONResponse(
                {"ok": True}
            )

        chat = mensagem.get(
            "chat"
        )

        if not chat:

            return JSONResponse(
                {"ok": True}
            )

        chat_id = chat.get(
            "id"
        )

        texto = mensagem.get(
            "text",
            ""
        )

        if texto:

            asyncio.create_task(
                processar_comando(
                    chat_id,
                    texto
                )
            )

        return JSONResponse(
            {"ok": True}
        )

    except Exception as e:

        logger.exception(
            f"Erro webhook: {e}"
        )

        return JSONResponse(
            {"ok": True}
        )


@app.post("/webhook")
async def webhook_alias(
    request: Request
):

    return await telegram_webhook(
        request
    )


# ============================================================
# HEALTH
# ============================================================

@app.get("/")
async def root():

    return {
        "status": "online",
        "version": "V5",
        "gravando": list(
            gravacoes_ativas.keys()
        ),
        "monitorados": list(
            tarefas_monitoramento.keys()
        )
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
        "version": "V5",
        "redis": redis_ok,
        "gravando": list(
            gravacoes_ativas.keys()
        ),
        "monitorados": list(
            tarefas_monitoramento.keys()
        )
    }


# ============================================================
# WEBHOOK
# ============================================================

async def configurar_webhook():

    if not BOT_TOKEN:

        logger.error(
            "BOT_TOKEN não configurado."
        )

        return

    if not WEBHOOK_URL:

        logger.warning(
            "WEBHOOK_URL não configurada."
        )

        return

    url = (
        WEBHOOK_URL.rstrip("/")
        + "/telegram/webhook"
    )

    logger.info(
        f"Configurando webhook: {url}"
    )

    resposta = await telegram_request(
        "setWebhook",
        {
            "url": url
        }
    )

    if resposta and resposta.ok:

        logger.info(
            "Webhook Telegram configurado."
        )

    elif resposta:

        logger.error(
            f"Erro webhook: "
            f"{resposta.text}"
        )


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup_event():

    global shutting_down

    shutting_down = False

    logger.info(
        "🚀 Iniciando BOT "
        "DIRECT_STREAM_V5..."
    )

    await conectar_redis()

    await configurar_webhook()

    await iniciar_monitores_salvos()

    logger.info(
        "✅ Sistema V5 iniciado."
    )


# ============================================================
# SHUTDOWN
# ============================================================

@app.on_event("shutdown")
async def shutdown_event():

    global shutting_down

    shutting_down = True

    logger.info(
        "🛑 Encerrando aplicação..."
    )

    # Cancela monitores
    for usuario, dados in list(
        tarefas_monitoramento.items()
    ):

        task = dados.get(
            "task"
        )

        if task:

            task.cancel()

    tarefas_monitoramento.clear()

    # Para FFmpeg
    for usuario, controle in list(
        gravacoes_ativas.items()
    ):

        controle["parar"] = True

        processo = controle.get(
            "processo"
        )

        if processo:

            try:

                processo.terminate()

                logger.info(
                    f"[{usuario}] "
                    f"FFmpeg solicitado a parar."
                )

            except Exception:
                pass

    # Pequena janela para o FFmpeg
    # fechar corretamente.
    await asyncio.sleep(2)

    if redis_client:

        try:

            await salvar_monitorados()

            await redis_client.close()

        except Exception:
            pass

    logger.info(
        "Aplicação encerrada."
    )


# ============================================================
# EXECUÇÃO
# ============================================================

if __name__ == "__main__":

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT
    )
