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
# CONFIGURAÇÃO
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
# REDIS
# ============================================================

async def conectar_redis():
    global redis_client

    if not REDIS_URL:
        logger.warning("⚠️ REDIS_URL não configurada.")
        return False

    try:
        redis_client = redis.from_url(
            REDIS_URL,
            decode_responses=True
        )

        await redis_client.ping()

        logger.info("✅ Redis conectado.")
        return True

    except Exception as e:
        logger.error(f"❌ Erro ao conectar Redis: {e}")
        redis_client = None
        return False


async def salvar_monitorados():
    if not redis_client:
        return

    try:
        contas = list(tarefas_monitoramento.keys())

        await redis_client.delete("monitorados")

        if contas:
            await redis_client.sadd(
                "monitorados",
                *contas
            )

        logger.info(
            f"💾 Monitorados salvos no Redis: {contas}"
        )

    except Exception as e:
        logger.error(
            f"❌ Erro ao salvar monitorados: {e}"
        )


async def carregar_monitorados():
    if not redis_client:
        return []

    try:
        contas = await redis_client.smembers(
            "monitorados"
        )

        return list(contas)

    except Exception as e:
        logger.error(
            f"❌ Erro ao carregar monitorados: {e}"
        )
        return []


# ============================================================
# TELEGRAM
# ============================================================

async def telegram_request(method, data=None, files=None):
    url = (
        f"https://api.telegram.org/bot"
        f"{BOT_TOKEN}/{method}"
    )

    try:
        response = await asyncio.to_thread(
            requests.post,
            url,
            data=data,
            files=files,
            timeout=60
        )

        if not response.ok:
            logger.error(
                f"❌ Telegram HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
            return None

        return response.json()

    except Exception as e:
        logger.error(
            f"❌ Erro Telegram {method}: {e}"
        )
        return None


async def enviar_mensagem(chat_id, texto):
    return await telegram_request(
        "sendMessage",
        data={
            "chat_id": chat_id,
            "text": texto
        }
    )


async def enviar_video(chat_id, arquivo, legenda=None):
    if not arquivo.exists():
        logger.error(
            f"❌ Arquivo não encontrado: {arquivo}"
        )
        return False

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

            resultado = await telegram_request(
                "sendVideo",
                data=data,
                files=files
            )

        if resultado and resultado.get("ok"):
            logger.info(
                f"✅ Vídeo enviado para Telegram: "
                f"{arquivo.name}"
            )
            return True

        logger.error(
            f"❌ Telegram não enviou o vídeo."
        )

        return False

    except Exception as e:
        logger.error(
            f"❌ Erro enviando vídeo: {e}"
        )
        return False


# ============================================================
# TIKTOK - HEADERS
# ============================================================

def headers_tiktok():

    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,image/avif,"
            "image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.tiktok.com/",
        "Connection": "keep-alive",
    }


# ============================================================
# DESCOBRIR ROOM ID
# ============================================================

async def obter_room_id(usuario):

    usuario = usuario.strip().lstrip("@")

    url = f"https://www.tiktok.com/@{usuario}/live"

    logger.info(
        f"[{usuario}] Consultando página LIVE..."
    )

    try:

        resposta = await asyncio.to_thread(
            http_session.get,
            url,
            headers=headers_tiktok(),
            timeout=TIKTOK_TIMEOUT,
            allow_redirects=True
        )

        logger.info(
            f"[{usuario}] TikTok HTTP "
            f"{resposta.status_code}"
        )

        if resposta.status_code != 200:
            logger.warning(
                f"[{usuario}] Página LIVE retornou "
                f"HTTP {resposta.status_code}"
            )
            return None

        html = resposta.text

        padroes = [

            r'"roomId"\s*:\s*"(\d+)"',

            r'"room_id"\s*:\s*"(\d+)"',

            r'"roomId"\s*:\s*(\d+)',

            r'"room_id"\s*:\s*(\d+)',

            r'roomId\\":\\"(\d+)',

            r'room_id\\":\\"(\d+)',

            r'roomId%22%3A%22(\d+)',

            r'room_id%22%3A%22(\d+)',
        ]

        for padrao in padroes:

            encontrado = re.search(
                padrao,
                html,
                re.IGNORECASE
            )

            if encontrado:

                room_id = encontrado.group(1)

                logger.info(
                    f"[{usuario}] Room ID encontrado: "
                    f"{room_id}"
                )

                return room_id

        logger.warning(
            f"[{usuario}] Room ID não encontrado."
        )

        return None

    except Exception as e:

        logger.error(
            f"[{usuario}] Erro obtendo Room ID: {e}"
        )

        return None


# ============================================================
# TIKTOK - ROOM INFO
# ============================================================

async def consultar_room_info(room_id, usuario):

    url = (
        "https://webcast.tiktok.com/"
        "webcast/room/info"
    )

    parametros = {
        "aid": "1988",
        "room_id": room_id,
    }

    headers = headers_tiktok()

    headers.update({
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.tiktok.com",
        "Referer": (
            f"https://www.tiktok.com/@"
            f"{usuario}/live"
        ),
    })

    logger.info(
        f"[{usuario}] Consultando API room/info..."
    )

    try:

        resposta = await asyncio.to_thread(
            http_session.get,
            url,
            params=parametros,
            headers=headers,
            timeout=TIKTOK_TIMEOUT
        )

        logger.info(
            f"[{usuario}] API room/info HTTP "
            f"{resposta.status_code}"
        )

        if resposta.status_code != 200:

            logger.warning(
                f"[{usuario}] room/info retornou "
                f"HTTP {resposta.status_code}: "
                f"{resposta.text[:300]}"
            )

            return None

        try:
            dados = resposta.json()
        except Exception:
            logger.error(
                f"[{usuario}] Resposta não é JSON."
            )
            return None

        return dados

    except Exception as e:

        logger.error(
            f"[{usuario}] Erro room/info: {e}"
        )

        return None


# ============================================================
# PROCURAR URLS DE STREAM
# ============================================================

def encontrar_urls_stream(obj):

    encontrados = []

    if isinstance(obj, dict):

        for valor in obj.values():

            encontrados.extend(
                encontrar_urls_stream(valor)
            )

    elif isinstance(obj, list):

        for item in obj:

            encontrados.extend(
                encontrar_urls_stream(item)
            )

    elif isinstance(obj, str):

        texto = obj.strip()

        if texto.startswith("http"):

            texto_lower = texto.lower()

            if (
                ".flv" in texto_lower
                or ".m3u8" in texto_lower
                or "pull-" in texto_lower
                or "pull." in texto_lower
            ):

                encontrados.append(texto)

    return encontrados


def escolher_stream(urls):

    if not urls:
        return None

    flv = []

    hls = []

    outros = []

    for url in urls:

        lower = url.lower()

        if ".flv" in lower:
            flv.append(url)

        elif ".m3u8" in lower:
            hls.append(url)

        else:
            outros.append(url)

    if flv:
        return flv[0]

    if hls:
        return hls[0]

    if outros:
        return outros[0]

    return None


# ============================================================
# DESCOBRIR STREAM
# ============================================================

async def descobrir_stream(usuario):

    usuario = usuario.strip().lstrip("@")

    room_id = await obter_room_id(usuario)

    if not room_id:

        logger.info(
            f"[{usuario}] Não foi possível obter Room ID."
        )

        return None

    dados = await consultar_room_info(
        room_id,
        usuario
    )

    if not dados:

        logger.info(
            f"[{usuario}] Não foi possível obter "
            f"informações da sala."
        )

        return None

    urls = encontrar_urls_stream(dados)

    logger.info(
        f"[{usuario}] URLs de stream encontradas: "
        f"{len(urls)}"
    )

    stream = escolher_stream(urls)

    if not stream:

        logger.warning(
            f"[{usuario}] Nenhuma URL de stream encontrada."
        )

        return None

    tipo = "FLV" if ".flv" in stream.lower() else "HLS"

    logger.info(
        f"[{usuario}] Stream encontrada ({tipo})."
    )

    logger.info(
        f"[{usuario}] URL: {stream[:250]}"
    )

    return stream


# ============================================================
# FFmpeg - GRAVAÇÃO
# ============================================================

async def gravar_stream(usuario, stream_url, arquivo_flv):

    logger.info(
        f"[{usuario}] Iniciando FFmpeg..."
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

        str(arquivo_flv)
    ]

    logger.info(
        f"[{usuario}] Comando FFmpeg iniciado."
    )

    try:

        processo = await asyncio.create_subprocess_exec(
            *comando,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        while True:

            linha = await processo.stderr.readline()

            if not linha:

                break

            texto = linha.decode(
                errors="ignore"
            ).strip()

            if texto:

                logger.info(
                    f"[{usuario}] FFmpeg: {texto}"
                )

        codigo = await processo.wait()

        logger.info(
            f"[{usuario}] FFmpeg terminou "
            f"com código {codigo}."
        )

        if arquivo_flv.exists():

            tamanho = arquivo_flv.stat().st_size

            logger.info(
                f"[{usuario}] Arquivo FLV: "
                f"{tamanho / 1024 / 1024:.2f} MB"
            )

        return codigo == 0

    except asyncio.CancelledError:

        logger.info(
            f"[{usuario}] Gravação cancelada."
        )

        raise

    except Exception as e:

        logger.error(
            f"[{usuario}] Erro FFmpeg: {e}"
        )

        return False


# ============================================================
# CONVERTER FLV -> MP4
# ============================================================

async def converter_mp4(usuario, arquivo_flv, arquivo_mp4):

    logger.info(
        f"[{usuario}] Convertendo FLV para MP4..."
    )

    if not arquivo_flv.exists():

        logger.error(
            f"[{usuario}] FLV não encontrado."
        )

        return False

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

        if processo.returncode != 0:

            erro = stderr.decode(
                errors="ignore"
            )

            logger.error(
                f"[{usuario}] Erro na conversão:\n"
                f"{erro[-3000:]}"
            )

            return False

        if not arquivo_mp4.exists():

            logger.error(
                f"[{usuario}] MP4 não foi criado."
            )

            return False

        tamanho = arquivo_mp4.stat().st_size

        logger.info(
            f"[{usuario}] MP4 criado: "
            f"{tamanho / 1024 / 1024:.2f} MB"
        )

        return True

    except Exception as e:

        logger.error(
            f"[{usuario}] Erro convertendo MP4: {e}"
        )

        return False


# ============================================================
# GRAVAR LIVE COMPLETA
# ============================================================

async def gravar_live(usuario, chat_id):

    usuario = usuario.strip().lstrip("@")

    if usuario in tarefas_gravacao:

        logger.info(
            f"[{usuario}] Já existe uma gravação."
        )

        return

    tarefas_gravacao[usuario] = True

    arquivo_flv = BASE_DIR / (
        f"{usuario}_{int(asyncio.get_running_loop().time())}.flv"
    )

    arquivo_mp4 = arquivo_flv.with_suffix(".mp4")

    try:

        await enviar_mensagem(
            chat_id,
            f"🎥 Iniciando gravação de @{usuario}."
        )

        stream_url = None

        for tentativa in range(
            1,
            TENTATIVAS_MANUAL + 1
        ):

            logger.info(
                f"[{usuario}] Tentativa "
                f"{tentativa}/{TENTATIVAS_MANUAL}"
            )

            stream_url = await descobrir_stream(
                usuario
            )

            if stream_url:
                break

            if tentativa < TENTATIVAS_MANUAL:

                await asyncio.sleep(5)

        if not stream_url:

            await enviar_mensagem(
                chat_id,
                f"❌ Não consegui encontrar a stream "
                f"de @{usuario}."
            )

            return

        await enviar_mensagem(
            chat_id,
            f"🔴 Stream encontrada de @{usuario}. "
            f"Iniciando gravação..."
        )

        sucesso = await gravar_stream(
            usuario,
            stream_url,
            arquivo_flv
        )

        if not arquivo_flv.exists():

            await enviar_mensagem(
                chat_id,
                f"❌ A gravação de @{usuario} "
                f"não gerou arquivo."
            )

            return

        tamanho = arquivo_flv.stat().st_size

        if tamanho < 100_000:

            logger.warning(
                f"[{usuario}] Arquivo muito pequeno: "
                f"{tamanho} bytes"
            )

            await enviar_mensagem(
                chat_id,
                f"⚠️ A gravação de @{usuario} "
                f"ficou muito pequena."
            )

            return

        await enviar_mensagem(
            chat_id,
            f"⏹️ Live de @{usuario} encerrada.\n"
            f"Convertendo para MP4..."
        )

        convertido = await converter_mp4(
            usuario,
            arquivo_flv,
            arquivo_mp4
        )

        if not convertido:

            await enviar_mensagem(
                chat_id,
                f"❌ Não consegui converter "
                f"a gravação de @{usuario}."
            )

            return

        await enviar_video(
            chat_id,
            arquivo_mp4,
            legenda=(
                f"🎥 Gravação de @{usuario}\n"
                f"Formato: MP4"
            )
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
                f"❌ Erro na gravação de "
                f"@{usuario}:\n{e}"
            )

        except Exception:
            pass

    finally:

        tarefas_gravacao.pop(
            usuario,
            None
        )

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

        logger.info(
            f"[{usuario}] Limpeza concluída."
        )


# ============================================================
# MONITORAMENTO
# ============================================================

async def monitorar_conta(usuario, chat_id):

    usuario = usuario.strip().lstrip("@")

    logger.info(
        f"[{usuario}] Monitoramento iniciado."
    )

    ao_vivo_anteriormente = False

    while not shutting_down:

        try:

            if usuario in tarefas_gravacao:

                await asyncio.sleep(
                    INTERVALO_MONITORAMENTO
                )

                continue

            stream_url = await descobrir_stream(
                usuario
            )

            esta_ao_vivo = stream_url is not None

            if esta_ao_vivo and not ao_vivo_anteriormente:

                logger.info(
                    f"[{usuario}] 🔴 LIVE DETECTADA!"
                )

                ao_vivo_anteriormente = True

                tarefa = asyncio.create_task(
                    gravar_live(
                        usuario,
                        chat_id
                    )
                )

                tarefas_gravacao[usuario] = tarefa

            elif not esta_ao_vivo:

                if ao_vivo_anteriormente:

                    logger.info(
                        f"[{usuario}] LIVE aparentemente encerrada."
                    )

                ao_vivo_anteriormente = False

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
        f"[{usuario}] Monitoramento finalizado."
    )


# ============================================================
# RESTAURAR MONITORES
# ============================================================

async def iniciar_monitores_salvos():

    contas = await carregar_monitorados()

    if not contas:

        logger.info(
            "📋 Nenhuma conta monitorada salva."
        )

        return

    logger.info(
        f"📋 Contas salvas encontradas: {contas}"
    )

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
            "⚠️ Monitorados existem, mas "
            "monitor_chat_id não foi encontrado."
        )

        return

    for usuario in contas:

        if usuario in tarefas_monitoramento:
            continue

        tarefa = asyncio.create_task(
            monitorar_conta(
                usuario,
                int(chat_id)
            )
        )

        tarefas_monitoramento[usuario] = tarefa

        logger.info(
            f"♻️ Monitor restaurado: @{usuario}"
        )


# ============================================================
# COMANDOS
# ============================================================

async def processar_comando(chat_id, texto):

    texto = texto.strip()

    if not texto:
        return

    partes = texto.split()

    comando = partes[0].lower()

    if "@" in comando:
        comando = comando.split("@")[0]

    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

    if comando == "/start":

        await enviar_mensagem(
            chat_id,
            "🤖 Bot de gravação TikTok LIVE ativo.\n\n"
            "/gravar usuario\n"
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
                "Use:\n/gravar usuario"
            )

            return

        usuario = partes[1].lstrip("@")

        if usuario in tarefas_gravacao:

            await enviar_mensagem(
                chat_id,
                f"⚠️ @{usuario} já está sendo gravado."
            )

            return

        await enviar_mensagem(
            chat_id,
            f"📨 Solicitação de gravação enviada "
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
    # MONITORAR
    # --------------------------------------------------------

    if comando == "/monitorar":

        if len(partes) < 2:

            await enviar_mensagem(
                chat_id,
                "Use:\n/monitorar usuario"
            )

            return

        usuario = partes[1].lstrip("@")

        if usuario in tarefas_monitoramento:

            await enviar_mensagem(
                chat_id,
                f"⚠️ @{usuario} já está sendo monitorado."
            )

            return

        tarefa = asyncio.create_task(
            monitorar_conta(
                usuario,
                chat_id
            )
        )

        tarefas_monitoramento[usuario] = tarefa

        if redis_client:

            try:

                await redis_client.sadd(
                    "monitorados",
                    usuario
                )

                await redis_client.set(
                    "monitor_chat_id",
                    str(chat_id)
                )

            except Exception as e:

                logger.error(
                    f"Erro salvando monitor: {e}"
                )

        await enviar_mensagem(
            chat_id,
            f"✅ @{usuario} agora está sendo monitorado.\n\n"
            f"Quando entrar ao vivo, a gravação será iniciada "
            f"automaticamente."
        )

        return

    # --------------------------------------------------------
    # DESMONITORAR
    # --------------------------------------------------------

    if comando == "/desmonitorar":

        if len(partes) < 2:

            await enviar_mensagem(
                chat_id,
                "Use:\n/desmonitorar usuario"
            )

            return

        usuario = partes[1].lstrip("@")

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

        if redis_client:

            try:

                await redis_client.srem(
                    "monitorados",
                    usuario
                )

            except Exception as e:

                logger.error(
                    f"Erro removendo monitor: {e}"
                )

        await enviar_mensagem(
            chat_id,
            f"🛑 @{usuario} removido do monitoramento."
        )

        return

    # --------------------------------------------------------
    # MONITORADOS
    # --------------------------------------------------------

    if comando == "/monitorados":

        contas = list(
            tarefas_monitoramento.keys()
        )

        if not contas:

            await enviar_mensagem(
                chat_id,
                "📋 Nenhuma conta está sendo monitorada."
            )

            return

        lista = "\n".join(
            f"• @{conta}"
            for conta in sorted(contas)
        )

        await enviar_mensagem(
            chat_id,
            f"📋 Contas monitoradas:\n\n{lista}"
        )

        return

    # --------------------------------------------------------
    # COMANDO DESCONHECIDO
    # --------------------------------------------------------

    await enviar_mensagem(
        chat_id,
        "❓ Comando não reconhecido.\n\n"
        "Use /start para ver os comandos."
    )


# ============================================================
# WEBHOOK TELEGRAM
# ============================================================

async def receber_webhook(request: Request):

    try:

        update = await request.json()

        logger.info(
            f"📩 Update Telegram recebido."
        )

        mensagem = update.get("message")

        if not mensagem:
            return JSONResponse(
                {"ok": True}
            )

        chat = mensagem.get("chat")

        if not chat:
            return JSONResponse(
                {"ok": True}
            )

        chat_id = chat.get("id")

        texto = mensagem.get("text", "")

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

        logger.error(
            f"❌ Erro webhook: {e}"
        )

        return JSONResponse(
            {
                "ok": False,
                "error": str(e)
            },
            status_code=500
        )


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):

    return await receber_webhook(request)


@app.post("/webhook")
async def webhook_alias(request: Request):

    return await receber_webhook(request)


# ============================================================
# CONFIGURAR WEBHOOK
# ============================================================

async def configurar_webhook():

    if not BOT_TOKEN:
        logger.error(
            "❌ BOT_TOKEN não configurado."
        )
        return False

    if not WEBHOOK_URL:

        logger.warning(
            "⚠️ WEBHOOK_URL não configurada."
        )

        return False

    url = (
        WEBHOOK_URL.rstrip("/")
        + "/telegram/webhook"
    )

    resultado = await telegram_request(
        "setWebhook",
        data={
            "url": url,
            "drop_pending_updates": "true"
        }
    )

    if resultado and resultado.get("ok"):

        logger.info(
            f"✅ Webhook configurado: {url}"
        )

        return True

    logger.error(
        "❌ Não foi possível configurar webhook."
    )

    return False


# ============================================================
# ROTAS DE STATUS
# ============================================================

@app.get("/")
async def raiz():

    return {
        "status": "online",
        "version": "DIRECT_STREAM_V2",
        "service": "telegram-tiktok-live-recorder"
    }


@app.get("/health")
async def health():

    return {
        "status": "ok",
        "version": "DIRECT_STREAM_V2",
        "redis": redis_client is not None,
        "monitorados": list(
            tarefas_monitoramento.keys()
        ),
        "gravando": list(
            tarefas_gravacao.keys()
        )
    }


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup_event():

    global shutting_down

    shutting_down = False

    logger.info(
        "🚀 Iniciando BOT DIRECT_STREAM_V2..."
    )

    await conectar_redis()

    await configurar_webhook()

    await iniciar_monitores_salvos()

    logger.info(
        "✅ BOT DIRECT_STREAM_V2 iniciado."
    )


# ============================================================
# SHUTDOWN
# ============================================================

@app.on_event("shutdown")
async def shutdown_event():

    global shutting_down

    shutting_down = True

    logger.info(
        "🛑 Iniciando desligamento..."
    )

    # Cancelar monitoramentos
    tarefas = list(
        tarefas_monitoramento.values()
    )

    for tarefa in tarefas:

        tarefa.cancel()

    if tarefas:

        await asyncio.gather(
            *tarefas,
            return_exceptions=True
        )

    tarefas_monitoramento.clear()

    # Cancelar gravações
    gravacoes = []

    for valor in list(
        tarefas_gravacao.values()
    ):

        if isinstance(
            valor,
            asyncio.Task
        ):

            gravacoes.append(valor)

    for tarefa in gravacoes:

        tarefa.cancel()

    if gravacoes:

        await asyncio.gather(
            *gravacoes,
            return_exceptions=True
        )

    tarefas_gravacao.clear()

    await salvar_monitorados()

    if redis_client:

        try:

            await redis_client.close()

        except Exception:
            pass

    logger.info(
        "✅ Aplicação encerrada."
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
