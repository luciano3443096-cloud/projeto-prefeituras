import csv
import time
import unicodedata
from typing import Dict, List, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

# Arquivos adaptados para a sua base de Goiás
INPUT_CSV = "prefeituras_goias_para_achar_sites.csv"
OUTPUT_BEST = "sites_prefeituras_goias_encontrados.csv"
OUTPUT_CANDIDATES = "candidatos_sites_prefeituras_goias.csv"

SEARCH_SLEEP_SECONDS = 2.0
TIMEOUT = 20
MAX_RESULTS_PER_QUERY = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

EXCLUDED_DOMAINS = {
    "facebook.com",
    "www.facebook.com",
    "instagram.com",
    "www.instagram.com",
    "youtube.com",
    "www.youtube.com",
    "linkedin.com",
    "www.linkedin.com",
    "wikipedia.org",
    "pt.wikipedia.org",
}

NEGATIVE_KEYWORDS = [
    "camara",
    "câmara",
    "vereadores",
    "legislativo",
    "assembleia",
]

POSITIVE_KEYWORDS = [
    "prefeitura",
    "prefeitura municipal",
    "site oficial",
    "portal oficial",
    "gabinete",
    "ouvidoria",
    "transparencia",
    "transparência",
    "secretarias",
]

session = requests.Session()
fetch_cache: Dict[str, str] = {}


def strip_accents(text: str) -> str:
    if not text:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def normalize_text(text: str) -> str:
    return strip_accents(text).lower().strip()


def safe_get(row: Dict[str, str], key: str) -> str:
    return (row.get(key) or "").strip()


def clean_result_url(url: str) -> str:
    if not url:
        return ""

    url = url.strip()

    if "duckduckgo.com/l/?" in url or "duckduckgo.com/l/?" in url.replace("&amp;", "&"):
        parsed = urlparse(url.replace("&amp;", "&"))
        qs = parse_qs(parsed.query)
        if "uddg" in qs and qs["uddg"]:
            return unquote(qs["uddg"][0])

    if url.startswith("//"):
        url = "https:" + url

    return url


def get_root_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/"


def get_domain(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc.lower()


def is_excluded_domain(domain: str) -> bool:
    if domain in EXCLUDED_DOMAINS:
        return True
    for bad in EXCLUDED_DOMAINS:
        if domain.endswith("." + bad):
            return True
    return False


def fetch_html(url: str) -> str:
    if not url:
        return ""

    if url in fetch_cache:
        return fetch_cache[url]

    try:
        resp = session.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        content_type = resp.headers.get("Content-Type", "")
        if resp.status_code == 200 and "text/html" in content_type:
            fetch_cache[url] = resp.text
            return resp.text
    except requests.RequestException:
        pass

    fetch_cache[url] = ""
    return ""


def search_duckduckgo(query: str, max_results: int = MAX_RESULTS_PER_QUERY) -> List[Dict[str, str]]:
    url = "https://duckduckgo.com/html/"
    results: List[Dict[str, str]] = []

    try:
        resp = session.get(
            url,
            params={"q": query},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return results

    soup = BeautifulSoup(resp.text, "html.parser")

    anchors = soup.select("a.result__a")
    snippets = soup.select(".result__snippet")

    for i, a in enumerate(anchors[:max_results]):
        href = clean_result_url(a.get("href", "").strip())
        title = a.get_text(" ", strip=True)

        snippet = ""
        if i < len(snippets):
            snippet = snippets[i].get_text(" ", strip=True)

        if href:
            results.append(
                {
                    "engine": "duckduckgo",
                    "query": query,
                    "title": title,
                    "url": href,
                    "snippet": snippet,
                }
            )

    return results


def generate_queries(municipio: str, uf: str, nome_prefeito: str) -> List[str]:
    queries = [
        f'"Prefeitura Municipal de {municipio}" "{uf}" site oficial',
        f'"Prefeitura de {municipio}" "{uf}"',
        f'"{municipio}" "{uf}" prefeitura',
        f'"gabinete do prefeito" "{municipio}" "{uf}" prefeitura',
    ]

    if nome_prefeito:
        queries.append(f'"{nome_prefeito}" "{municipio}" "{uf}" prefeitura')

    return queries


def score_candidate(
    municipio: str,
    uf: str,
    nome_prefeito: str,
    candidate_url: str,
    title: str,
    snippet: str,
) -> Tuple[int, str]:
    score = 0
    reasons: List[str] = []

    municipio_n = normalize_text(municipio)
    uf_n = normalize_text(uf)
    prefeito_n = normalize_text(nome_prefeito)
    title_n = normalize_text(title)
    snippet_n = normalize_text(snippet)
    combined_n = f"{title_n} {snippet_n}"

    domain = get_domain(candidate_url)
    root_url = get_root_url(candidate_url)

    if not domain:
        return -999, "url inválida"

    if is_excluded_domain(domain):
        return -999, "domínio excluído"

    if domain.endswith(".gov.br"):
        score += 50
        reasons.append("domínio gov.br")

    if "prefeitura" in domain:
        score += 15
        reasons.append("domínio menciona prefeitura")

    for bad in NEGATIVE_KEYWORDS:
        if bad in normalize_text(domain) or bad in combined_n:
            score -= 35
            reasons.append(f"parece legislativo/câmara ({bad})")
            break

    if municipio_n in normalize_text(domain):
        score += 20
        reasons.append("município no domínio")

    if municipio_n in combined_n:
        score += 20
        reasons.append("município no título/snippet")

    if uf_n and uf_n in combined_n:
        score += 5
        reasons.append("UF no título/snippet")

    for good in POSITIVE_KEYWORDS:
        if normalize_text(good) in combined_n:
            score += 8
            reasons.append(f"keyword positiva ({good})")

    if prefeito_n and prefeito_n in combined_n:
        score += 10
        reasons.append("nome do prefeito encontrado")

    html = fetch_html(root_url)
    if html:
        soup = BeautifulSoup(html, "html.parser")
        page_text = soup.get_text(" ", strip=True)
        page_n = normalize_text(page_text[:15000])

        if "prefeitura" in page_n:
            score += 20
            reasons.append("homepage menciona prefeitura")

        if municipio_n in page_n:
            score += 15
            reasons.append("homepage menciona município")

        if "gabinete" in page_n:
            score += 5
            reasons.append("homepage menciona gabinete")

        if "ouvidoria" in page_n or "transparencia" in page_n:
            score += 5
            reasons.append("homepage menciona ouvidoria/transparência")

        if "camara municipal" in page_n or "poder legislativo" in page_n:
            score -= 50
            reasons.append("homepage parece câmara/legislativo")
    else:
        reasons.append("não foi possível validar homepage")

    return score, " | ".join(reasons[:6])


def classify_confidence(score: int) -> str:
    if score >= 85:
        return "alta"
    if score >= 55:
        return "média"
    if score >= 30:
        return "baixa"
    return "revisar"


def deduplicate_candidates(candidates: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    deduped: List[Dict[str, str]] = []

    for item in candidates:
        root = get_root_url(item["url"])
        key = root or item["url"]
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    return deduped


def process_row(row: Dict[str, str]) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    municipio = safe_get(row, "municipio")
    uf = safe_get(row, "uf")
    codigo_ibge = safe_get(row, "codigo_ibge")
    nome_prefeito = safe_get(row, "nome_prefeito")

    queries = generate_queries(municipio, uf, nome_prefeito)

    raw_candidates: List[Dict[str, str]] = []

    for query in queries:
        print(f"Buscando: {municipio}/{uf} -> {query}")
        results = search_duckduckgo(query)
        time.sleep(SEARCH_SLEEP_SECONDS)

        for res in results:
            raw_candidates.append(res)

    raw_candidates = deduplicate_candidates(raw_candidates)

    scored_candidates: List[Dict[str, str]] = []
    for cand in raw_candidates:
        score, reason = score_candidate(
            municipio=municipio,
            uf=uf,
            nome_prefeito=nome_prefeito,
            candidate_url=cand["url"],
            title=cand["title"],
            snippet=cand["snippet"],
        )
        scored_candidates.append(
            {
                "municipio": municipio,
                "uf": uf,
                "codigo_ibge": codigo_ibge,
                "nome_prefeito": nome_prefeito,
                "engine": cand["engine"],
                "query": cand["query"],
                "pagina_encontrada": cand["url"],
                "site_oficial_candidato": get_root_url(cand["url"]),
                "titulo": cand["title"],
                "snippet": cand["snippet"],
                "score": str(score),
                "confianca": classify_confidence(score),
                "observacao": reason,
            }
        )

    scored_candidates.sort(key=lambda x: int(x["score"]), reverse=True)

    if scored_candidates:
        best = scored_candidates[0]
        best_row = {
            "municipio": municipio,
            "uf": uf,
            "codigo_ibge": codigo_ibge,
            "nome_prefeito": nome_prefeito,
            "site_oficial_prefeitura": best["site_oficial_candidato"],
            "pagina_encontrada": best["pagina_encontrada"],
            "score": best["score"],
            "confianca": best["confianca"],
            "status": "ok" if best["confianca"] in {"alta", "média"} else "revisar manualmente",
            "observacao": best["observacao"],
        }
    else:
        best_row = {
            "municipio": municipio,
            "uf": uf,
            "codigo_ibge": codigo_ibge,
            "nome_prefeito": nome_prefeito,
            "site_oficial_prefeitura": "",
            "pagina_encontrada": "",
            "score": "",
            "confianca": "revisar",
            "status": "nenhum candidato encontrado",
            "observacao": "não houve resultado de busca",
        }

    return best_row, scored_candidates[:3]


def main() -> None:
    best_rows: List[Dict[str, str]] = []
    candidate_rows: List[Dict[str, str]] = []

    with open(INPUT_CSV, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        required_columns = {"municipio", "uf", "codigo_ibge"}
        missing = required_columns - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Colunas obrigatórias ausentes no CSV: {sorted(missing)}")

        for row in reader:
            best_row, top_candidates = process_row(row)
            best_rows.append(best_row)
            candidate_rows.extend(top_candidates)

    with open(OUTPUT_BEST, "w", encoding="utf-8", newline="") as f:
        fieldnames = [
            "municipio",
            "uf",
            "codigo_ibge",
            "nome_prefeito",
            "site_oficial_prefeitura",
            "pagina_encontrada",
            "score",
            "confianca",
            "status",
            "observacao",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(best_rows)

    with open(OUTPUT_CANDIDATES, "w", encoding="utf-8", newline="") as f:
        fieldnames = [
            "municipio",
            "uf",
            "codigo_ibge",
            "nome_prefeito",
            "engine",
            "query",
            "pagina_encontrada",
            "site_oficial_candidato",
            "titulo",
            "snippet",
            "score",
            "confianca",
            "observacao",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(candidate_rows)

    print("\nConcluído.")
    print(f"Melhor candidato por prefeitura: {OUTPUT_BEST}")
    print(f"Top candidatos para revisão: {OUTPUT_CANDIDATES}")


if __name__ == "__main__":
    main()
