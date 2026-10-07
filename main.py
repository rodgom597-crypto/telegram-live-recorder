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

redis_client = None

shutting_down = False

http_session = requests.Session()

tarefas_monitoramento = {}

# Guarda as gravações que estão acontecendo
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
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{endpoint}"


async def telegram_request(endpoint, payload=None, files=None):
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
            logger.error(f"Erro Telegram {endpoint}: {e}")
            return None

    return await asyncio.to_thread(request_sync)


async def enviar_mensagem(chat_id, texto):
    if not chat_id:
        return

    resposta = await telegram_request(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": texto
        }
    )

    if resposta is None:
        logger.error("Não foi possível enviar mensagem para o Telegram.")


async def enviar_video(chat_id, arquivo, legenda=None):
    if not chat_id:
        return False

    if not arquivo.exists():
        logger.error(f"Arquivo não encontrado para envio: {arquivo}")
        return False

    def upload_sync():
        try:
            with open(arquivo, "rb") as video:
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
            logger.error(f"Erro ao enviar vídeo: {e}")
            return None

    resposta = await asyncio.to_thread(upload_sync)

    if resposta is None:
        return False

    if resposta.ok:
        logger.info(f"Vídeo enviado para o chat {chat_id}.")
        return True

    logger.error(
        f"Erro ao enviar vídeo. "
        f"HTTP {resposta.status_code}: {resposta.text[:500]}"
    )

    return False


# ============================================================
# REDIS
# ============================================================

async def conectar_redis():
    global redis_client

    if not REDIS_URL:
        logger.warning("REDIS_URL não configurada.")
        return False

    try:
        redis_client = redis.from_url(
            REDIS_URL,
            decode_responses=True
        )

        await redis_client.ping()

        logger.info("Redis conectado com sucesso.")

        return True

    except Exception as e:
        logger.error(f"Erro ao conectar no Redis: {e}")
        redis_client = None
        return False


async def salvar_monitorados():
    if not redis_client:
        return

    try:
        await redis_client.delete("tiktok_monitorados")

        if not tarefas_monitoramento:
            return

        for usuario, dados in tarefas_monitoramento.items():

            chat_id = dados.get("chat_id")

            await redis_client.hset(
                "tiktok_monitorados",
                usuario,
                str(chat_id)
            )

        logger.info(
            f"Monitorados salvos no Redis: "
            f"{list(tarefas_monitoramento.keys())}"
        )

    except Exception as e:
        logger.error(f"Erro ao salvar monitorados: {e}")


async def carregar_monitorados():
    if not redis_client:
        return {}

    try:
        dados = await redis_client.hgetall(
            "tiktok_monitorados"
        )

        logger.info(
            f"Monitorados carregados do Redis: "
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
    url = f"https://www.tiktok.com/@{usuario}/live"

    try:
        resposta = http_session.get(
            url,
            headers=headers_tiktok(),
            timeout=TIKTOK_TIMEOUT
        )

        if resposta.status_code != 200:
            logger.info(
                f"[{usuario}] TikTok retornou "
                f"HTTP {resposta.status_code}"
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

        logger.info(
            f"[{usuario}] Não foi possível encontrar "
            f"Room ID."
        )

        return None

    except Exception as e:
        logger.error(
            f"[{usuario}] Erro ao obter Room ID: {e}"
        )

        return None


def consultar_room_info(room_id, usuario):
    url = (
        "https://webcast.tiktok.com/webcast/room/info"
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

        for chave, valor in obj.items():

            if isinstance(valor, str):

                valor_lower = valor.lower()

                if (
                    ".flv" in valor_lower
                    or ".m3u8" in valor_lower
                    or "pull-" in valor_lower
                    or "pull." in valor_lower
                ):
                    urls.append(valor)

            elif isinstance(valor, (dict, list)):
                urls.extend(
                    encontrar_urls_stream(valor)
                )

    elif isinstance(obj, list):

        for item in obj:
            urls.extend(
                encontrar_urls_stream(item)
            )

    return urls


def escolher_stream(urls):
    if not urls:
        return None

    # Primeiro tenta FLV
    for url in urls:
        if ".flv" in url.lower():
            return url

    # Depois HLS
    for url in urls:
        if ".m3u8" in url.lower():
            return url

    # Qualquer pull válido
    for url in urls:
        if (
            "pull-" in url.lower()
            or "pull." in url.lower()
        ):
            return url

    return None


def descobrir_stream(usuario):
    room_id = obter_room_id(usuario)

    if not room_id:
        return None

    dados = consultar_room_info(
        room_id,
        usuario
    )

    if not dados:
        return None

    urls = encontrar_urls_stream(dados)

    # Remove duplicadas preservando ordem
    urls = list(dict.fromkeys(urls))

    logger.info(
        f"[{usuario}] Encontradas "
        f"{len(urls)} URLs de stream."
    )

    stream = escolher_stream(urls)

    if stream:
        logger.info(
            f"[{usuario}] Stream escolhida: "
            f"{stream[:180]}"
        )

    return stream


# ============================================================
# FFmpeg
# ============================================================

async def gravar_stream(
    usuario,
    stream_url,
    arquivo_flv,
    controle
):
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
        str(arquivo_flv)
    ]

    logger.info(
        f"[{usuario}] Iniciando FFmpeg."
    )

    try:

        processo = await asyncio.create_subprocess_exec(
            *comando,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE
        )

        controle["processo"] = processo

        while True:

            if controle.get("parar"):
                logger.info(
                    f"[{usuario}] Solicitação de parada recebida."
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

                break

            if processo.returncode is not None:
                break

            try:
                linha = await asyncio.wait_for(
                    processo.stderr.readline(),
                    timeout=1.0
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
            f"[{usuario}] FFmpeg finalizado. "
            f"Código: {codigo}"
        )

        return codigo

    except Exception as e:

        logger.error(
            f"[{usuario}] Erro no FFmpeg: {e}"
        )

        return -1


# ============================================================
# CONVERSÃO RÁPIDA FLV -> MP4
# ============================================================

async def converter_mp4_rapido(
    arquivo_flv,
    arquivo_mp4
):
    """
    Primeira tentativa:

    FLV -> MP4 usando -c copy.

    Não reencoda o vídeo.
    É MUITO mais rápido.
    """

    comando = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",

        "-fflags",
        "+genpts",

        "-i",
        str(arquivo_flv),

        "-map",
        "0:v?",
        "-map",
        "0:a?",

        "-c",
        "copy",

        "-movflags",
        "+faststart",

        "-y",
        str(arquivo_mp4)
    ]

    logger.info(
        "Tentando conversão rápida "
        "FLV -> MP4 sem reencodar."
    )

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

                if tamanho > 0:

                    logger.info(
                        "Conversão rápida concluída "
                        f"com sucesso. "
                        f"Tamanho: {tamanho / 1024 / 1024:.2f} MB"
                    )

                    return True

        erro = stderr.decode(
            errors="ignore"
        )

        logger.warning(
            "Conversão rápida falhou:\n"
            f"{erro[-3000:]}"
        )

        if arquivo_mp4.exists():
            try:
                arquivo_mp4.unlink()
            except Exception:
                pass

        return False

    except Exception as e:

        logger.warning(
            f"Erro na conversão rápida: {e}"
        )

        return False


# ============================================================
# CONVERSÃO DE SEGURANÇA
# ============================================================

async def converter_mp4_reencode(
    arquivo_flv,
    arquivo_mp4
):
    """
    Fallback.

    Só será usado se a conversão rápida
    não funcionar.

    Aqui o vídeo é reencodado para garantir
    compatibilidade e tentar corrigir problemas
    de frames.
    """

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

    logger.info(
        "Usando conversão de segurança "
        "com reencode."
    )

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

                if tamanho > 0:

                    logger.info(
                        "Conversão de segurança "
                        "concluída com sucesso."
                    )

                    return True

        erro = stderr.decode(
            errors="ignore"
        )

        logger.error(
            "Falha no reencode:\n"
            f"{erro[-5000:]}"
        )

        return False

    except Exception as e:

        logger.error(
            f"Erro no reencode: {e}"
        )

        return False


async def converter_para_mp4(
    arquivo_flv,
    arquivo_mp4
):
    """
    Sistema inteligente:

    1. Tenta copy = MUITO rápido
    2. Se falhar = reencode
    """

    sucesso = await converter_mp4_rapido(
        arquivo_flv,
        arquivo_mp4
    )

    if sucesso:
        return True

    logger.warning(
        "Conversão rápida não funcionou. "
        "Iniciando fallback."
    )

    return await converter_mp4_reencode(
        arquivo_flv,
        arquivo_mp4
    )


# ============================================================
# FINALIZAÇÃO
# ============================================================

async def finalizar_gravacao(
    usuario,
    chat_id,
    arquivo_flv
):
    if not arquivo_flv.exists():

        logger.warning(
            f"[{usuario}] Arquivo FLV não encontrado."
        )

        await enviar_mensagem(
            chat_id,
            f"⚠️ A gravação de @{usuario} terminou, "
            f"mas o arquivo não foi encontrado."
        )

        return

    tamanho_flv = arquivo_flv.stat().st_size

    logger.info(
        f"[{usuario}] FLV finalizado: "
        f"{tamanho_flv / 1024 / 1024:.2f} MB"
    )

    await enviar_mensagem(
        chat_id,
        f"⏳ Finalizei a gravação de @{usuario}.\n"
        f"Convertendo para MP4..."
    )

    arquivo_mp4 = arquivo_flv.with_suffix(".mp4")

    inicio = asyncio.get_running_loop().time()

    sucesso = await converter_para_mp4(
        arquivo_flv,
        arquivo_mp4
    )

    tempo = (
        asyncio.get_running_loop().time()
        - inicio
    )

    if not sucesso:

        await enviar_mensagem(
            chat_id,
            f"❌ Não consegui converter "
            f"a gravação de @{usuario} para MP4."
        )

        try:
            arquivo_flv.unlink()
        except Exception:
            pass

        return

    tamanho_mp4 = arquivo_mp4.stat().st_size

    logger.info(
        f"[{usuario}] MP4 pronto em "
        f"{tempo:.1f} segundos. "
        f"Tamanho: {tamanho_mp4 / 1024 / 1024:.2f} MB"
    )

    await enviar_mensagem(
        chat_id,
        f"✅ MP4 pronto!\n"
        f"⏱️ Conversão: {tempo:.1f}s\n"
        f"📦 Tamanho: "
        f"{tamanho_mp4 / 1024 / 1024:.2f} MB\n"
        f"📤 Enviando para o Telegram..."
    )

    enviado = await enviar_video(
        chat_id,
        arquivo_mp4,
        f"🎥 Gravação @{usuario}"
    )

    if enviado:

        await enviar_mensagem(
            chat_id,
            f"✅ Gravação de @{usuario} enviada!"
        )

    else:

        await enviar_mensagem(
            chat_id,
            f"⚠️ O MP4 foi criado, "
            f"mas não consegui enviá-lo ao Telegram."
        )

    # Limpeza
    try:
        if arquivo_flv.exists():
            arquivo_flv.unlink()
    except Exception:
        pass

    try:
        if arquivo_mp4.exists():
            arquivo_mp4.unlink()
    except Exception:
        pass


# ============================================================
# GRAVAÇÃO COMPLETA
# ============================================================

async def gravar_live(
    usuario,
    chat_id
):
    usuario = usuario.strip().lstrip("@").lower()

    if usuario in gravacoes_ativas:

        await enviar_mensagem(
            chat_id,
            f"⚠️ @{usuario} já está sendo gravado."
        )

        return

    timestamp = int(
        asyncio.get_running_loop().time() * 1000
    )

    arquivo_flv = (
        BASE_DIR
        / f"{usuario}_{timestamp}.flv"
    )

    controle = {
        "parar": False,
        "processo": None,
        "chat_id": chat_id
    }

    gravacoes_ativas[usuario] = controle

    try:

        await enviar_mensagem(
            chat_id,
            f"🔎 Procurando a LIVE de @{usuario}..."
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

            logger.info(
                f"[{usuario}] Stream não encontrada. "
                f"Tentativa {tentativa}/"
                f"{TENTATIVAS_MANUAL}"
            )

            if tentativa < TENTATIVAS_MANUAL:

                await asyncio.sleep(5)

        if not stream_url:

            await enviar_mensagem(
                chat_id,
                f"❌ Não consegui encontrar "
                f"a transmissão de @{usuario}."
            )

            return

        await enviar_mensagem(
            chat_id,
            f"🔴 LIVE encontrada!\n"
            f"🎥 Iniciando gravação de @{usuario}..."
        )

        logger.info(
            f"[{usuario}] Iniciando gravação."
        )

        codigo = await gravar_stream(
            usuario,
            stream_url,
            arquivo_flv,
            controle
        )

        logger.info(
            f"[{usuario}] Gravação encerrada."
        )

        if arquivo_flv.exists():

            tamanho = arquivo_flv.stat().st_size

            logger.info(
                f"[{usuario}] Arquivo gravado: "
                f"{tamanho / 1024 / 1024:.2f} MB"
            )

            if tamanho > 0:

                await finalizar_gravacao(
                    usuario,
                    chat_id,
                    arquivo_flv
                )

            else:

                await enviar_mensagem(
                    chat_id,
                    f"⚠️ A gravação de @{usuario} "
                    f"gerou um arquivo vazio."
                )

        else:

            await enviar_mensagem(
                chat_id,
                f"⚠️ Nenhum arquivo foi gerado "
                f"para @{usuario}."
            )

    except Exception as e:

        logger.exception(
            f"[{usuario}] Erro na gravação."
        )

        await enviar_mensagem(
            chat_id,
            f"❌ Erro na gravação de @{usuario}:\n"
            f"{str(e)[:500]}"
        )

    finally:

        gravacoes_ativas.pop(
            usuario,
            None
        )


# ============================================================
# PARAR GRAVAÇÃO
# ============================================================

async def parar_gravacao(
    usuario,
    chat_id
):
    usuario = usuario.strip().lstrip("@").lower()

    controle = gravacoes_ativas.get(
        usuario
    )

    if not controle:

        await enviar_mensagem(
            chat_id,
            f"⚠️ Não existe gravação ativa de "
            f"@{usuario}."
        )

        return

    logger.info(
        f"[{usuario}] Solicitação de parada."
    )

    controle["parar"] = True

    processo = controle.get(
        "processo"
    )

    if processo:

        try:
            processo.terminate()

            await enviar_mensagem(
                chat_id,
                f"⏹️ Parando gravação de @{usuario}..."
            )

        except Exception as e:

            logger.warning(
                f"[{usuario}] Erro ao parar FFmpeg: {e}"
            )

    else:

        await enviar_mensagem(
            chat_id,
            f"⏹️ Cancelando gravação de @{usuario}..."
        )


# ============================================================
# MONITORAMENTO
# ============================================================

async def monitorar_usuario(
    usuario,
    chat_id
):
    usuario = usuario.strip().lstrip("@").lower()

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
                f"[{usuario}] Erro monitoramento: {e}"
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
# COMANDOS
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
            "🤖 Bot de gravação TikTok LIVE\n\n"
            "Comandos disponíveis:\n\n"
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

        usuario = partes[1].lstrip("@")

        if usuario.lower() in gravacoes_ativas:

            await enviar_mensagem(
                chat_id,
                f"⚠️ @{usuario} já está sendo gravado."
            )

            return

        await enviar_mensagem(
            chat_id,
            f"📥 Solicitação de gravação enviada "
            f"para @{usuario}."
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

        usuario = partes[1].lstrip("@")

        await parar_gravacao(
            usuario,
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

        usuario = partes[1].lstrip("@").lower()

        if usuario in tarefas_monitoramento:

            await enviar_mensagem(
                chat_id,
                f"⚠️ @{usuario} já está sendo monitorado."
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
            f"👁️ @{usuario} agora está sendo monitorado."
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

        usuario = partes[1].lstrip("@").lower()

        dados = tarefas_monitoramento.pop(
            usuario,
            None
        )

        if not dados:

            await enviar_mensagem(
                chat_id,
                f"⚠️ @{usuario} não está sendo monitorado."
            )

            return

        task = dados.get("task")

        if task:

            task.cancel()

        await salvar_monitorados()

        await enviar_mensagem(
            chat_id,
            f"🛑 @{usuario} foi removido do monitoramento."
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
                    f"🟢 @{usuario} — monitorando"
                )

        await enviar_mensagem(
            chat_id,
            "\n".join(linhas)
        )

        return

    # --------------------------------------------------------
    # DESCONHECIDO
    # --------------------------------------------------------

    await enviar_mensagem(
        chat_id,
        "❓ Comando não reconhecido.\n\n"
        "Use /start para ver os comandos."
    )


# ============================================================
# WEBHOOK TELEGRAM
# ============================================================

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):

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
            f"Erro webhook Telegram: {e}"
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
        "service": "TikTok LIVE Recorder",
        "recordings": list(
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
        "redis": redis_ok,
        "gravando": list(
            gravacoes_ativas.keys()
        ),
        "monitorados": list(
            tarefas_monitoramento.keys()
        )
    }


# ============================================================
# WEBHOOK CONFIG
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

    url = WEBHOOK_URL.rstrip(
        "/"
    ) + "/telegram/webhook"

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
            f"Erro ao configurar webhook: "
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
        "🚀 Iniciando BOT DIRECT_STREAM_V4..."
    )

    await conectar_redis()

    await configurar_webhook()

    await iniciar_monitores_salvos()

    logger.info(
        "✅ Sistema iniciado."
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

    # Para monitoramentos
    for usuario, dados in list(
        tarefas_monitoramento.items()
    ):

        task = dados.get("task")

        if task:
            task.cancel()

    tarefas_monitoramento.clear()

    # Para gravações ativas
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

            except Exception:
                pass

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
