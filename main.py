#!/usr/bin/env python3
"""Coleta notícias globais sobre Inteligência Artificial e gera um relatório Markdown.

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
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import requests
import yaml
from dateutil import parser as date_parser
from dateutil import tz
from rapidfuzz import fuzz, process

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
OUTPUT_DIR = BASE_DIR / "output"

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
# A GDELT limita por IP: pede no mínimo 5s entre chamadas e bloqueia por vários
# minutos quem acumula requisições. Por isso os termos são agrupados em poucas
# consultas, espaçadas com folga e com esperas longas quando o bloqueio acontece.
GDELT_INTERVAL = 10.0
GDELT_KEYWORDS_PER_QUERY = 5
GDELT_BACKOFF = (60, 120, 240)
# Quando uma consulta devolve o máximo de registros, o dia é dividido em janelas
# menores para não perder notícias. O teto de requisições protege contra bloqueios.
GDELT_MAX_SPLIT_DEPTH = 2
GDELT_MAX_REQUESTS = 14
USER_AGENT = "IA-News/1.0 (+https://github.com/)"
HTTP_TIMEOUT = 60

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

# "AI", "A.I.", "IA", "I.A." em caixa alta são sinais de IA em praticamente qualquer idioma.
# A checagem é sensível a maiúsculas para não casar com palavras comuns ("j'ai", "ele ia").
AI_ACRONYM_PATTERN = re.compile(r"(?<![A-Za-z0-9])(?:A\.?I|I\.?A)\.?(?![A-Za-z0-9])")

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


@dataclass
class Article:
    title: str
    source: str
    published: dt.datetime
    language: str
    description: str
    url: str
    url_key: str


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


def is_ai_related(text: str, keywords: list[str]) -> bool:
    """Aceita o texto quando casa com um termo forte da lista, com um termo ambíguo
    em contexto de IA, ou com um sinal de IA em outro idioma."""
    lowered = text.lower()
    for keyword in keywords:
        if not token_pattern(keyword).search(lowered):
            continue
        if keyword.lower() in AMBIGUOUS_KEYWORDS:
            continue  # termos ambíguos são confirmados pelo contexto, abaixo
        return True
    return has_ai_context(text)


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
    print("  GDELT segue bloqueando; esta consulta foi ignorada (tente de novo mais tarde).")
    return []


def build_gdelt_queries(keywords: list[str]) -> list[tuple[str, bool]]:
    """Agrupa os termos em poucas consultas OR, para reduzir chamadas à API.

    Devolve pares (consulta, exige_contexto). Os termos ambíguos vão numa consulta
    própria que já exige contexto de IA no próprio artigo.
    """
    strong = [k for k in keywords if k.lower() not in AMBIGUOUS_KEYWORDS]
    ambiguous = [k for k in keywords if k.lower() in AMBIGUOUS_KEYWORDS]

    queries: list[tuple[str, bool]] = []
    for index in range(0, len(strong), GDELT_KEYWORDS_PER_QUERY):
        chunk = strong[index : index + GDELT_KEYWORDS_PER_QUERY]
        queries.append(("(" + " OR ".join(f'"{k}"' for k in chunk) + ")", False))
    if ambiguous:
        terms = " OR ".join(f'"{k}"' for k in ambiguous)
        queries.append((f'({terms}) ("AI" OR "artificial intelligence")', True))
    return queries


def fetch_gdelt(
    config: dict, keywords: list[str], start: dt.datetime, end: dt.datetime
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
    for index, (query, needs_context) in enumerate(queries, start=1):
        collected = gdelt_collect(
            session, query, needs_context, start, end, max_records, budget
        )
        articles.extend(collected)
        print(f"  GDELT [{index}/{len(queries)}]: {len(collected)} artigo(s)", flush=True)

    return articles


def gdelt_collect(
    session: requests.Session,
    query: str,
    needs_context: bool,
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
        published = parse_gdelt_date(item.get("seendate", ""))
        if published is None or not window_start <= published <= window_end:
            continue
        # Termos ambíguos (Claude, Gemini, Mistral) exigem sinal de IA no título.
        if needs_context and not has_ai_context(title):
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
            articles.extend(
                gdelt_collect(
                    session,
                    query,
                    needs_context,
                    sub_start,
                    sub_end,
                    max_records,
                    budget,
                    depth + 1,
                )
            )

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


def entry_description(entry) -> str:
    contents = entry.get("content") or []
    if contents:
        return clean_text(contents[0].get("value"))
    return clean_text(entry.get("summary") or entry.get("description"))


def fetch_rss(
    config: dict, keywords: list[str], start: dt.datetime, end: dt.datetime
) -> list[Article]:
    feeds = config.get("rss_feeds") or []
    articles: list[Article] = []

    for feed_config in feeds:
        if not feed_config.get("enabled", False):
            continue
        url = feed_config.get("url")
        if not url:
            continue
        name = feed_config.get("name") or urlparse(url).netloc
        try:
            parsed_feed = feedparser.parse(
                url, agent=USER_AGENT, request_headers={"Accept": "application/rss+xml, */*"}
            )
        except Exception as error:  # feedparser encapsula erros de rede de várias formas
            print(f"  Falha ao ler o feed '{name}': {error}")
            continue
        if getattr(parsed_feed, "bozo", 0) and not parsed_feed.entries:
            print(f"  Feed '{name}' indisponível: {parsed_feed.get('bozo_exception')}")
            continue

        feed_language = parsed_feed.feed.get("language") if parsed_feed.feed else ""
        kept = 0
        for entry in parsed_feed.entries:
            link = (entry.get("link") or "").strip()
            title = clean_text(entry.get("title"))
            if not link or not title:
                continue
            published = entry_datetime(entry)
            if published is None or not start <= published.astimezone(start.tzinfo) <= end:
                continue
            description = entry_description(entry)
            if not is_ai_related(f"{title} {description}", keywords):
                continue
            source = clean_text(entry.get("source", {}).get("title")) or name
            articles.append(
                Article(
                    title=title,
                    source=source,
                    published=published,
                    language=language_code(entry.get("language") or feed_language),
                    description=description,
                    url=clean_display_url(link),
                    url_key=normalize_url(link),
                )
            )
            kept += 1
        print(f"  RSS '{name}': {kept} artigo(s)", flush=True)

    return articles


# --------------------------------------------------------------------------- #
# Deduplicação e ordenação
# --------------------------------------------------------------------------- #


def normalize_title(title: str) -> str:
    text = re.sub(r"[^\w\s]", " ", title.lower())
    return re.sub(r"\s+", " ", text).strip()


def deduplicate(articles: list[Article], threshold: int) -> list[Article]:
    ordered = sorted(articles, key=lambda item: item.published, reverse=True)

    unique: list[Article] = []
    seen_urls: set[str] = set()
    for article in ordered:
        if article.url_key in seen_urls:
            continue
        seen_urls.add(article.url_key)
        unique.append(article)

    kept: list[Article] = []
    kept_titles: list[str] = []
    for article in unique:
        comparable = normalize_title(article.title)
        if comparable and kept_titles:
            match = process.extractOne(
                comparable,
                kept_titles,
                scorer=fuzz.token_sort_ratio,
                score_cutoff=threshold,
            )
            if match:
                continue
        kept.append(article)
        kept_titles.append(comparable)

    return kept


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
        "Relatório automático com notícias encontradas nas fontes configuradas.",
        "",
    ]

    if not articles:
        lines.append("Nenhuma notícia encontrada para esta data nas fontes configuradas.")
        lines.append("")
        return "\n".join(lines)

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
        lines.append(f"- **Link:** {article.url}")
        lines.append("")
        if article.description:
            lines.append(article.description)
            lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Execução
# --------------------------------------------------------------------------- #


def main() -> int:
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

    print(f"Coletando notícias de {target.isoformat()} ({timezone_name})")
    articles = fetch_gdelt(config, keywords, start, end)
    articles += fetch_rss(config, keywords, start, end)
    print(f"Total bruto: {len(articles)} artigo(s)")

    articles = deduplicate(articles, threshold)
    print(f"Após deduplicação: {len(articles)} artigo(s)")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / f"IA_{target.strftime('%Y%m%d')}.md"
    output_path.write_text(
        render_markdown(articles, target, zone, timezone_name), encoding="utf-8"
    )
    print(f"Arquivo gerado: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
