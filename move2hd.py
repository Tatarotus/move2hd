import os
import re
import shutil
import requests
import logging
import json
from pathlib import Path
from time import sleep
from dotenv import load_dotenv
from ratelimit import limits, sleep_and_retry
from tqdm import tqdm

# Load environment variables
load_dotenv()
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
SAMBANOVA_API_KEY = os.getenv("SAMBANOVA_API_KEY")
if not TMDB_API_KEY or not SAMBANOVA_API_KEY:
    raise ValueError("API keys not found in .env")

# Configurable settings
LANGUAGE = os.getenv("LANGUAGE", "pt-BR")
SRC_DIR = Path(os.getenv("SRC_DIR", "."))
DEST_FILMES = Path(os.getenv("DEST_FILMES", "./FILMES"))
DEST_SERIES = Path(os.getenv("DEST_SERIES", "./SÉRIES"))
DEST_ANIMES = Path(os.getenv("DEST_ANIMES", "./ANIMES"))
DEST_PENDENTE = Path(os.getenv("DEST_PENDENTE", "./PENDENTES"))
VALID_EXTENSIONS = set(os.getenv("VALID_EXTENSIONS", ".mp4,.mkv,.avi,.mov,.wmv,.flv").lower().split(","))

# API settings
BASE_URL = "https://api.themoviedb.org/3"
SAMBANOVA_URL = "https://api.sambanova.ai/v1/chat/completions"
CALLS = 40
PERIOD = 10

# Cache and anime settings
CACHE_FILE = Path("llm_cache.json")
ANIME_KEYWORDS = [
    "[Erai-raws]", "CR WEB-DL", "MultiSub", "fansub",
    "BDrip", "JP audio", "SubsPlease", "Atelier", "Meister"
]

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
file_handler = logging.FileHandler("media_organizer.log")
file_handler.setFormatter(formatter)
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# Create destination folders
for path in [DEST_FILMES, DEST_SERIES, DEST_ANIMES, DEST_PENDENTE]:
    path.mkdir(parents=True, exist_ok=True)

def load_cache():
    return json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}

def save_cache(cache):
    CACHE_FILE.write_text(json.dumps(cache))

@sleep_and_retry
@limits(calls=CALLS, period=PERIOD)
def buscar_tmdb(titulo, tipo="tv", ano=None):
    endpoint = f"{BASE_URL}/search/{tipo}"
    params = {"api_key": TMDB_API_KEY, "query": titulo, "language": LANGUAGE}
    if ano and tipo == "movie":
        params["year"] = ano
    
    try:
        resp = requests.get(endpoint, params=params, timeout=10)
        resp.raise_for_status()
        dados = resp.json()
        
        if not dados["results"]:
            return None
            
        best_result = max(dados["results"], key=lambda x: x.get("popularity", 0))
        
        if tipo == "movie":
            movie_id = best_result["id"]
            details_url = f"{BASE_URL}/movie/{movie_id}"
            details_params = {"api_key": TMDB_API_KEY, "language": LANGUAGE}
            details_resp = requests.get(details_url, params=details_params)
            details_resp.raise_for_status()
            return details_resp.json()
            
        return best_result
    except Exception as e:
        logger.error(f"TMDB error: {e}")
        return None

def limpar_nome(nome):
    nome = Path(nome).stem
    nome = re.sub(r"(S\d{1,2}E\d{1,2}|\d{1,2}x\d{1,2})", lambda m: f" {m.group(1)} ", nome)
    patterns = [
        r"\[.*?\]", r"\b(19|20)\d{2}\b", r"\d{3,4}p",
        r"(WEB[-.]?DL|WEBRip|HDRip|BluRay|x264|x265|HEVC|AVC|AAC|6CH|DUAL)"
    ]
    for pattern in patterns:
        nome = re.sub(pattern, " ", nome, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", nome).strip()

def consultar_llm(filename):
    cache = load_cache()
    if filename in cache:
        return cache[filename]
    
    system_prompt = """Analyze media filenames and EXTRACT:
1. Type (movie/series/anime) using STRICT RULES:
   - 'anime' for Japanese content/fansub terms
   - 'series' if S##E##/Season/Episode
   - 'movie' otherwise
2. Clean title without technical terms
3. Year from 4 consecutive digits

Respond ONLY with valid JSON: {"title": "...", "type": "...", "year": ...}"""
    
    headers = {"Authorization": f"Bearer {SAMBANOVA_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "Meta-Llama-3.3-70B-Instruct",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Filename: {filename}"}
        ],
        "temperature": 0.1,
        "max_tokens": 150
    }
    
    try:
        response = requests.post(SAMBANOVA_URL, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()["choices"][0]["message"]["content"]
        clean_result = re.sub(r"```json|```", "", result).strip()
        parsed = json.loads(clean_result)
        
        if not all(key in parsed for key in ["title", "type"]):
            raise ValueError("Invalid LLM response")
            
        cache[filename] = parsed
        save_cache(cache)
        return parsed
    except Exception as e:
        logger.error(f"LLM error: {e}")
        return None

def sanitizar_nome_pasta(nome):
    # Remove special characters and normalize Portuguese characters
    replacements = {
        "ç": "c",
        "ã": "a",
        "á": "a",
        "à": "a",
        "â": "a",
        "é": "e",
        "ê": "e",
        "í": "i",
        "ó": "o",
        "ô": "o",
        "ú": "u",
        "ñ": "n",
        " ": "_"
    }
    sanitized = re.sub(r'[<>:"/\\|?*]', "_", nome.strip().lower())
    for char, replacement in replacements.items():
        sanitized = sanitized.replace(char, replacement)
    return sanitized

def mover(arquivo: Path, destino_base: Path, subpasta_nome: str):
    safe_name = sanitizar_nome_pasta(subpasta_nome)
    destino_final = destino_base / safe_name
    destino_final.mkdir(parents=True, exist_ok=True)
    
    counter = 1
    novo_caminho = destino_final / arquivo.name
    while novo_caminho.exists():
        novo_nome = f"{arquivo.stem}_{counter}{arquivo.suffix}"
        novo_caminho = destino_final / novo_nome
        counter += 1
    
    logger.info(f"Moving to {novo_caminho}")
    print(f"📁 Movendo para: {novo_caminho}")
    shutil.move(str(arquivo), str(novo_caminho))

def extract_anime_series_name(filename):
    # Remove anime-specific patterns and episode numbers
    patterns = [
        r"-\s*\d+(\s*\[)?",  # Episode numbers like "- 03"
        r"\s\d{2,3}(?:v\d+)?\s*\[",  # Episode numbers followed by [
        r"\[.*?\]",  # All bracketed content
        r"(?:EP|Episode)\s*\d+",  # Explicit episode markers
        r"\b\d{1,3}\b(?=\s*[^\d])"  # Standalone episode numbers
    ]
    for pattern in patterns:
        filename = re.sub(pattern, "", filename, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", filename).strip()

def main():
    arquivos = [f for f in SRC_DIR.rglob("*") if f.is_file() and f.suffix.lower() in VALID_EXTENSIONS]
    if not arquivos:
        print("⚠️ Nenhum arquivo encontrado")
        return

    for arquivo in tqdm(arquivos, desc="Organizando arquivos"):
        filename = arquivo.name
        logger.info(f"Processando: {filename}")
        
        # Anime keyword detection
        if any(kw in filename for kw in ANIME_KEYWORDS):
            print(f"🎌 Anime detectado: {filename}")
            series_name = extract_anime_series_name(limpar_nome(filename))
            mover(arquivo, DEST_ANIMES, series_name)
            continue
            
        # LLM classification
        llm_result = consultar_llm(filename)
        if llm_result and "title" in llm_result and "type" in llm_result:
            media_type = llm_result["type"].lower()
            title = llm_result["title"]
            year = llm_result.get("year")
            
            print(f"🤖 LLM classificou como {media_type}: {title}")
            
            if media_type == "anime":
                mover(arquivo, DEST_ANIMES, title)
                continue
            elif media_type == "series":
                mover(arquivo, DEST_SERIES, title)
                continue
            elif media_type == "movie":
                resultado = buscar_tmdb(title, tipo="movie", ano=year)
                if resultado:
                    genres = resultado.get("genres", [])
                    if genres:
                        primary_genre = genres[0]["name"].replace(" ", "_")
                    else:
                        primary_genre = "Outros"
                    mover(arquivo, DEST_FILMES / primary_genre, title)
                else:
                    mover(arquivo, DEST_FILMES / "Outros", title)
                continue

        # TMDB fallback
        print(f"⚠️ Fallback para {filename}")
        nome_limpo = limpar_nome(filename)
        
        # Series detection
        if re.search(r"(?:S\d+E\d+|EP?\d+)", filename):
            resultado = buscar_tmdb(nome_limpo, tipo="tv")
            if resultado:
                mover(arquivo, DEST_SERIES, resultado.get("name", nome_limpo))
                continue
        
        # Movie detection
        resultado = buscar_tmdb(nome_limpo, tipo="movie")
        if resultado:
            genres = resultado.get("genres", [])
            if genres:
                primary_genre = genres[0]["name"].replace(" ", "_")
            else:
                primary_genre = "Outros"
            mover(arquivo, DEST_FILMES / primary_genre, resultado.get("title", nome_limpo))
            continue
        
        # Final fallback
        mover(arquivo, DEST_PENDENTE, "Desconhecido")

if __name__ == "__main__":
    main()
