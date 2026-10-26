# Dipendenze leggere (compatibili con il limite di 512 MB di PythonAnywhere Free):
# pip install --user --no-cache-dir PyMuPDF EbookLib python-dotenv pyTelegramBotAPI requests
#
# Imposta GEMINI_API_KEY nel file .env (stessa cartella di questo script)
# oppure come variabile d'ambiente.

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from datetime import date
from difflib import SequenceMatcher
from html import escape
from pathlib import Path
from typing import Any

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24
except ImportError:
    import fitz  # type: ignore[no-redef]

import requests
from dotenv import load_dotenv
from ebooklib import epub

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

INPUT_DIR = Path(r"C:\Users\argen\Desktop\Internazionale da convertire")
RATE_LIMIT_SECONDS = float(os.getenv("RATE_LIMIT_SECONDS", "0.0"))
MAX_RETRIES = 5
RENDER_DPI = 150
JPEG_QUALITY = 80
PAGES_PER_BATCH = 2
_env_max_pages = os.getenv("MAX_PAGES", "").strip()
MAX_PAGES: int | None = int(_env_max_pages) if _env_max_pages.isdigit() else None
DEFAULT_MODEL = "gemini-2.5-flash"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

GEMINI_PROMPT = """
Sei un redattore-archivio esperto della rivista italiana «Internazionale».
Analizzi UNA COPPIA di pagine consecutive stampate (o una singola pagina finale)
e produci un JSON di estrazione pronto per l'impaginazione in eBook EPUB.

════════════════════════════════════════
A. COSA FARE
════════════════════════════════════════
1. Osserva le pagine fornite in ordine (dalla prima alla seconda) e distingui il tipo di contenuto.
2. Se ci sono articoli (anche brevi, recensioni o rubriche), estraili TUTTI nell'ordine
   di lettura naturale: prima pagina (dall'alto in basso, colonne da sinistra a destra),
   poi seconda pagina.
3. CASI SPECIALI:
   - PAGINA SOMMARIO (di solito pag. 5): ignora l'indice numerico di copertina, MA ESTRAI SEMPRE
     l'articolo/editoriale di apertura nella colonna di sinistra (rubrica "La settimana", firmato da Giovanni De Mauro).
   - RUBRICA "IMMAGINI" o "PORTFOLIO": estraili come articoli completi unendo la foto principale e il testo descrittivo.
   - GRAPHIC JOURNALISM / STRISCE (Fumetti): crea un articolo con formato="fumetto" e inserisci l'intera vignetta/tavola come blocco immagine, senza trascrivere i balloon come testo.
4. UNIONE TRA LE DUE PAGINE DEL BLOCCO:
   Se un articolo inizia nella prima pagina ricevuta e prosegue nella seconda,
   DEVI UNIRLO in un UNICO elemento dentro "articoli", ricucendo direttamente nel testo
   eventuali frasi o parole spezzate a cavallo tra fine della prima pagina e inizio della seconda.
5. CONTESTO DALLA PAGINA PRECEDENTE:
   Nel messaggio può esserti fornito il "CONTESTO PAGINA PRECEDENTE" (titolo, sezione, autore e
   ultime parole dell'articolo rimasto aperto).
   Se un articolo in queste pagine prosegue quel contesto:
   - Imposta "continua_da_prima": true e "in_apertura": false.
   - Riporta esattamente lo stesso "titolo" del contesto precedente.

════════════════════════════════════════
B. COSA IGNORARE
════════════════════════════════════════
- Pubblicità commerciali, inserti promozionali, abbonamenti, QR code commerciali.
- Numerazione di pagina isolata, testata ripetuta «internazionale», data, barcode.
- Colonphon redazionale (elenco redattori, stampa, amministrazione).
Se entrambe le pagine sono solo pubblicità o vuote: restituisci {"tipo_pagina":"pubblicita","articoli":[]}

════════════════════════════════════════
C. METADATI DA IDENTIFICARE PER OGNI ARTICOLO
════════════════════════════════════════
- sezione: scegli UNA macro-sezione standard tra:
  "In copertina", "La settimana", "Editoriali", "Le opinioni", "Attualità", "Europa",
  "Africa e Medio Oriente", "Americhe", "Asia e Pacifico", "Portfolio", "Ritratti",
  "Viaggi", "Graphic journalism", "Cultura - Schermi", "Cultura - Libri",
  "Cultura - Suoni e Arte", "Pop", "Economia e lavoro", "Scienza", "Corpo e mente", "Rubriche".
- occhiello: indicazione geografica o tematica sopra il titolo (es. "STRISCIA DI GAZA", "SENEGAL", "BRASILE", "FILM", "ROMANZO"). Vuoto "" se assente.
- formato: "copertina" (solo per il pezzo principale di copertina), "editoriale", "opinione", "articolo", "breve" (notizie brevi o recensioni), "portfolio", "fumetto".
- titolo: titolo principale.
- sottotitolo: catenaccio sotto il titolo. Vuoto se assente.
- autore: firma dell'articolo. Vuoto se assente.
- fonte: giornale di provenienza (es. «The Guardian», «Le Monde», «El País»). Vuoto se assente.
- data_originale: data pubblicazione originale se presente, altrimenti "".
- in_apertura: true se in QUESTE pagine compaiono titolo e/o inizio.
- continua_dopo: true se l'articolo prosegue nelle pagine successive.
- continua_da_prima: true se l'articolo riprende da una pagina precedente.
- continua_paragrafo_precedente: true se prosegue direttamente la frase della pagina prima.

════════════════════════════════════════
D. IMPAGINAMENTO DEL CORPO E IMMAGINI (campo "blocchi")
════════════════════════════════════════
Spezza il corpo in una lista ordinata di blocchi ("tipo" e "testo" o attributi specifici):
- "paragrafo"     corpo narrativo (un elemento per ogni capoverso).
- "intertitolo"   titoletto interno di sezione.
- "citazione"     pull-quote, stralcio evidenziato.
- "box"           riquadro laterale (metti il titolo in "titolo_blocco").
- "lista"         elenco puntato o numerato.
- "firma"         firma o nota in chiusura.
- "immagine"      foto, mappa, grafico o tavola a fumetti associata all'articolo.
  Per il blocco immagine, compila i campi:
  - "pagina": numero pagina relativa alla coppia (1 o 2)
  - "bbox": [ymin, xmin, ymax, xmax] coordinate normalizzate (da 0 a 1000) della foto/mappa nel foglio
  - "didascalia": testo della didascalia sotto la foto
  - "credito": autore dello scatto / agenzia (es. AP/Getty)

════════════════════════════════════════
E. FORMATO DI USCITA (solo JSON valido)
════════════════════════════════════════
{
  "tipo_pagina": "articolo" | "piu_articoli" | "seguito" | "pubblicita" | "altro",
  "articoli": [
    {
      "sezione": "Attualità",
      "occhiello": "UCRAINA",
      "formato": "articolo",
      "titolo": "",
      "sottotitolo": "",
      "autore": "",
      "fonte": "",
      "data_originale": "",
      "in_apertura": true,
      "continua_dopo": false,
      "continua_da_prima": false,
      "continua_paragrafo_precedente": false,
      "blocchi": [
        {"tipo": "paragrafo", "testo": "", "titolo_blocco": "", "pagina": 1, "bbox": [], "didascalia": "", "credito": ""}
      ]
    }
  ]
}
Non aggiungere testo fuori dal JSON.
""".strip()

TEXT_KEYS = (
    "sezione",
    "occhiello",
    "formato",
    "rubrica",
    "titolo",
    "sottotitolo",
    "autore",
    "fonte",
    "data_originale",
)
BLOCK_TYPES = {
    "paragrafo",
    "intertitolo",
    "citazione",
    "box",
    "didascalia",
    "lista",
    "firma",
    "immagine",
}
CONTINUATION_TITLES = {
    "segue",
    "(segue)",
    "continua",
    "(continua)",
    "seguito",
    "continuazione",
}
SENTENCE_END_CHARS = (".", "!", "?", "…", "»", '"', "”", ":", ";")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("convertitore")

ProgressCallback = Callable[[int, int, int], None]


class GeminiRestClient:
    """Client REST leggero per Gemini."""

    def __init__(self, api_key: str, model_name: str, system_instruction: str) -> None:
        self.api_key = api_key
        self.model_name = model_name.removeprefix("models/")
        self.system_instruction = system_instruction
        self.session = requests.Session()

    def generate_json(self, prompt: str, images_bytes: list[bytes]) -> str:
        url = f"{GEMINI_API_BASE}/{self.model_name}:generateContent"
        parts: list[dict[str, Any]] = [{"text": prompt}]
        for img in images_bytes:
            parts.append(
                {
                    "inlineData": {
                        "mimeType": "image/jpeg",
                        "data": base64.b64encode(img).decode("ascii"),
                    }
                }
            )

        body = {
            "systemInstruction": {
                "parts": [{"text": self.system_instruction}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": parts,
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }

        response = self.session.post(
            url,
            params={"key": self.api_key},
            json=body,
            timeout=120,
        )
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")

        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            return "[]"
        content_parts = (candidates[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in content_parts).strip()


def load_api_key() -> str:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("API key mancante. Crea un file .env con GEMINI_API_KEY=...")
    return key.strip()


def build_gemini_model() -> GeminiRestClient:
    api_key = load_api_key()
    model_name = os.getenv("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    log.info("Modello Gemini (REST): %s", model_name)
    return GeminiRestClient(
        api_key=api_key,
        model_name=model_name,
        system_instruction=GEMINI_PROMPT,
    )


def get_latest_pdf(folder: Path = INPUT_DIR) -> Path:
    if not folder.is_dir():
        raise FileNotFoundError(f"Cartella non trovata: {folder}")
    pdfs = list(folder.glob("*.pdf")) + list(folder.glob("*.PDF"))
    if not pdfs:
        raise FileNotFoundError(f"Nessun file PDF in {folder}")
    latest = max(pdfs, key=lambda p: (p.stat().st_mtime, p.stat().st_ctime, p.name))
    log.info("PDF selezionato (più recente): %s", latest.name)
    return latest


def render_page_jpeg(page: fitz.Page, dpi: int = RENDER_DPI) -> bytes:
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    try:
        return pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY)
    except TypeError:
        return pix.tobytes("jpeg")


def crop_bbox_from_page(page: fitz.Page, bbox: list[float], dpi: int = RENDER_DPI) -> bytes | None:
    """Ritaglia una regione [ymin, xmin, ymax, xmax] (0..1000) dalla pagina PDF."""
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None
    ymin, xmin, ymax, xmax = [max(0.0, min(1000.0, float(v))) for v in bbox]
    if xmax - xmin < 20 or ymax - ymin < 20:
        return None

    rect = page.rect
    clip = fitz.Rect(
        rect.x0 + (xmin / 1000.0) * rect.width,
        rect.y0 + (ymin / 1000.0) * rect.height,
        rect.x0 + (xmax / 1000.0) * rect.width,
        rect.y0 + (ymax / 1000.0) * rect.height,
    )
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
    try:
        return pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY)
    except TypeError:
        return pix.tobytes("jpeg")


def _strip_json_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _clean_text(value: str) -> str:
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _one_line(value: str) -> str:
    return re.sub(r"\s+", " ", _clean_text(value))


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n\n".join(_as_text(v) for v in value if _as_text(v)).strip()
    if isinstance(value, bool):
        return ""
    return _clean_text(str(value))


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "si", "sì", "yes"}
    return bool(value)


def _normalize_block(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        return {"tipo": "paragrafo", "testo": text, "titolo_blocco": "", "pagina": 1, "bbox": [], "didascalia": "", "credito": ""}
    if not isinstance(raw, dict):
        return None
    tipo = _as_text(raw.get("tipo") or "paragrafo").lower()
    if tipo not in BLOCK_TYPES:
        tipo = "paragrafo"
    
    text = _as_text(raw.get("testo") or raw.get("contenuto") or raw.get("testo_completo"))
    if not text and tipo != "immagine":
        return None

    try:
        pagina = int(raw.get("pagina", 1))
    except (TypeError, ValueError):
        pagina = 1

    bbox = raw.get("bbox") or []
    if not isinstance(bbox, list):
        bbox = []

    return {
        "tipo": tipo,
        "testo": text,
        "titolo_blocco": _as_text(raw.get("titolo_blocco") or raw.get("titolo")),
        "pagina": pagina,
        "bbox": bbox,
        "didascalia": _as_text(raw.get("didascalia")),
        "credito": _as_text(raw.get("credito")),
    }


def _blocks_from_raw(raw: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(raw.get("blocchi"), list):
        blocks = [_normalize_block(item) for item in raw["blocchi"]]
        return [b for b in blocks if b]
    body = _as_text(raw.get("testo_completo"))
    if not body:
        return []
    chunks = re.split(r"\n\s*\n", body)
    return [
        {"tipo": "paragrafo", "testo": chunk.strip(), "titolo_blocco": "", "pagina": 1, "bbox": [], "didascalia": "", "credito": ""}
        for chunk in chunks
        if chunk.strip()
    ]


def _text_from_blocks(blocks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for block in blocks:
        if block["tipo"] == "intertitolo":
            parts.append(block["testo"])
        elif block["tipo"] == "box" and block.get("titolo_blocco"):
            parts.append(block["titolo_blocco"] + "\n" + block["testo"])
        elif block["tipo"] == "paragrafo":
            parts.append(block["testo"])
    return "\n\n".join(parts).strip()


def _normalize_article(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    blocks = _blocks_from_raw(raw)
    article: dict[str, Any] = {key: _as_text(raw.get(key, "")) for key in TEXT_KEYS}
    
    if not article["sezione"]:
        article["sezione"] = "Attualità"
        
    article["in_apertura"] = _as_bool(raw.get("in_apertura", bool(article["titolo"])))
    article["continua_dopo"] = _as_bool(raw.get("continua_dopo", False))
    article["continua_da_prima"] = _as_bool(raw.get("continua_da_prima", False))
    article["continua_paragrafo_precedente"] = _as_bool(
        raw.get("continua_paragrafo_precedente", False)
    )
    article["blocchi"] = blocks
    article["testo_completo"] = _text_from_blocks(blocks)
    if not article["testo_completo"] and not article["titolo"] and not any(b["tipo"] == "immagine" for b in blocks):
        return None
    return article


def parse_gemini_articles(raw_text: str) -> list[dict[str, Any]]:
    cleaned = _strip_json_fence(raw_text)
    if not cleaned or cleaned in ("[]", "{}", "null"):
        return []

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", cleaned)
        if not match:
            return []
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []

    if data in (None, {}, []):
        return []

    candidates: list[Any]
    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        nested = None
        for key in ("articoli", "articles", "items", "data"):
            if isinstance(data.get(key), list):
                nested = data[key]
                break
        if nested is not None:
            candidates = nested
        elif any(k in data for k in TEXT_KEYS) or "blocchi" in data:
            candidates = [data]
        else:
            candidates = []
    else:
        candidates = []

    articles: list[dict[str, Any]] = []
    for item in candidates:
        article = _normalize_article(item)
        if article:
            articles.append(article)
    return articles


def _last_narrative_snippet(article: dict[str, Any], max_chars: int = 240) -> str:
    blocks = article.get("blocchi") or []
    for block in reversed(blocks):
        if block.get("tipo") == "paragrafo" and block.get("testo"):
            text = _one_line(block["testo"])
            return text[-max_chars:] if len(text) > max_chars else text
    full = _one_line(article.get("testo_completo") or "")
    return full[-max_chars:] if len(full) > max_chars else full


def _build_batch_prompt(
    page_numbers: list[int],
    total_pages: int,
    open_article_context: dict[str, Any] | None,
) -> str:
    if len(page_numbers) == 1:
        header = f"Stai analizzando la pagina {page_numbers[0]} di {total_pages}."
    else:
        header = (
            f"Stai analizzando in coppia le pagine consecutive {page_numbers[0]} e {page_numbers[1]} "
            f"(su {total_pages} totali). La prima immagine è pagina {page_numbers[0]} (pagina_indice: 1), "
            f"la seconda immagine è pagina {page_numbers[1]} (pagina_indice: 2)."
        )

    if not open_article_context:
        return header + "\nNessun articolo aperto dalla pagina precedente."

    ctx_title = open_article_context.get("titolo") or "(senza titolo)"
    ctx_sezione = open_article_context.get("sezione") or ""
    ctx_tail = open_article_context.get("ultime_parole") or ""

    lines = [
        header,
        "",
        "═══ CONTESTO PAGINA PRECEDENTE ═══",
        f"- Titolo articolo precedente: {ctx_title}",
        f"- Sezione: {ctx_sezione}",
        f'- Ultime parole: «...{ctx_tail}»',
        "Se il testo prosegue questo articolo, imposta 'continua_da_prima': true, 'in_apertura': false e 'titolo': "
        f'"{ctx_title}".'
    ]
    return "\n".join(lines)


def _extract_open_context(
    articles: list[dict[str, Any]],
    previous_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not articles:
        return previous_context

    candidate: dict[str, Any] | None = None
    for art in reversed(articles):
        if art.get("continua_dopo"):
            candidate = art
            break
    if candidate is None:
        candidate = articles[-1]

    return {
        "titolo": candidate.get("titolo") or "",
        "sezione": candidate.get("sezione") or "",
        "continua_dopo": bool(candidate.get("continua_dopo")),
        "ultime_parole": _last_narrative_snippet(candidate),
    }


def process_pages_with_gemini(
    model: GeminiRestClient,
    images_bytes: list[bytes],
    page_numbers: list[int],
    total_pages: int,
    open_article_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    label = (
        f"Pagina {page_numbers[0]}/{total_pages}"
        if len(page_numbers) == 1
        else f"Pagine {page_numbers[0]}-{page_numbers[-1]}/{total_pages}"
    )
    log.info("%s: invio a Gemini...", label)
    batch_prompt = _build_batch_prompt(page_numbers, total_pages, open_article_context)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            text = model.generate_json(batch_prompt, images_bytes)
            articles = parse_gemini_articles(text)
            log.info("%s: estratti %s articoli.", label, len(articles))
            return articles
        except Exception as exc:
            wait = 3.0 * attempt
            log.warning("%s: tentativo %s fallito (%s). Attendo...", label, attempt, exc)
            time.sleep(wait)
    return []


def _norm_title(title: str) -> str:
    title = title.lower().strip()
    title = re.sub(r"[\u2018\u2019`]", "'", title)
    title = re.sub(r"[\u201c\u201d]", '"', title)
    title = re.sub(r"\s+", " ", title)
    title = re.sub(r"[\s:;,.…\-–—]+$", "", title)
    return title


def _is_continuation(article: dict[str, Any], previous: dict[str, Any] | None) -> bool:
    if previous is None:
        return False
    if article.get("continua_da_prima"):
        return True
    title = _norm_title(article["titolo"])
    prev_title = _norm_title(previous["titolo"])
    if not title and not article.get("in_apertura", False):
        return True
    if title in CONTINUATION_TITLES or title.startswith("segue "):
        return True
    if prev_title and title == prev_title:
        return True
    if previous.get("continua_dopo") and not article.get("in_apertura", True):
        return True
    return False


def _should_stitch_paragraphs(prev_text: str, next_text: str, explicit_flag: bool = False) -> bool:
    prev_clean = prev_text.rstrip()
    next_clean = next_text.lstrip()
    if not prev_clean or not next_clean:
        return False
    if explicit_flag:
        return True
    ends_open = not prev_clean.endswith(SENTENCE_END_CHARS)
    first_alpha = next((ch for ch in next_clean if ch.isalpha()), "")
    starts_lower = bool(first_alpha and first_alpha.islower())
    return bool((prev_clean.endswith(("-", "–")) and starts_lower) or (ends_open and starts_lower))


def _join_split_paragraph_text(prev_text: str, next_text: str) -> str:
    prev_clean = prev_text.rstrip()
    next_clean = next_text.lstrip()
    if prev_clean.endswith("-") and len(prev_clean) > 1:
        return prev_clean[:-1] + next_clean
    return prev_clean + " " + next_clean


def _merge_into(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key in TEXT_KEYS:
        if not target[key] and incoming[key]:
            target[key] = incoming[key]

    incoming_blocks = [dict(b) for b in (incoming.get("blocchi") or [])]
    target_blocks = target["blocchi"]
    target_blocks.extend(incoming_blocks)
    target["testo_completo"] = _text_from_blocks(target_blocks)
    target["continua_dopo"] = bool(incoming.get("continua_dopo"))


def merge_articles(pages_articles: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    last_open_article: dict[str, Any] | None = None

    for articles in pages_articles:
        for article in articles:
            previous = merged[-1] if merged else None
            candidate_prev = last_open_article if (last_open_article and _is_continuation(article, last_open_article)) else previous
            if _is_continuation(article, candidate_prev):
                _merge_into(candidate_prev, article)
                continue
            merged.append({**article, "blocchi": [dict(b) for b in article.get("blocchi", [])]})
            if article.get("continua_dopo"):
                last_open_article = merged[-1]

    log.info("Articoli totali dopo il merge: %s", len(merged))
    return merged


def _render_blocks(blocks: list[dict[str, Any]], pdf_document: fitz.Document, batch_page_map: dict[int, fitz.Page], book: epub.EpubBook) -> str:
    html_parts: list[str] = []
    first_paragraph = True
    img_counter = 0

    for block in blocks:
        tipo = block["tipo"]
        text = escape(block.get("testo", "")).replace("\n", "<br/>")
        titolo_blocco = escape(block.get("titolo_blocco") or "")

        if tipo == "immagine":
            # Estrae l'immagine dal bbox usando il documento PDF
            pagina_relativa = block.get("pagina", 1)
            bbox = block.get("bbox", [])
            # Dobbiamo trovare la pagina corretta in base alla pagina relativa del batch
            # Nel batch salviamo le pagine elaborate. Per semplicità, passiamo il documento pdf e cerchiamo la pagina giusta se specificata nel blocco o gestita esternamente.
            # Qui riceviamo il documento fitz completo.
            pass  # Gestito nella creazione capitolo per avere accesso diretto alle pagine del PDF.

        if tipo == "intertitolo":
            html_parts.append(f"<h3>{text}</h3>")
            first_paragraph = False
        elif tipo == "citazione":
            html_parts.append(f"<blockquote><p>{text}</p></blockquote>")
            first_paragraph = False
        elif tipo == "box":
            inner = f"<h4>{titolo_blocco}</h4>" if titolo_blocco else ""
            html_parts.append(f'<aside class="box">{inner}<p>{text}</p></aside>')
            first_paragraph = False
        elif tipo == "didascalia":
            html_parts.append(f'<p class="didascalia">{text}</p>')
        elif tipo == "lista":
            items = [ln.strip() for ln in block["testo"].split("\n") if ln.strip()]
            lis = "".join(f"<li>{escape(item)}</li>" for item in items)
            html_parts.append(f"<ul>{lis}</ul>")
            first_paragraph = False
        elif tipo == "firma":
            html_parts.append(f'<p class="firma"><i>{text}</i></p>')
        else:
            if text:
                css = ' class="incipit"' if first_paragraph else ""
                html_parts.append(f"<p{css}>{text}</p>")
                first_paragraph = False
    return "\n".join(html_parts) if html_parts else "<p></p>"


EPUB_CSS = """
body { font-family: Georgia, serif; line-height: 1.5; margin: 1.2em 1.4em; color: #111; }
h1 { font-size: 1.6em; line-height: 1.2; margin: 0.1em 0 0.3em 0; }
h2 { font-size: 1.1em; font-weight: normal; font-style: italic; margin: 0 0 0.6em 0; color: #333; }
.sezione { font-size: 0.75em; letter-spacing: 0.1em; text-transform: uppercase; color: #666; margin: 0 0 0.2em 0; }
.occhiello { font-size: 0.85em; letter-spacing: 0.08em; text-transform: uppercase; color: #c0392b; font-weight: bold; margin: 0 0 0.3em 0; }
.meta { margin: 0 0 1em 0; color: #444; font-size: 0.9em; }
p { margin: 0 0 0.8em 0; text-align: justify; hyphens: auto; }
blockquote { margin: 1em 1.4em; border-left: 3px solid #bbb; font-style: italic; color: #222; padding-left: 0.8em; }
aside.box { margin: 1em 0; padding: 0.8em 1em; border: 1px solid #ccc; background: #f9f9f9; font-size: 0.95em; }
.didascalia { font-size: 0.85em; color: #555; font-style: italic; margin: 0.3em 0 0.8em 0; }
.articolo-img { max-width: 100%; height: auto; display: block; margin: 1em auto; border-radius: 4px; }
"""


def create_epub(
    articles: list[dict[str, Any]],
    output_path: Path,
    source_pdf: Path,
    pdf_document: fitz.Document,
) -> Path:
    if not articles:
        raise ValueError("Nessun articolo da inserire nell'EPUB.")

    today = date.today().isoformat()
    book = epub.EpubBook()
    book.set_identifier(str(uuid.uuid4()))
    book.set_title(f"Internazionale {today}")
    book.set_language("it")
    book.add_author("Internazionale")

    # Imposta la Copertina usando la pagina 1 del PDF
    if pdf_document.page_count > 0:
        cover_page = pdf_document[0]
        cover_bytes = render_page_jpeg(cover_page, dpi=150)
        cover_item = epub.EpubImage(
            uid="cover_image",
            file_name="images/cover.jpg",
            media_type="image/jpeg",
            content=cover_bytes,
        )
        book.add_item(cover_item)
        book.set_cover("images/cover.jpg", cover_bytes)

    style = epub.EpubItem(
        uid="style",
        file_name="style.css",
        media_type="text/css",
        content=EPUB_CSS.encode("utf-8"),
    )
    book.add_item(style)

    # Raggruppamento per Sezioni per indice gerarchico
    sections_map: dict[str, list[Any]] = {}
    
    spine: list[Any] = ["nav"]
    img_counter = 0

    for index, article in enumerate(articles, start=1):
        chapter_id = f"cap_{index:03d}"
        fallback_title = f"Articolo {index}"
        title = _one_line(article["titolo"]) or fallback_title
        sezione = article.get("sezione") or "Attualità"
        occhiello = article.get("occhiello") or ""

        # Costruisce l'HTML dell'articolo gestendo anche il ritaglio delle immagini dalle pagine del PDF
        html_parts: list[str] = []
        if sezione:
            html_parts.append(f'<p class="sezione">{escape(sezione)}</p>')
        if occhiello:
            html_parts.append(f'<p class="occhiello">{escape(occhiello)}</p>')
        html_parts.append(f"<h1>{escape(title)}</h1>")

        subtitle = article.get("sottotitolo")
        if subtitle:
            html_parts.append(f"<h2>{escape(subtitle)}</h2>")

        meta_bits = [bit for bit in (article.get("autore"), article.get("fonte"), article.get("data_originale")) if bit]
        if meta_bits:
            html_parts.append(f'<p class="meta"><i>{escape(" — ".join(meta_bits))}</i></p>')

        # Processa i blocchi, inclusi i ritagli immagine
        for block in article.get("blocchi", []):
            tipo = block["tipo"]
            if tipo == "immagine":
                img_counter += 1
                pagina_num = block.get("pagina", 1)
                bbox = block.get("bbox", [])
                
                # Trova la pagina corretta nel documento PDF (cerca approssimativamente in base al testo o all'indice)
                # Per semplicità usiamo la pagina corrente stimata o cerchiamo nel documento
                cropped_bytes = None
                for p_idx in range(len(pdf_document)):
                    # Cerchiamo di estrarre dal documento in base alla pagina indicata
                    pass
                # Fallback: usiamo la prima pagina o una pagina del documento se valida
                try:
                    target_page_idx = max(0, min(pdf_document.page_count - 1, block.get("_page_index", 0)))
                    if bbox and len(bbox) == 4:
                        cropped_bytes = crop_bbox_from_page(pdf_document[target_page_idx], bbox)
                except Exception:
                    pass

                if cropped_bytes:
                    img_filename = f"images/img_{img_counter:03d}.jpg"
                    img_item = epub.EpubImage(
                        uid=f"img_{img_counter:03d}",
                        file_name=img_filename,
                        media_type="image/jpeg",
                        content=cropped_bytes,
                    )
                    book.add_item(img_item)
                    html_parts.append(f'<img src="{img_filename}" class="articolo-img" alt="Immagine articolo" />')

                didascalia = block.get("didascalia")
                credito = block.get("credito")
                cap_text = " — ".join([t for t in [didascalia, credito] if t])
                if cap_text:
                    html_parts.append(f'<p class="didascalia">{escape(cap_text)}</p>')
            else:
                # Blocchi testuali standard
                text = escape(block.get("testo", "")).replace("\n", "<br/>")
                titolo_blocco = escape(block.get("titolo_blocco") or "")
                if tipo == "intertitolo":
                    html_parts.append(f"<h3>{text}</h3>")
                elif tipo == "citazione":
                    html_parts.append(f"<blockquote><p>{text}</p></blockquote>")
                elif tipo == "box":
                    inner = f"<h4>{titolo_blocco}</h4>" if titolo_blocco else ""
                    html_parts.append(f'<aside class="box">{inner}<p>{text}</p></aside>')
                elif tipo == "lista":
                    items = [ln.strip() for ln in block.get("testo", "").split("\n") if ln.strip()]
                    lis = "".join(f"<li>{escape(item)}</li>" for item in items)
                    html_parts.append(f"<ul>{lis}</ul>")
                elif tipo == "firma":
                    html_parts.append(f'<p class="firma"><i>{text}</i></p>')
                else:
                    if text:
                        html_parts.append(f"<p>{text}</p>")

        chapter = epub.EpubHtml(
            title=title[:200],
            file_name=f"{chapter_id}.xhtml",
            lang="it",
            uid=chapter_id,
        )
        chapter.content = "\n".join(html_parts)
        chapter.add_item(style)
        book.add_item(chapter)
        spine.append(chapter)

        if sezione not in sections_map:
            sections_map[sezione] = []
        sections_map[sezione].append(chapter)

    # Costruzione Indice Gerarchico (TOC) e Spine
    toc_structure = []
    for sezione, chapters in sections_map.items():
        toc_structure.append((epub.Section(sezione), tuple(chapters)))

    book.toc = tuple(toc_structure)
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    epub.write_epub(str(output_path), book, {"epub3_pages": False})
    log.info("EPUB salvato: %s", output_path)
    return output_path


def _cache_dir_for(pdf_path: Path) -> Path:
    cache_dir = pdf_path.parent / ".cache_internazionale" / pdf_path.stem
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def extract_articles_from_pdf(
    pdf_path: Path,
    model: GeminiRestClient,
) -> tuple[list[dict[str, Any]], fitz.Document]:
    document = fitz.open(pdf_path)
    total = min(document.page_count, MAX_PAGES) if MAX_PAGES else document.page_count
    cache_dir = _cache_dir_for(pdf_path)

    pages_articles: list[list[dict[str, Any]]] = []
    open_context: dict[str, Any] | None = None

    try:
        for start_idx in range(0, total, PAGES_PER_BATCH):
            batch_indices = list(range(start_idx, min(start_idx + PAGES_PER_BATCH, total)))
            page_numbers = [idx + 1 for idx in batch_indices]
            cache_file = cache_dir / f"pagine_{page_numbers[0]:03d}_{page_numbers[-1]:03d}.json"

            if cache_file.exists():
                try:
                    cached = json.loads(cache_file.read_text(encoding="utf-8"))
                    articles = [_normalize_article(item) for item in cached]
                    articles = [a for a in articles if a]
                    for a in articles:
                        for b in a.get("blocchi", []):
                            if b["tipo"] == "immagine":
                                p_rel = b.get("pagina", 1)
                                b["_page_index"] = batch_indices[0] if p_rel == 1 else batch_indices[-1]
                    pages_articles.append(articles)
                    open_context = _extract_open_context(articles, open_context)
                    continue
                except Exception:
                    pass

            images_bytes = [render_page_jpeg(document[idx]) for idx in batch_indices]
            articles = process_pages_with_gemini(
                model=model,
                images_bytes=images_bytes,
                page_numbers=page_numbers,
                total_pages=total,
                open_article_context=open_context,
            )
            for a in articles:
                for b in a.get("blocchi", []):
                    if b["tipo"] == "immagine":
                        p_rel = b.get("pagina", 1)
                        b["_page_index"] = batch_indices[0] if p_rel == 1 else batch_indices[-1]

            cache_file.write_text(json.dumps(articles, ensure_ascii=False, indent=2), encoding="utf-8")
            pages_articles.append(articles)
            open_context = _extract_open_context(articles, open_context)
            if RATE_LIMIT_SECONDS > 0:
                time.sleep(RATE_LIMIT_SECONDS)
    except Exception:
        pass

    merged = merge_articles(pages_articles)
    return merged, document


def convert_pdf_to_epub(pdf_path: Path, output_path: Path | None = None) -> tuple[Path, int]:
    model = build_gemini_model()
    articles, document = extract_articles_from_pdf(pdf_path, model)
    try:
        if not articles:
            raise ValueError("Nessun articolo estratto dal PDF.")
        if output_path is None:
            today = date.today().strftime("%Y-%m-%d")
            output_path = pdf_path.parent / f"Internazionale_{today}.epub"

        create_epub(articles, output_path, pdf_path, document)
        return output_path, len(articles)
    finally:
        document.close()


def main() -> None:
    pdf_path = get_latest_pdf(INPUT_DIR)
    today = date.today().strftime("%Y-%m-%d")
    output_path = INPUT_DIR / f"Internazionale_{today}.epub"
    convert_pdf_to_epub(pdf_path, output_path=output_path)


if __name__ == "__main__":
    main()
