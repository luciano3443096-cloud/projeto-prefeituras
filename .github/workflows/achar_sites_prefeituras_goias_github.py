import csv
import time
import unicodedata
from typing import Dict, List, Tuple
from urllib.parse import parse_qs, unquote, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import requests
from bs4 import BeautifulSoup

INPUT_CSV = "prefeituras_goias_para_achar_sites.csv"
OUTPUT_BEST = "sites_prefeituras_goias_encontrados.csv"
OUTPUT_CANDIDATES = "candidatos_sites_prefeituras_goias.csv"

TIMEOUT = 20
MAX_RESULTS_PER_QUERY = 5
SEARCH_SLEEP_SECONDS = 1.0
DEFAULT_WORKERS = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
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

_write_lock = Lock()
_fetch_cache: Dict[str, str] = {}
_fetch_cache_lock = Lock()



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
    return any(domain.endswith("." + bad) for bad in EXCLUDED_DOMAINS)



def fetch_html(url: str) -> str:
    if not url:
        return ""

    with _fetch_cache_lock:
        if url in _fetch_cache:
            return _fetch_cache[url]

    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        content_type = resp.headers.get("Content-Type", "")
        if resp.status_code == 200 and "text/html" in content_type:
            html = resp.text
        else:
            html = ""
    except requests.RequestException:
        html = ""

    with _fetch_cache_lock:
        _fetch_cache[url] = html

    return html



def search_duckduckgo(query: str, max_results: int = MAX_RESULTS_PER_QUERY) -> List[Dict[str, str]]:
    url = "https://duckduckgo.com/html/"
    results: List[Dict[str, str]] = []

    try:
        resp = requests.get(url, params={"q": query}, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        return results

    soup = BeautifulSoup(resp.text, "html.parser")
    anchors = soup.select("a.result__a")
    snippets = soup.select(".result__snippet")

    for i, a in enumerate(anchors[:max_results]):
        href = clean_result_url(a.get("href", "").strip())
        title = a.get_text(" ", strip=True)
        snippet = snippets[i].get_text(" ", strip=True) if i < len(snippets) else ""

        if href:
            results.append({
                "engine": "duckduckgo",
                "query": query,
                "title": title,
                "url": href,
                "snippet": snippet,
            })

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
        return -999, "url invalida"

    if is_excluded_domain(domain):
        return -999, "dominio excluido"

    if domain.endswith(".gov.br"):
        score += 50
        reasons.append("dominio gov.br")

    if "prefeitura" in domain:
        score += 15
        reasons.append("dominio menciona prefeitura")

    for bad in NEGATIVE_KEYWORDS:
        if bad in normalize_text(domain) or bad in combined_n:
            score -= 35
            reasons.append(f"parece legislativo/camara ({bad})")
            break

    if municipio_n in normalize_text(domain):
        score += 20
        reasons.append("municipio no dominio")

    if municipio_n in combined_n:
        score += 20
        reasons.append("municipio no titulo/snippet")

    if uf_n and uf_n in combined_n:
        score += 5
        reasons.append("uf no titulo/snippet")

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
            reasons.append("homepage menciona municipio")

        if "gabinete" in page_n:
            score += 5
            reasons.append("homepage menciona gabinete")

        if "ouvidoria" in page_n or "transparencia" in page_n:
            score += 5
            reasons.append("homepage menciona ouvidoria/transparencia")

        if "camara municipal" in page_n or "poder legislativo" in page_n:
            score -= 50
            reasons.append("homepage parece camara/legislativo")
    else:
        reasons.append("nao foi possivel validar homepage")

    return score, " | ".join(reasons[:6])



def classify_confidence(score: int) -> str:
    if score >= 85:
        return "alta"
    if score >= 55:
        return "media"
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
        raw_candidates.extend(search_duckduckgo(query))
        time.sleep(SEARCH_SLEEP_SECONDS)

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
            "status": "ok" if best["confianca"] in {"alta", "media"} else "revisar manualmente",
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
            "observacao": "nao houve resultado de busca",
        }

    return best_row, scored_candidates[:3]



def write_csv(path: str, rows: List[Dict[str, str]], fieldnames: List[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)



def save_partial(best_rows: List[Dict[str, str]], candidate_rows: List[Dict[str, str]]) -> None:
    with _write_lock:
        write_csv(
            OUTPUT_BEST,
            best_rows,
            [
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
            ],
        )
        write_csv(
            OUTPUT_CANDIDATES,
            candidate_rows,
            [
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
            ],
        )



def main() -> None:
    with open(INPUT_CSV, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = set(reader.fieldnames or [])
        required_columns = {"municipio", "uf", "codigo_ibge"}
        missing = required_columns - fieldnames
        if missing:
            raise ValueError(f"Colunas obrigatorias ausentes no CSV: {sorted(missing)}")
        rows = list(reader)

    workers = min(DEFAULT_WORKERS, max(1, len(rows)))
    print(f"Iniciando busca com {workers} workers paralelos.")

    best_rows: List[Dict[str, str]] = []
    candidate_rows: List[Dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(process_row, row): row for row in rows}

        for index, future in enumerate(as_completed(future_map), start=1):
            row = future_map[future]
            municipio = safe_get(row, "municipio")
            uf = safe_get(row, "uf")
            try:
                best_row, top_candidates = future.result()
                best_rows.append(best_row)
                candidate_rows.extend(top_candidates)
                save_partial(best_rows, candidate_rows)
                print(f"[{index}/{len(rows)}] Finalizado: {municipio}/{uf}")
            except Exception as exc:  # noqa: BLE001
                print(f"Erro em {municipio}/{uf}: {exc}")
                best_rows.append(
                    {
                        "municipio": municipio,
                        "uf": uf,
                        "codigo_ibge": safe_get(row, "codigo_ibge"),
                        "nome_prefeito": safe_get(row, "nome_prefeito"),
                        "site_oficial_prefeitura": "",
                        "pagina_encontrada": "",
                        "score": "",
                        "confianca": "revisar",
                        "status": "erro",
                        "observacao": str(exc),
                    }
                )
                save_partial(best_rows, candidate_rows)

    print("\nConcluido.")
    print(f"Melhor candidato por prefeitura: {OUTPUT_BEST}")
    print(f"Top candidatos para revisao: {OUTPUT_CANDIDATES}")


if __name__ == "__main__":
    main()
