import asyncio
import json
import os
import re
import subprocess
import time
import urllib.parse

import edge_tts
import requests
from google import genai

# ==========================================
# 1. CONFIGURAÇÕES (tudo vem das variáveis de ambiente / Secrets)
# ==========================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

TEMA = os.environ.get("TEMA", "3 curiosidades impressionantes sobre o espaço")
MODO_VISUAL = os.environ.get("MODO_VISUAL", "ia")
VOZ = os.environ.get("VOZ", "pt-BR-AntonioNeural")
VELOCIDADE_VOZ = os.environ.get("VELOCIDADE_VOZ", "+12%")

os.makedirs("temp", exist_ok=True)
os.makedirs("saida", exist_ok=True)


# ==========================================
# Funções auxiliares
# ==========================================
def rodar_ffmpeg(cmd: list):
    """Roda o FFmpeg e, se falhar, mostra o erro real no log."""
    resultado = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if resultado.returncode != 0:
        print("\n❌ ERRO NO FFMPEG:")
        print("Comando:", " ".join(cmd))
        print(resultado.stderr[-3000:])
        raise RuntimeError("FFmpeg falhou (veja o log acima).")


def baixar_arquivo(url: str, destino: str, headers: dict = None, tentativas: int = 3, timeout: int = 60, pausa: int = 4):
    """Baixa um arquivo verificando o status HTTP, com novas tentativas."""
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            if len(resp.content) < 1000:
                raise ValueError("Arquivo baixado é pequeno demais (provável erro do servidor).")
            with open(destino, "wb") as f:
                f.write(resp.content)
            return
        except Exception as e:
            ultimo_erro = e
            print(f"   Tentativa {tentativa}/{tentativas} falhou: {e}")
            if tentativa < tentativas:
                time.sleep(pausa)
    raise RuntimeError(f"Não foi possível baixar {url}: {ultimo_erro}")


def gerar_roteiro_e_cenas(tema: str) -> list:
    print("1/4 - Gerando roteiro e dividindo em cenas com Gemini...")
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
    Crie um roteiro curto e envolvente para um vídeo vertical (Reels/TikTok) de cerca de 25 a 35 segundos sobre: "{tema}".
    Comece com um gancho forte nos primeiros 3 segundos.
    Divida o roteiro em 5 a 7 cenas curtas.
    Responda EXCLUSIVAMENTE com um JSON válido no formato abaixo, sem crases ou texto extra:
    [
      {{
        "fala": "Frase curta em português que será narrada nesta cena.",
        "busca_ingles": "2 to 3 simple english keywords for stock video search (e.g. 'deep space galaxy')",
        "prompt_imagem": "Detailed vertical cinematic photo prompt in english for AI image generation"
      }}
    ]
    """
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    texto = response.text.strip()
    texto = re.sub(r"^```(?:json)?\s*|\s*```$", "", texto)
    return json.loads(texto)


# ==========================================
# Narração e legendas
# ==========================================
def srt_tempo_para_ass(tempo: str) -> str:
    """Converte '00:00:01,234' (SRT) para '0:00:01.23' (ASS)."""
    tempo = tempo.strip().replace(",", ".")
    horas, minutos, resto = tempo.split(":")
    segundos, milesimos = resto.split(".")
    centesimos = int(milesimos[:2].ljust(2, "0"))
    return f"{int(horas)}:{minutos}:{segundos}.{centesimos:02d}"


def converter_srt_para_ass_reels(srt_text: str, ass_path: str):
    """Converte SRT para legenda estilizada grande e centralizada estilo Reels/TikTok."""
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,64,&H0000FFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,2,60,60,380,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    blocos = srt_text.strip().split("\n\n")
    eventos = []
    for bloco in blocos:
        linhas = bloco.splitlines()
        if len(linhas) >= 3 and "-->" in linhas[1]:
            inicio_srt, fim_srt = linhas[1].split(" --> ")
            inicio = srt_tempo_para_ass(inicio_srt)
            fim = srt_tempo_para_ass(fim_srt)
            texto = " ".join(linhas[2:]).upper()
            eventos.append(f"Dialogue: 0,{inicio},{fim},Default,,0,0,0,,{texto}")

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(eventos))


async def gerar_audio_e_legenda(texto_completo: str, audio_path: str, ass_path: str):
    print("2/4 - Gerando narração e sincronizando legendas...")
    # WordBoundary = legenda palavra por palavra (estilo Reels)
    communicate = edge_tts.Communicate(
        texto_completo, VOZ, rate=VELOCIDADE_VOZ, boundary="WordBoundary"
    )
    submaker = edge_tts.SubMaker()

    with open(audio_path, "wb") as file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                file.write(chunk["data"])
            elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                submaker.feed(chunk)

    srt_content = submaker.get_srt()
    converter_srt_para_ass_reels(srt_content, ass_path)


def obter_duracao_audio(caminho: str) -> float:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", caminho
    ]
    resultado = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, check=True)
    return float(resultado.stdout.strip())


# ==========================================
# Cenas visuais
# ==========================================
def baixar_cena_pexels(query: str, indice: int, duracao: float) -> str:
    if not PEXELS_API_KEY:
        raise RuntimeError(
            "PEXELS_API_KEY não encontrada. Crie o secret em "
            "Settings > Secrets and variables > Actions, ou use o modo 'ia'."
        )

    headers = {"Authorization": PEXELS_API_KEY}
    url = f"https://api.pexels.com/videos/search?query={urllib.parse.quote(query)}&orientation=portrait&per_page=3"
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    videos = resp.json().get("videos", [])

    if not videos:
        # Fallback genérico se não achar o termo exato
        resp = requests.get(
            "https://api.pexels.com/videos/search?query=abstract%20background&orientation=portrait&per_page=3",
            headers=headers, timeout=15
        )
        resp.raise_for_status()
        videos = resp.json().get("videos", [])

    if not videos:
        raise RuntimeError(f"Nenhum vídeo encontrado no Pexels para: {query}")

    video_files = videos[0]["video_files"]
    # Pega arquivo HD vertical
    link = next((v["link"] for v in video_files if v.get("width", 0) >= 720), video_files[0]["link"])
    bruto = f"temp/bruto_{indice}.mp4"
    cortado = f"temp/cena_{indice}.mp4"

    baixar_arquivo(link, bruto, timeout=90)

    # Corta na duração exata da cena e ajusta para 1080x1920
    rodar_ffmpeg([
        "ffmpeg", "-y", "-stream_loop", "-1", "-i", bruto,
        "-t", f"{duracao:.2f}",
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30",
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", cortado
    ])
    return cortado


def gerar_cena_ia(prompt: str, indice: int, duracao: float) -> str:
    url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}?width=1080&height=1920&nologo=true"
    img_path = f"temp/img_{indice}.jpg"
    cortado = f"temp/cena_{indice}.mp4"

    # O nível anônimo do Pollinations limita a ~1 imagem a cada 15 s
    if indice > 0:
        time.sleep(16)
    baixar_arquivo(url, img_path, tentativas=4, timeout=90, pausa=16)

    frames = int(duracao * 30)
    # Zoom suave (efeito Ken Burns) na imagem estática, já em 30 fps
    rodar_ffmpeg([
        "ffmpeg", "-y", "-i", img_path,
        "-vf", (
            "scale=1280:2275,"
            f"zoompan=z='min(zoom+0.0015,1.15)':d={frames}"
            ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps=30"
        ),
        "-frames:v", str(frames),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", cortado
    ])
    return cortado


# ==========================================
# Programa principal
# ==========================================
async def main():
    cenas = gerar_roteiro_e_cenas(TEMA)
    texto_completo = " ".join([c["fala"] for c in cenas])
    print("\n--- ROTEIRO GERADO ---")
    print(texto_completo)
    print("----------------------\n")

    audio_final = "temp/narracao.mp3"
    legenda_ass = "temp/legendas.ass"
    await gerar_audio_e_legenda(texto_completo, audio_final, legenda_ass)

    duracao_total = obter_duracao_audio(audio_final)
    total_caracteres = sum(len(c["fala"]) for c in cenas)

    print(f"3/4 - Baixando/gerando {len(cenas)} cenas visuais ({MODO_VISUAL})...")
    arquivos_cenas = []
    for i, cena in enumerate(cenas):
        # Calcula duração proporcional ao tamanho da frase
        proporcao = len(cena["fala"]) / total_caracteres
        duracao_cena = max(1.5, duracao_total * proporcao)
        print(f"   Cena {i + 1}/{len(cenas)} ({duracao_cena:.1f}s)")
        if MODO_VISUAL == "ia":
            arq = gerar_cena_ia(cena["prompt_imagem"], i, duracao_cena)
        else:
            arq = baixar_cena_pexels(cena["busca_ingles"], i, duracao_cena)
        arquivos_cenas.append(arq)

    # Cria lista para concatenar cenas
    with open("temp/lista.txt", "w", encoding="utf-8") as f:
        for arq in arquivos_cenas:
            f.write(f"file '{os.path.abspath(arq)}'\n")

    print("4/4 - Renderizando vídeo final com legendas...")
    video_sem_legenda = "temp/video_base.mp4"
    rodar_ffmpeg([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "temp/lista.txt",
        "-c", "copy", video_sem_legenda
    ])

    video_final = "saida/reel_pronto.mp4"
    # Formata o caminho de forma segura para o filtro 'ass' do FFmpeg
    legenda_ffmpeg = os.path.abspath(legenda_ass).replace("\\", "/").replace(":", "\\:")

    # Junta vídeo + narração + queima legendas .ass
    rodar_ffmpeg([
        "ffmpeg", "-y", "-i", video_sem_legenda, "-i", audio_final,
        "-vf", f"ass='{legenda_ffmpeg}'",
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest",
        video_final
    ])
    print(f"\n✅ PRONTO! Vídeo salvo em: {os.path.abspath(video_final)}")


if __name__ == "__main__":
    asyncio.run(main())
