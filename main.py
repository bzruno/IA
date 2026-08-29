#!/usr/bin/env python3
"""Coleta notícias globais sobre Inteligência Artificial e gera um relatório Markdown
com o texto integral de cada notícia.

Uso:
    python main.py                    # notícias do dia anterior
    python main.py --date 2026-08-02  # reprocessa uma data específica
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import html
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import requests
import trafilatura
import yaml
from dateutil import parser as date_parser
from dateutil import tz
from rapidfuzz import fuzz

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
OUTPUT_DIR = BASE_DIR / "output"

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
# A GDELT limita por IP: pede no mínimo 5s entre chamadas e bloqueia por vários
# minutos quem acumula requisições. Por isso os termos são agrupados em poucas
# consultas, espaçadas com folga e com esperas longas quando o bloqueio acontece.
GDELT_INTERVAL = 10.0
GDELT_KEYWORDS_PER_QUERY = 5
GDELT_BACKOFF = (60, 120)
# Quando uma consulta devolve o máximo de registros, o dia é dividido em janelas
# menores para não perder notícias. O teto de requisições protege contra bloqueios.
GDELT_MAX_SPLIT_DEPTH = 2
GDELT_MAX_REQUESTS = 14
# A API rejeita a consulta inteira se qualquer termo tiver menos de 4 caracteres.
GDELT_MIN_PHRASE = 4
USER_AGENT = "IA-News/1.0 (+https://github.com/)"
HTTP_TIMEOUT = 60
# Para baixar as páginas das notícias: veículos costumam recusar clientes sem
# aparência de navegador. Nada aqui tenta contornar paywall — o que vier fechado
# simplesmente fica sem texto.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
}

# Termos que sozinhos não garantem relação com IA (nome de pessoa, signo, vento...).
AMBIGUOUS_KEYWORDS = {"gemini", "claude", "mistral"}

# "AI", "A.I.", "IA", "I.A." e "KI" (alemão) em caixa alta são sinais de IA em
# praticamente qualquer idioma. A checagem é sensível a maiúsculas para não casar
# com palavras comuns ("j'ai" em francês, "ele ia" em português).
AI_ACRONYM_PATTERN = re.compile(r"(?<![A-Za-z0-9])(?:A\.?I|I\.?A|KI)\.?(?![A-Za-z0-9])")

# Sinais de contexto de IA em vários idiomas (comparados sem distinção de maiúsculas).
AI_CONTEXT_TOKENS = [
    "artificial intelligence",
    "inteligência artificial",
    "inteligencia artificial",
    "intelligence artificielle",
    "intelligenza artificiale",
    "künstliche intelligenz",
    "kunstliche intelligenz",
    "искусственный интеллект",
    "الذكاء الاصطناعي",
    "人工知能",
    "人工智能",
    "인공지능",
    "openai",
    "anthropic",
    "deepmind",
    "chatgpt",
    "llm",
    "chatbot",
    "machine learning",
    "generative",
    "modelo de linguagem",
    "language model",
]

# --------------------------------------------------------------------------- #
# Relevância: separar notícia de impacto de ruído editorial
# --------------------------------------------------------------------------- #

# Um traço, barra vertical ou dois-pontos usados como separador no título.
_SEP = r"[-–—|:]"

# Formatos que não são notícia: podcast, vídeo, carta do leitor, coluna de
# opinião, webinar, newsletter, listão, tutorial, resenha, promoção e chamada de
# evento. Um único casamento descarta o item, por mais que ele cite IA.
NOISE_TITLE_PATTERNS = [
    # o próprio título anuncia o formato. "podcast" só conta como marcador de
    # formato (sufixo, prefixo ou composto alemão) — uma notícia *sobre* podcasts
    # continua valendo.
    rf"{_SEP}\s*(v[ií]deo)?podcasts?\s*$",
    r"^\s*podcasts?\s*[:–—-]",
    r"\w-podcast\b",
    r"\bder \w*podcast\b",
    r"\bvodcast\b",
    rf"{_SEP}\s*(v[ií]deo|audio|áudio|trailer|teaser|ao vivo|live ?blog)\s*$",
    rf"^\s*(v[ií]deo|audio|áudio|ao vivo|live ?blog|galeria|fotos)\s*{_SEP}",
    r"\blive (blog|updates?|coverage)\b",
    r"\bnewsletters?\b",
    r"\bwebinars?\b",
    r"\bwhite ?paper\b",
    r"#heiseshow",
    r"\bheise[- ]angebot\b",
    r"heise\+",
    r"software-architektur\.tv",
    r"\bthe download\b",
    # opinião, cartas do leitor e entrevistas
    rf"{_SEP}\s*(opini[ãa]o|opinion|an[áa]lise|editorial|coluna|kommentar|meinung"
    r"|entrevista|interview|debate)\s*$",
    rf"^\s*(opini[ãa]o|opinion|an[áa]lise|editorial|coluna|kommentar|meinung"
    rf"|entrevista|interview|q&a)\s*{_SEP}",
    r"\|\s*letters?\b",
    r"\bbrief letters\b",
    r"\bletters? to the editor\b",
    r"\bcartas? (do|dos) leitor",
    r"\bq&a\b",
    r"インタビュー",
    r"聞いた",
    r"対談",
    # listões, guias e tutoriais
    r"^\s*\d+\s+(new\s+)?(ways?|things?|tips?|reasons?|best|coisas|dicas|motivos|maneiras)\b",
    r"\bhow to\b",
    r"\bstep[- ]by[- ]step\b",
    r"\bcheat sheet\b",
    r"\btutorials?\b",
    r"\bhands[- ]on\b",
    r"\bexplained\s*[:$]",
    rf"{_SEP}?\s*\bexplained\s*$",
    r"\bexplicad[oa]s?\b",
    r"\bguia (de|para|completo)\b",
    r"\bcomo (fazer|usar|criar|escolher|instalar)\b",
    r"\bthings you (should|need to) know\b",
    # recomendação de ação e isca de clique financeira (Motley Fool e afins,
    # republicados por portais de finanças)
    r"\bmotley fool\b",
    r"\bstocks? to buy\b",
    r"\bbest stocks?\b",
    r"\bshould you buy\b",
    r"\bno[- ]brainer\b",
    r"\bmillionaire[- ]maker\b",
    r"\bthis (magnificent|incredible|amazing|monster|top|unstoppable)\b",
    r"\bbillionaire\b.{0,40}\b(bought|buying|just sold|is loading up)\b",
    r"\bup \d[\d,.\s]*%\s+since\b",
    r"^\s*prediction\s*:",
    r"\bações? para comprar\b",
    # comércio e promoções
    r"\bgift guide\b",
    r"\bbest deals?\b",
    r"\bdeals? of the (day|week)\b",
    r"\bblack friday\b",
    r"\bcoupons?\b",
    r"\bdiscount code\b",
    r"\bpromo[çc][ãa]o\b",
    # conteúdo patrocinado e chamada de evento
    r"\bsponsored\b",
    r"\badvertorial\b",
    r"\banzeige\b",
    r"\bpatrocinad[oa]s?\b",
    r"\bjoin us\b",
    r"\bregister (now|today)\b",
    r"\bsave the date\b",
    r"\btechcrunch disrupt\b",
    r"\bcall for (papers|speakers)\b",
    r"\bearly[- ]bird\b",
    # resenha de produto
    rf"^\s*review\s*{_SEP}",
    rf"{_SEP}\s*review\s*$",
]

# Seções que publicam qualquer coisa menos notícia. Comparadas só com o caminho
# da URL, para que um domínio como technologyreview.com não case por engano.
NOISE_PATH_PATTERNS = [
    r"/opinion(/|$)",
    r"/opiniao(/|$)",
    r"/opini[oó]n(/|$)",
    r"/commentisfree(/|$)",
    r"/meinung(/|$)",
    r"/kommentar",
    r"/colunas?(/|$)",
    r"/podcasts?(/|$)",
    r"/audio(/|$)",
    r"/videos?(/|$)",
    r"/live(/|$)",
    r"/liveblog",
    r"/newsletters?(/|$)",
    r"/webinar",
    r"/deals?(/|$)",
    r"/coupons?(/|$)",
    r"/gift",
    r"/quiz",
    r"/interviews?(/|$)",
    r"/reviews?(/|$)",
    r"/how-to(/|$)",
    r"/tutorial",
    r"/sponsored",
    r"/advertorial",
    r"/angebot",
    r"/events?(/|$)",
    r"/lifestyle(/|$)",
    r"/sports?(/|$)",
    r"/entertainment(/|$)",
    r"/culture(/|$)",
]

NOISE_TITLE_REGEX = [re.compile(pattern, re.IGNORECASE) for pattern in NOISE_TITLE_PATTERNS]
NOISE_PATH_REGEX = [re.compile(pattern, re.IGNORECASE) for pattern in NOISE_PATH_PATTERNS]

# Categorias de acontecimento e o peso de cada uma. Um título pode cair em mais
# de uma; o total é limitado por CATEGORY_SCORE_CAP para que um acúmulo de
# palavras-chave não supere uma notícia de fato importante.
EVENT_SIGNALS: dict[str, tuple[float, list[str]]] = {
    # lançamento de produto, modelo ou padrão técnico
    "lancamento": (
        4.0,
        [
            "launch", "launches", "launched", "launching", "introducing", "introduces",
            "unveils", "unveiled", "announces", "announced", "announcement", "releases",
            "released", "release", "debuts", "rolls out", "rolling out", "now available",
            "general availability", "open-sources", "open sources", "open weights",
            "open-weight", "rolled out", "ships", "new standard", "new protocol",
            "lança", "lançou", "lançamento", "apresenta", "anuncia", "anunciou", "estreia",
            "lanza", "presenta", "anuncia", "dévoile", "lance", "annonce",
            "stellt vor", "vorgestellt", "veröffentlicht", "kündigt an", "startet",
            "発表", "公開", "リリース", "提供開始", "投入", "発売",
            "推出", "发布", "上线", "출시", "공개",
        ],
    ),
    # modelo novo ou nova versão de modelo
    "modelo": (
        4.0,
        [
            "new model", "frontier model", "foundation model", "flagship model",
            "reasoning model", "multimodal model", "world model", "novo modelo",
            "neues modell", "新モデル", "新型モデル", "新模型", "새 모델",
        ],
    ),
    # aquisição, fusão, rodada de investimento
    "negocio": (
        3.5,
        [
            "acquire", "acquires", "acquired", "acquiring", "acquisition", "buys",
            "buyout", "merger", "merges", "takeover", "raises", "raised", "funding",
            "funding round", "valuation", "valued at", "invests", "investment", "ipo",
            "stake", "billion", "trillion",
            "adquire", "aquisição", "compra", "fusão", "bilhões", "trilhões",
            "investimento", "rodada", "avaliada", "aporte",
            "übernimmt", "übernahme", "kauft", "milliarden", "investiert", "beteiligung",
            "買収", "出資", "調達", "億ドル", "兆",
            "收购", "融资", "인수", "투자",
        ],
    ),
    # regulação, tribunais, política pública
    "politica": (
        3.0,
        [
            # "act" sozinho é verbo comum em inglês; só vale como nome de lei.
            "regulation", "regulator", "regulators", "regulatory", "law", "bill",
            "ai act", "court", "judge", "ruling", "lawsuit", "sues", "sued",
            "settlement", "ban", "bans", "banned", "antitrust", "probe", "subpoena",
            # "fine" também casaria "fine-tuning"; só a forma verbal conta.
            "fined", "fines",
            "sanctions", "export controls", "executive order", "congress", "senate",
            "parliament", "treaty", "moratorium",
            "regulação", "regulamentação", "lei", "projeto de lei", "tribunal", "juiz",
            "processo", "multa", "proibição", "veto", "liminar", "decreto",
            "gericht", "urteil", "klage", "gesetz", "verbot", "regulierung", "aufsicht",
            "規制", "訴訟", "判決", "法案", "禁止", "监管", "诉讼", "규제", "소송",
        ],
    ),
    # incidentes de segurança e uso indevido
    "seguranca": (
        2.5,
        [
            "hack", "hacked", "hackers", "hacking", "breach", "cyberattack",
            "cyberattacks", "malware", "exploit", "vulnerability", "ransomware",
            "leaked", "jailbreak", "misuse", "espionage", "deepfake", "deepfakes",
            "ataque", "invasão", "vazamento", "violação", "espionagem",
            "angriff", "sicherheitslücke", "datenleck",
            "攻撃", "侵入", "漏洞", "해킹",
        ],
    ),
    # pesquisa e resultados científicos
    "pesquisa": (
        2.5,
        [
            "research", "researchers", "study", "paper", "breakthrough", "discovery",
            "benchmark", "outperforms", "state-of-the-art", "arxiv",
            "peer-reviewed", "evaluation", "evaluations",
            "pesquisa", "estudo", "descoberta", "avanço", "artigo científico",
            "forschung", "studie", "durchbruch",
            "研究", "論文", "突破", "연구",
        ],
    ),
    # infraestrutura: chips, data centers, energia
    "infraestrutura": (
        2.0,
        [
            "data center", "data centre", "datacenter", "data centers", "data centres",
            "chip", "chips", "semiconductor", "semiconductors", "gpu", "gpus", "tpu",
            "wafer", "foundry", "hbm", "supercomputer", "gigawatt", "cluster",
            "centro de dados", "semicondutor", "semicondutores",
            "rechenzentrum", "halbleiter",
            "半導体", "データセンター", "数据中心", "반도체",
        ],
    ),
    # movimento de mercado e expansão de operação
    "negocios_operacao": (
        2.0,
        [
            "earnings", "revenue", "profit", "shares", "market cap", "layoffs",
            "cuts jobs", "resigns", "steps down", "expands", "expanding", "expansion",
            "opens", "opening", "enters", "partnership", "partners with", "deal",
            "receita", "lucro", "ações", "demissões", "expande", "expansão", "parceria",
            "operação", "escritório", "renuncia",
            "umsatz", "gewinn", "aktien", "entlassungen", "eröffnet", "partnerschaft",
            "決算", "売上", "株価", "進出", "提携",
        ],
    ),
}

CATEGORY_SCORE_CAP = 10.0
ENTITY_SCORE = 1.0
ENTITY_SCORE_CAP = 3.0
# Uma história publicada por vários veículos é, por definição, a mais relevante
# do dia. Cada veículo extra soma este bônus, até o teto.
CLUSTER_BONUS = 2.0
CLUSTER_BONUS_CAP = 6.0

# Famílias de modelo: o nome da família junto de um número de versão indica
# lançamento ou atualização de modelo, em qualquer idioma.
# "Nova" ficou de fora de propósito: em português é adjetivo comum ("nova versão"),
# e casaria com qualquer título que trouxesse um número.
MODEL_FAMILIES = [
    "gpt", "claude", "gemini", "llama", "grok", "qwen", "deepseek", "mistral", "glm",
    "phi", "sora", "veo", "imagen", "kimi", "ernie", "hunyuan", "doubao",
    "minimax", "granite", "falcon", "olmo", "stable diffusion", "flux", "whisper",
    "copilot", "titan", "command r", "gemma", "codestral",
]
# Versão curta (5, 4.8, 1.1) — quatro dígitos seriam ano, não versão.
VERSION_PATTERN = re.compile(r"(?<![\w.])\d{1,2}(?:\.\d+)?(?![\w])")

# Organizações cuja presença no título indica notícia de peso. As chaves são a
# forma canônica usada para agrupar a mesma história em idiomas diferentes.
MAJOR_ENTITIES: dict[str, list[str]] = {
    "openai": ["openai", "chatgpt", "sam altman"],
    "anthropic": ["anthropic", "claude"],
    "google": ["google", "alphabet", "deepmind", "gemini", "waymo", "youtube"],
    "meta": ["meta", "facebook", "instagram", "llama", "zuckerberg"],
    "microsoft": ["microsoft", "copilot", "azure"],
    "nvidia": ["nvidia", "jensen huang"],
    "amazon": ["amazon", "aws", "bedrock", "alexa"],
    "apple": ["apple", "siri"],
    "xai": ["xai", "grok"],
    "musk": ["elon musk"],
    "mistral": ["mistral"],
    "deepseek": ["deepseek"],
    "alibaba": ["alibaba", "qwen", "阿里巴巴"],
    "bytedance": ["bytedance", "tiktok", "doubao"],
    "baidu": ["baidu", "ernie"],
    "tencent": ["tencent", "hunyuan"],
    "huawei": ["huawei", "ascend"],
    "zhipu": ["zhipu", "glm", "智谱"],
    "moonshot": ["moonshot", "kimi"],
    "huggingface": ["hugging face", "huggingface", "hugging-face"],
    "tsmc": ["tsmc"],
    "samsung": ["samsung"],
    "skhynix": ["sk hynix", "sk-hynix"],
    "amd": ["amd"],
    "intel": ["intel"],
    "qualcomm": ["qualcomm"],
    "broadcom": ["broadcom"],
    "asml": ["asml"],
    "oracle": ["oracle"],
    "salesforce": ["salesforce", "agentforce"],
    "ibm": ["ibm", "watson"],
    "tesla": ["tesla"],
    "perplexity": ["perplexity"],
    "cohere": ["cohere"],
    "midjourney": ["midjourney"],
    "databricks": ["databricks"],
    "palantir": ["palantir"],
    "softbank": ["softbank"],
    "adobe": ["adobe", "firefly", "photoshop"],
    "safe_superintelligence": ["safe superintelligence", "ssi"],
    "thinking_machines": ["thinking machines"],
    "eu": [
        "european union", "european commission", "eu ai act", "brussels",
        "união europeia", "comissão europeia", "eu-kommission",
    ],
    # Uma mesma decisão é noticiada ora como "Pentagon", ora como "Trump
    # administration", ora como "US judge": todas apontam para o governo dos EUA,
    # e sem juntá-las a notícia sai duas vezes no relatório.
    "us_gov": [
        "white house", "pentagon", "congress", "u.s. senate", "ftc", "sec", "doj",
        "department of defense", "trump administration", "us judge", "u.s. judge",
        "federal judge", "us court", "u.s. court", "casa branca", "pentágono",
        "governo trump", "weißes haus", "us-regierung", "us-gericht",
    ],
    "united_nations": ["united nations", "nações unidas", "vereinte nationen"],
}

# Números com pelo menos dois dígitos ancoram a mesma história em idiomas
# diferentes ("12.9 billion" e "12,9 Milliarden").
NUMBER_PATTERN = re.compile(r"\d[\d.,]*\d|\d")


# GDELT devolve o idioma pelo nome em inglês; o relatório usa códigos ISO 639-1.
LANGUAGE_CODES = {
    "afrikaans": "af",
    "albanian": "sq",
    "arabic": "ar",
    "armenian": "hy",
    "azerbaijani": "az",
    "basque": "eu",
    "belarusian": "be",
    "bengali": "bn",
    "bosnian": "bs",
    "bulgarian": "bg",
    "catalan": "ca",
    "chinese": "zh",
    "croatian": "hr",
    "czech": "cs",
    "danish": "da",
    "dutch": "nl",
    "english": "en",
    "estonian": "et",
    "finnish": "fi",
    "french": "fr",
    "galician": "gl",
    "georgian": "ka",
    "german": "de",
    "greek": "el",
    "gujarati": "gu",
    "hebrew": "he",
    "hindi": "hi",
    "hungarian": "hu",
    "icelandic": "is",
    "indonesian": "id",
    "italian": "it",
    "japanese": "ja",
    "kannada": "kn",
    "kazakh": "kk",
    "khmer": "km",
    "korean": "ko",
    "latvian": "lv",
    "lithuanian": "lt",
    "macedonian": "mk",
    "malay": "ms",
    "malayalam": "ml",
    "marathi": "mr",
    "mongolian": "mn",
    "nepali": "ne",
    "norwegian": "no",
    "persian": "fa",
    "polish": "pl",
    "portuguese": "pt",
    "punjabi": "pa",
    "romanian": "ro",
    "russian": "ru",
    "serbian": "sr",
    "sinhala": "si",
    "slovak": "sk",
    "slovenian": "sl",
    "somali": "so",
    "spanish": "es",
    "swahili": "sw",
    "swedish": "sv",
    "tagalog": "tl",
    "tamil": "ta",
    "telugu": "te",
    "thai": "th",
    "turkish": "tr",
    "ukrainian": "uk",
    "urdu": "ur",
    "vietnamese": "vi",
    "welsh": "cy",
}


class GdeltBlocked(RuntimeError):
    """A GDELT recusou as requisições deste IP; insistir só prolonga o bloqueio."""


@dataclass
class Article:
    title: str
    source: str
    published: dt.datetime
    language: str
    description: str
    url: str
    url_key: str
    text: str = field(default="")
    # Preenchidos na etapa de relevância.
    outlets: int = field(default=1)
    score: float = field(default=0.0)


# --------------------------------------------------------------------------- #
# Configuração e datas
# --------------------------------------------------------------------------- #


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def resolve_timezone(name: str):
    zone = tz.gettz(name)
    if zone is None:
        raise SystemExit(f"Fuso horário desconhecido: {name}")
    return zone


def resolve_target_date(raw_date: str | None, zone) -> dt.date:
    if raw_date:
        try:
            return dt.datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            raise SystemExit(f"Data inválida: {raw_date} (use o formato YYYY-MM-DD)")
    return (dt.datetime.now(tz=zone) - dt.timedelta(days=1)).date()


def day_bounds(target: dt.date, zone) -> tuple[dt.datetime, dt.datetime]:
    """Início e fim do dia local, como datetimes com fuso."""
    start = dt.datetime.combine(target, dt.time.min, tzinfo=zone)
    end = dt.datetime.combine(target, dt.time.max, tzinfo=zone)
    return start, end


# --------------------------------------------------------------------------- #
# Normalização de texto, URL e idioma
# --------------------------------------------------------------------------- #


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_url(url: str) -> str:
    """Chave de deduplicação: sem esquema, sem 'www.', sem parâmetros de rastreamento."""
    parsed = urlparse(url.strip())
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(("https", netloc, path, "", urlencode(query), ""))


def clean_display_url(url: str) -> str:
    """Mantém a URL original, apenas sem os parâmetros de rastreamento."""
    parsed = urlparse(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    return urlunparse(parsed._replace(query=urlencode(query), fragment=""))


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def is_allowed_source(url: str, allowlist: list[str]) -> bool:
    """Aceita apenas domínios da lista (e seus subdomínios)."""
    if not allowlist:
        return True
    host = domain_of(url)
    return any(host == domain or host.endswith(f".{domain}") for domain in allowlist)


def build_tier_lookup(sources: dict):
    """Devolve uma função url -> peso, conforme as faixas de fonte da configuração.

    O peso mais específico ganha: um domínio listado em `tutorial` (blog de
    tutoriais de produto) pesa negativo mesmo que o domínio-raiz também apareça
    numa faixa alta.
    """
    tiers = sources.get("tiers") or {}
    default = float(sources.get("default_weight", 0))

    ranked: list[tuple[str, float]] = []
    for tier in tiers.values():
        weight = float((tier or {}).get("weight", 0))
        for domain in (tier or {}).get("domains") or []:
            ranked.append((str(domain).strip().lower(), weight))
    # Domínio mais longo primeiro: "aws.amazon.com" vence "amazon.com".
    ranked.sort(key=lambda item: len(item[0]), reverse=True)

    def tier_of(url: str) -> float:
        host = domain_of(url)
        for domain, weight in ranked:
            if host == domain or host.endswith(f".{domain}"):
                return weight
        return default

    return tier_of


def language_code(value: str | None) -> str:
    if not value:
        return ""
    # "pt-BR" -> "pt"; "Chinese (Simplified)" -> "chinese"
    raw = value.split("(")[0].strip().replace("_", "-").split("-")[0].strip().lower()
    if not raw:
        return ""
    return LANGUAGE_CODES.get(raw, raw)


@lru_cache(maxsize=None)
def token_pattern(token: str) -> re.Pattern:
    """Casa o termo isolado, sem exigir fronteira de palavra latina (funciona com CJK)."""
    return re.compile(
        r"(?<![a-z0-9])" + re.escape(token.lower()) + r"(?![a-z0-9])",
        re.IGNORECASE,
    )


def has_ai_context(text: str) -> bool:
    if AI_ACRONYM_PATTERN.search(text):
        return True
    lowered = text.lower()
    return any(token_pattern(token).search(lowered) for token in AI_CONTEXT_TOKENS)


def is_ai_related(text: str, keywords: list[str], trust_ambiguous: bool = False) -> bool:
    """Aceita o texto quando casa com um termo forte da lista, com um termo ambíguo
    em contexto de IA, ou com um sinal de IA em outro idioma.

    Em veículos que só publicam sobre IA (`trust_ambiguous`), termos como "Claude"
    e "Gemini" já valem sozinhos — sem isso, um título como "Introducing Claude
    Opus 5" seria descartado por não conter nenhuma outra palavra sobre IA.
    """
    lowered = text.lower()
    for keyword in keywords:
        if not token_pattern(keyword).search(lowered):
            continue
        if keyword.lower() in AMBIGUOUS_KEYWORDS and not trust_ambiguous:
            continue  # termos ambíguos são confirmados pelo contexto, abaixo
        return True
    return has_ai_context(text)


# --------------------------------------------------------------------------- #
# Relevância: ruído, categoria do acontecimento e pontuação
# --------------------------------------------------------------------------- #


def is_noise(title: str, url: str) -> bool:
    """Descarta o que não é notícia: podcast, vídeo, opinião, tutorial, promoção."""
    if any(regex.search(title) for regex in NOISE_TITLE_REGEX):
        return True
    path = urlparse(url).path
    return any(regex.search(path) for regex in NOISE_PATH_REGEX)


def is_model_release(title: str) -> bool:
    """Nome de família de modelo acompanhado de número de versão."""
    lowered = title.lower()
    if not any(token_pattern(family).search(lowered) for family in MODEL_FAMILIES):
        return False
    return bool(VERSION_PATTERN.search(title))


def event_categories(title: str) -> frozenset[str]:
    """Que tipo de acontecimento o título descreve (pode ser mais de um)."""
    lowered = title.lower()
    found = {
        category
        for category, (_, tokens) in EVENT_SIGNALS.items()
        if any(token_pattern(token).search(lowered) for token in tokens)
    }
    if is_model_release(title):
        found.add("modelo")
    return frozenset(found)


def entities_in(title: str) -> frozenset[str]:
    lowered = title.lower()
    return frozenset(
        canonical
        for canonical, aliases in MAJOR_ENTITIES.items()
        if any(token_pattern(alias).search(lowered) for alias in aliases)
    )


def normalize_number(raw: str) -> str:
    """Reduz '12,9' e '12.9' à mesma forma, e '12,900' a '12900'."""
    text = raw.replace(",", ".").strip(".")
    parts = [part for part in text.split(".") if part]
    if not parts:
        return ""
    head, tail = parts[0], parts[1:]
    # Um grupo de exatamente três dígitos é separador de milhar em qualquer notação.
    while tail and len(tail[0]) == 3:
        head += tail.pop(0)
    return head + ("." + ".".join(tail) if tail else "")


def significant_numbers(title: str) -> set[str]:
    """Números que identificam a história (valores, contagens) — anos não contam."""
    numbers = set()
    for raw in NUMBER_PATTERN.findall(title):
        value = normalize_number(raw)
        digits = value.replace(".", "")
        if len(digits) < 2:
            continue
        if len(digits) == 4 and value.isdigit() and 1900 <= int(value) <= 2100:
            continue  # ano
        numbers.add(value)
    return numbers


def story_anchors(title: str) -> frozenset[str]:
    """Marcas da história que sobrevivem à tradução: organizações e números."""
    return frozenset(entities_in(title) | significant_numbers(title))


def relevance_score(article: Article, tier_weight: float, outlets: int) -> float:
    categories = event_categories(article.title)
    score = min(
        sum(EVENT_SIGNALS[category][0] for category in categories),
        CATEGORY_SCORE_CAP,
    )
    score += min(len(entities_in(article.title)) * ENTITY_SCORE, ENTITY_SCORE_CAP)
    score += tier_weight
    score += min((outlets - 1) * CLUSTER_BONUS, CLUSTER_BONUS_CAP)
    return score


# --------------------------------------------------------------------------- #
# Coleta: GDELT
# --------------------------------------------------------------------------- #


def parse_gdelt_date(value: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=tz.UTC)
    except (TypeError, ValueError):
        pass
    try:
        parsed = date_parser.parse(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz.UTC)


def gdelt_request(session: requests.Session, params: dict) -> list[dict]:
    for wait in (*GDELT_BACKOFF, None):
        response = session.get(GDELT_URL, params=params, timeout=HTTP_TIMEOUT)
        if response.status_code == 429:
            if wait is None:
                break
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait = max(wait, int(retry_after))
            print(
                f"  GDELT bloqueou o IP por excesso de requisições; aguardando {wait}s...",
                flush=True,
            )
            time.sleep(wait)
            continue
        response.raise_for_status()
        try:
            return response.json().get("articles", []) or []
        except ValueError:
            # A API responde em texto puro quando a consulta é recusada.
            print(f"  Resposta não-JSON do GDELT: {response.text.strip()[:200]}")
            return []
    raise GdeltBlocked


def build_gdelt_queries(keywords: list[str]) -> list[str]:
    """Agrupa os termos em poucas consultas OR, para reduzir chamadas à API.

    Os termos ambíguos vão numa consulta própria, que já exige contexto de IA no
    próprio artigo.
    """
    # A GDELT recusa a consulta inteira ("The specified phrase is too short")
    # quando um dos termos tem menos de quatro caracteres, derrubando em silêncio
    # todo o grupo. Termos curtos como "LLM" e "xAI" ficam de fora por isso.
    usable = []
    for keyword in keywords:
        if len(keyword.strip()) < GDELT_MIN_PHRASE:
            print(f"  Termo '{keyword}' curto demais para a GDELT; será buscado só no RSS.")
            continue
        usable.append(keyword)

    strong = [k for k in usable if k.lower() not in AMBIGUOUS_KEYWORDS]
    ambiguous = [k for k in usable if k.lower() in AMBIGUOUS_KEYWORDS]

    queries: list[str] = []
    for index in range(0, len(strong), GDELT_KEYWORDS_PER_QUERY):
        chunk = strong[index : index + GDELT_KEYWORDS_PER_QUERY]
        queries.append("(" + " OR ".join(f'"{k}"' for k in chunk) + ")")
    if ambiguous:
        terms = " OR ".join(f'"{k}"' for k in ambiguous)
        queries.append(f'({terms}) ("AI" OR "artificial intelligence")')
    return queries


def fetch_gdelt(
    config: dict,
    keywords: list[str],
    allowlist: list[str],
    start: dt.datetime,
    end: dt.datetime,
) -> list[Article]:
    gdelt_config = config.get("gdelt") or {}
    if not gdelt_config.get("enabled", True):
        return []

    max_records = int(gdelt_config.get("max_records", 250))

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    queries = build_gdelt_queries(keywords)
    budget = [GDELT_MAX_REQUESTS]
    articles: list[Article] = []
    for index, query in enumerate(queries, start=1):
        try:
            collected = gdelt_collect(
                session, query, keywords, allowlist, start, end, max_records, budget
            )
        except GdeltBlocked:
            # O bloqueio é por IP e vale para a API inteira: insistir nas demais
            # consultas só prolongaria a punição. O relatório segue com os RSS.
            print(
                "  GDELT bloqueou este IP (limite por endereço, comum em nuvem). "
                "Consultas restantes canceladas; seguindo com os feeds RSS.",
                flush=True,
            )
            break
        articles.extend(collected)
        print(f"  GDELT [{index}/{len(queries)}]: {len(collected)} artigo(s)", flush=True)
        if budget[0] <= 0:
            print("  Limite de requisições ao GDELT atingido; seguindo com os feeds RSS.")
            break

    return articles


def gdelt_collect(
    session: requests.Session,
    query: str,
    keywords: list[str],
    allowlist: list[str],
    window_start: dt.datetime,
    window_end: dt.datetime,
    max_records: int,
    budget: list[int],
    depth: int = 0,
) -> list[Article]:
    """Consulta uma janela de tempo; se ela vier cheia, divide-a e consulta as metades."""
    if budget[0] <= 0:
        return []
    budget[0] -= 1
    if budget[0] < GDELT_MAX_REQUESTS - 1:
        time.sleep(GDELT_INTERVAL)

    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": max_records,
        "startdatetime": window_start.astimezone(tz.UTC).strftime("%Y%m%d%H%M%S"),
        "enddatetime": window_end.astimezone(tz.UTC).strftime("%Y%m%d%H%M%S"),
        "sort": "datedesc",
    }
    try:
        raw_articles = gdelt_request(session, params)
    except requests.RequestException as error:
        print(f"  Falha ao consultar o GDELT: {error}")
        return []

    articles: list[Article] = []
    for item in raw_articles:
        url = (item.get("url") or "").strip()
        title = clean_text(item.get("title"))
        if not url or not title:
            continue
        if not is_allowed_source(url, allowlist):
            continue
        published = parse_gdelt_date(item.get("seendate", ""))
        if published is None or not window_start <= published <= window_end:
            continue
        # O GDELT casa a consulta com o texto inteiro do artigo, então devolve muita
        # notícia que só cita IA de passagem. O título precisa confirmar o assunto.
        if not is_ai_related(title, keywords):
            continue
        if is_noise(title, url):
            continue
        articles.append(
            Article(
                title=title,
                source=clean_text(item.get("domain")) or urlparse(url).netloc,
                published=published,
                language=language_code(item.get("language")),
                description="",
                url=clean_display_url(url),
                url_key=normalize_url(url),
            )
        )

    if len(raw_articles) >= max_records and depth < GDELT_MAX_SPLIT_DEPTH:
        middle = window_start + (window_end - window_start) / 2
        print(
            f"  Consulta atingiu o limite de {max_records} registros; "
            f"dividindo a janela em {middle:%H:%M}.",
            flush=True,
        )
        for sub_start, sub_end in ((window_start, middle), (middle, window_end)):
            try:
                articles.extend(
                    gdelt_collect(
                        session,
                        query,
                        keywords,
                        allowlist,
                        sub_start,
                        sub_end,
                        max_records,
                        budget,
                        depth + 1,
                    )
                )
            except GdeltBlocked:
                # Preserva o que já foi coletado e encerra o uso da API.
                print("  GDELT bloqueou o IP no meio da coleta; mantendo o que já veio.")
                budget[0] = 0
                break

    return articles


# --------------------------------------------------------------------------- #
# Coleta: RSS
# --------------------------------------------------------------------------- #


def entry_datetime(entry) -> dt.datetime | None:
    for field in ("published_parsed", "updated_parsed"):
        parsed = entry.get(field)
        if parsed:
            return dt.datetime.fromtimestamp(calendar.timegm(parsed), tz=tz.UTC)
    for field in ("published", "updated", "created"):
        raw = entry.get(field)
        if raw:
            try:
                value = date_parser.parse(raw)
            except (TypeError, ValueError, OverflowError):
                continue
            return value if value.tzinfo else value.replace(tzinfo=tz.UTC)
    return None


def is_echo_of_title(description: str, title: str, source: str) -> bool:
    """Descrição que só repete o título (com ou sem o nome do veículo) não informa nada."""
    normalized = normalize_title(description)
    normalized_source = normalize_title(source)
    if normalized_source and normalized.endswith(normalized_source):
        normalized = normalized[: -len(normalized_source)].strip()
    return fuzz.token_sort_ratio(normalized, normalize_title(title)) >= 90


def entry_description(entry) -> str:
    contents = entry.get("content") or []
    if contents:
        return clean_text(contents[0].get("value"))
    return clean_text(entry.get("summary") or entry.get("description"))


def entry_source_title(entry) -> str:
    """O campo `source` nem sempre vem como dicionário; trata os dois formatos."""
    source = entry.get("source")
    if isinstance(source, dict):
        return clean_text(source.get("title"))
    return clean_text(source) if isinstance(source, str) else ""


def build_rss_article(
    entry,
    feed_name: str,
    feed_language: str,
    keywords: list[str],
    allowlist: list[str],
    ai_only: bool,
    start: dt.datetime,
    end: dt.datetime,
) -> Article | None:
    """Converte uma entrada de feed em Article, ou devolve None se ela não serve."""
    link = (entry.get("link") or "").strip()
    title = clean_text(entry.get("title"))
    if not link or not title:
        return None
    if not is_allowed_source(link, allowlist):
        return None
    published = entry_datetime(entry)
    if published is None or not start <= published.astimezone(start.tzinfo) <= end:
        return None
    # O assunto tem de estar no título. Aceitar a descrição fazia entrar matéria de
    # baleia, futebol ou LibreOffice que só cita IA de passagem. Feeds marcados como
    # `ai_only` já vêm filtrados pelo veículo, então "Introducing Claude Opus 5"
    # entra sem precisar repetir a palavra "IA".
    if not ai_only and not is_ai_related(title, keywords):
        return None
    if is_noise(title, link):
        return None

    description = entry_description(entry)
    source = entry_source_title(entry) or feed_name
    # Agregadores (Google News) anexam " - Veículo" ao título e repetem o título
    # como descrição; sem limpar isso a deduplicação não enxerga que duas entradas
    # são a mesma notícia.
    suffix = f" - {source}"
    if title.endswith(suffix) and len(title) > len(suffix):
        title = title[: -len(suffix)].strip()
    if description and is_echo_of_title(description, title, source):
        description = ""

    return Article(
        title=title,
        source=source,
        published=published,
        language=language_code(entry.get("language") or feed_language),
        description=description,
        url=clean_display_url(link),
        url_key=normalize_url(link),
    )


def fetch_rss(
    config: dict,
    keywords: list[str],
    allowlist: list[str],
    start: dt.datetime,
    end: dt.datetime,
) -> list[Article]:
    feeds = config.get("rss_feeds") or []
    articles: list[Article] = []

    # Vários veículos devolvem uma página de bloqueio para clientes que não
    # parecem navegador, então o XML é baixado aqui e só depois interpretado.
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": BROWSER_UA,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
            "Accept-Language": "*",
        }
    )

    for feed_config in feeds:
        if not feed_config.get("enabled", False):
            continue
        url = feed_config.get("url")
        if not url:
            continue
        name = feed_config.get("name") or urlparse(url).netloc
        ai_only = bool(feed_config.get("ai_only", False))
        try:
            response = session.get(url, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
            parsed_feed = feedparser.parse(response.content)
        except Exception as error:  # rede, XML corrompido, IDNA inválido...
            print(f"  Feed '{name}' indisponível: {error}")
            continue
        if not parsed_feed.entries:
            print(f"  Feed '{name}' sem entradas: {parsed_feed.get('bozo_exception')}")
            continue

        feed_language = parsed_feed.feed.get("language") if parsed_feed.feed else ""
        kept = 0
        for entry in parsed_feed.entries:
            try:
                article = build_rss_article(
                    entry, name, feed_language, keywords, allowlist, ai_only, start, end
                )
            except Exception as error:  # uma entrada malformada não derruba o feed
                print(f"  Entrada ignorada em '{name}': {error}")
                continue
            if article is None:
                continue
            articles.append(article)
            kept += 1
        print(f"  RSS '{name}': {kept} artigo(s)", flush=True)

    return articles


# --------------------------------------------------------------------------- #
# Agrupamento da mesma história e seleção das mais relevantes
# --------------------------------------------------------------------------- #


def normalize_title(title: str) -> str:
    text = re.sub(r"[^\w\s]", " ", title.lower())
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Story:
    """A mesma notícia contada por um ou mais veículos."""

    representative: Article
    comparable: str
    anchors: frozenset[str]
    categories: frozenset[str]
    outlets: int = 1
    tier: float = 0.0


def same_story(story: Story, comparable: str, anchors: frozenset[str],
               categories: frozenset[str], threshold: int) -> bool:
    """Duas notícias são a mesma história quando o título coincide, ou quando
    compartilham duas âncoras (organização, valor) e o tipo de acontecimento.

    A segunda regra é o que junta "Nvidia agrees to buy Hugging Face for $12.9
    billion" com "Nvidia übernimmt Hugging Face für 12,9 Milliarden Dollar";
    exigir o tipo de acontecimento em comum evita colar duas notícias diferentes
    que apenas citam as mesmas empresas.
    """
    if comparable and story.comparable:
        if fuzz.token_sort_ratio(comparable, story.comparable) >= threshold:
            return True
    if len(anchors & story.anchors) < 2:
        return False
    return bool(categories & story.categories)


def cluster_stories(articles: list[Article], threshold: int, tier_of) -> list[Story]:
    """Agrupa duplicatas e escolhe, para cada história, a versão de melhor fonte."""
    ordered = sorted(articles, key=lambda item: item.published, reverse=True)

    unique: list[Article] = []
    seen_urls: set[str] = set()
    for article in ordered:
        if article.url_key in seen_urls:
            continue
        seen_urls.add(article.url_key)
        unique.append(article)

    stories: list[Story] = []
    for article in unique:
        comparable = normalize_title(article.title)
        anchors = story_anchors(article.title)
        categories = event_categories(article.title)
        tier = tier_of(article.url)

        for story in stories:
            if not same_story(story, comparable, anchors, categories, threshold):
                continue
            story.outlets += 1
            # A versão publicada pela fonte mais forte vira a representante.
            if tier > story.tier:
                story.representative = article
                story.comparable = comparable
                story.anchors = anchors
                story.categories = categories
                story.tier = tier
            break
        else:
            stories.append(
                Story(
                    representative=article,
                    comparable=comparable,
                    anchors=anchors,
                    categories=categories,
                    tier=tier,
                )
            )

    return stories


def select_top(stories: list[Story], limit: int, min_score: float) -> list[Article]:
    """Pontua cada história e devolve as mais relevantes, da maior para a menor."""
    selected: list[Article] = []
    for story in stories:
        article = story.representative
        article.outlets = story.outlets
        article.score = relevance_score(article, story.tier, story.outlets)
        if article.score >= min_score:
            selected.append(article)

    selected.sort(key=lambda item: (item.score, item.published), reverse=True)
    return selected[:limit]


# --------------------------------------------------------------------------- #
# Texto integral das notícias
# --------------------------------------------------------------------------- #


def extract_text(html_bytes: bytes, url: str, title: str = "") -> str:
    """Extrai o corpo da notícia, descartando menus, anúncios e comentários."""
    try:
        text = trafilatura.extract(
            html_bytes,
            url=url,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
    except Exception:
        # HTML quebrado derruba o extrator; a notícia entra só com os metadados.
        return ""
    if not text:
        return ""
    paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
    # A extração costuma repetir o título como primeira linha do corpo.
    if paragraphs and title and normalize_title(paragraphs[0]) == normalize_title(title):
        paragraphs.pop(0)
    return "\n\n".join(paragraphs)


def fetch_full_texts(articles: list[Article], config: dict) -> None:
    """Baixa cada notícia e guarda o texto no artigo (in-place)."""
    settings = config.get("full_text") or {}
    if not settings.get("enabled", True) or not articles:
        return

    workers = max(1, int(settings.get("max_workers", 8)))
    timeout = int(settings.get("timeout_seconds", 20))
    min_chars = int(settings.get("min_chars", 400))
    max_chars = int(settings.get("max_chars", 20000))

    session = requests.Session()
    session.headers.update({"User-Agent": BROWSER_UA, "Accept-Language": "*"})

    def download(article: Article) -> None:
        # Qualquer erro aqui é local à notícia: sem texto, ela ainda entra no
        # relatório com os metadados. Antes, uma exceção fora de RequestException
        # (URL com domínio inválido, por exemplo) subia pelo pool e abortava a
        # execução inteira — foi assim que o relatório do dia deixou de sair.
        try:
            response = session.get(article.url, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
        except Exception:
            return
        try:
            text = extract_text(response.content, response.url, article.title)
        except Exception:
            return
        # Textos muito curtos costumam ser aviso de cookies ou chamada de paywall.
        if len(text) < min_chars:
            return
        if len(text) > max_chars:
            text = text[:max_chars].rsplit("\n\n", 1)[0] + "\n\n_[texto truncado]_"
        article.text = text

    print(f"Baixando o texto de {len(articles)} notícia(s)...", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(download, articles))

    with_text = sum(1 for article in articles if article.text)
    print(
        f"Texto integral obtido em {with_text} de {len(articles)} notícia(s).",
        flush=True,
    )


# --------------------------------------------------------------------------- #
# Relatório Markdown
# --------------------------------------------------------------------------- #


def render_markdown(articles: list[Article], target: dt.date, zone, timezone_name: str) -> str:
    human_date = target.strftime("%d/%m/%Y")
    lines = [
        "---",
        f'title: "Notícias de IA — {human_date}"',
        f'date: "{target.isoformat()}"',
        f'timezone: "{timezone_name}"',
        f"total_articles: {len(articles)}",
        "---",
        "",
        f"# Notícias de Inteligência Artificial — {human_date}",
        "",
        "Seleção automática das notícias de maior impacto do dia, apuradas em "
        "veículos oficiais. Ordenadas da mais para a menos relevante.",
        "",
    ]

    if not articles:
        lines.append("Nenhuma notícia encontrada para esta data nas fontes configuradas.")
        lines.append("")
        return "\n".join(lines)

    lines.append("## Resumo do dia")
    lines.append("")
    for index, article in enumerate(articles, start=1):
        lines.append(f"{index}. **{article.title}** — {article.source}")
    lines.extend(["", "---", ""])

    for index, article in enumerate(articles, start=1):
        if index > 1:
            lines.extend(["---", ""])
        published_local = article.published.astimezone(zone).strftime("%Y-%m-%d %H:%M")
        lines.append(f"## {index}. {article.title}")
        lines.append("")
        lines.append(f"- **Fonte:** {article.source}")
        lines.append(f"- **Publicado em:** {published_local}")
        if article.language:
            lines.append(f"- **Idioma:** {article.language}")
        if article.outlets > 1:
            lines.append(f"- **Cobertura:** {article.outlets} veículos noticiaram o caso")
        lines.append(f"- **Relevância:** {article.score:.1f}")
        lines.append(f"- **Link:** {article.url}")
        lines.append("")
        body = article.text or article.description
        if body:
            lines.append(sanitize_body(body))
            lines.append("")
        if not article.text:
            lines.append("_Texto integral indisponível na fonte._")
            lines.append("")

    return "\n".join(lines)


def sanitize_body(text: str) -> str:
    """Impede que o texto da notícia quebre a estrutura do Markdown."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"[-=_*]{3,}", stripped):
            continue  # viraria uma linha horizontal, separando notícias por engano
        if stripped.startswith("#"):
            stripped = "\\" + stripped  # viraria um título da hierarquia do relatório
        lines.append(stripped)
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- #
# Execução
# --------------------------------------------------------------------------- #


def main() -> int:
    # Títulos em japonês ou alemão aparecem nas mensagens de erro; num console
    # que não fala UTF-8 (Windows) isso derrubaria a execução no meio.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(
        description="Coleta notícias globais sobre Inteligência Artificial de um dia."
    )
    parser.add_argument(
        "--date",
        help="Data a reprocessar no formato YYYY-MM-DD (padrão: dia anterior).",
    )
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        raise SystemExit(f"Arquivo de configuração não encontrado: {CONFIG_PATH}")
    config = load_config(CONFIG_PATH)

    timezone_name = config.get("timezone") or "America/Sao_Paulo"
    zone = resolve_timezone(timezone_name)
    target = resolve_target_date(args.date, zone)
    start, end = day_bounds(target, zone)
    keywords = [str(item) for item in (config.get("keywords") or []) if str(item).strip()]
    threshold = int((config.get("deduplication") or {}).get("title_similarity_threshold", 92))

    sources = config.get("sources") or {}
    allowlist = []
    if sources.get("official_only", True):
        allowlist = [str(d).strip().lower() for d in (sources.get("allowlist") or []) if str(d).strip()]
    tier_of = build_tier_lookup(sources)

    report = config.get("report") or {}
    max_articles = int(report.get("max_articles", 15))
    min_score = float(report.get("min_score", 3))

    print(f"Coletando notícias de {target.isoformat()} ({timezone_name})")
    if allowlist:
        print(f"Restrito a {len(allowlist)} veículos oficiais da lista de fontes.")

    # Cada coletor é isolado: se um cair, o relatório sai com o que o outro trouxe.
    articles: list[Article] = []
    for label, collect in (
        ("GDELT", lambda: fetch_gdelt(config, keywords, allowlist, start, end)),
        ("RSS", lambda: fetch_rss(config, keywords, allowlist, start, end)),
    ):
        try:
            articles += collect()
        except Exception as error:
            print(f"  Coletor {label} falhou por completo: {error!r}")
    print(f"Total bruto: {len(articles)} artigo(s)")

    stories = cluster_stories(articles, threshold, tier_of)
    print(f"Histórias distintas: {len(stories)}")

    articles = select_top(stories, max_articles, min_score)
    print(
        f"Selecionadas {len(articles)} notícia(s) "
        f"(teto de {max_articles}, relevância mínima {min_score:g})."
    )

    try:
        fetch_full_texts(articles, config)
    except Exception as error:
        # Sem o texto integral o relatório ainda vale; sem relatório, o dia se perde.
        print(f"Falha ao baixar o texto das notícias: {error!r}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / f"IA_{target.strftime('%Y%m%d')}.md"
    output_path.write_text(
        render_markdown(articles, target, zone, timezone_name), encoding="utf-8"
    )
    print(f"Arquivo gerado: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
