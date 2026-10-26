# Convertitore Internazionale to EPUB (Versione Cloud / Streamlit)
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
    import pymupdf as fitz
except ImportError:
    import fitz

import requests
from dotenv import load_dotenv
from ebooklib import epub

RATE_LIMIT_SECONDS = float(os.getenv("RATE_LIMIT_SECONDS", "0.0"))
MAX_RETRIES = 5
RENDER_DPI = 150
JPEG_QUALITY = 80
PAGES_PER_BATCH = 2
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
- formato: "copertina", "editoriale", "opinione", "articolo", "breve", "portfolio", "fumetto".
- titolo, sottotitolo, autore, fonte, data_originale, in_apertura, continua_dopo, continua_da_prima, continua_paragrafo_precedente.

════════════════════════════════════════
D. IMPAGINAMENTO DEL CORPO E IMMAGINI (campo "blocchi")
════════════════════════════════════════
Spezza il corpo in una lista ordinata di blocchi ("tipo" e "testo" o attributi specifici):
- "paragrafo", "intertitolo", "citazione", "box", "lista", "firma", "immagine".
Per il blocco immagine, compila: "pagina" (1 o 2), "bbox" [ymin, xmin, ymax, xmax] (0..1000), "didascalia", "credito".

════════════════════════════════════════
E. FORMATO DI USCITA (solo JSON valido)
════════════════════════════════════════
{
  "tipo_pagina": "articolo" | "piu_articoli" | "seguito" | "pubblicita" | "altro",
  "articoli": [
    {
      "sezione": "Attualità",
      "occhiello": "",
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
"""

TEXT_KEYS = ("sezione", "occhiello", "formato", "rubrica", "titolo", "sottotitolo", "autore", "fonte", "data_originale")
BLOCK_TYPES = {"paragrafo", "intertitolo", "citazione", "box", "didascalia", "lista", "firma", "immagine"}
CONTINUATION_TITLES = {"segue", "(segue)", "continua", "(continua)", "seguito", "continuazione"}
SENTENCE_END_CHARS = (".", "!", "?", "…", "»", '"', "”", ":", ";")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
log = logging.getLogger("convertitore")


class GeminiRestClient:
    def __init__(self, api_key: str, model_name: str, system_instruction: str) -> None:
        self.api_key = api_key
        self.model_name = model_name.removeprefix("models/")
        self.system_instruction = system_instruction
        self.session = requests.Session()

    def generate_json(self, prompt: str, images_bytes: list[bytes]) -> str:
        url = f"{GEMINI_API_BASE}/{self.model_name}:generateContent"
        parts: list[dict[str, Any]] = [{"text": prompt}]
        for img in images_bytes:
            parts.append({
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": base64.b64encode(img).decode("ascii"),
                }
            })
        body = {
            "systemInstruction": {"parts": [{"text": self.system_instruction}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
        }
        response = self.session.post(url, params={"key": self.api_key}, json=body, timeout=120)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            return "[]"
        content_parts = (candidates[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in content_parts).strip()


def build_gemini_model(api_key: str) -> GeminiRestClient:
    model_name = os.getenv("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    return GeminiRestClient(api_key=api_key.strip(), model_name=model_name, system_instruction=GEMINI_PROMPT)


def render_page_jpeg(page: fitz.Page, dpi: int = RENDER_DPI) -> bytes:
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    try:
        return pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY)
    except TypeError:
        return pix.tobytes("jpeg")


def crop_bbox_from_page(page: fitz.Page, bbox: list[float], dpi: int = RENDER_DPI) -> bytes | None:
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
    return [{"tipo": "paragrafo", "testo": chunk.strip(), "titolo_blocco": "", "pagina": 1, "bbox": [], "didascalia": "", "credito": ""} for chunk in chunks if chunk.strip()]


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
    article["continua_paragrafo_precedente"] = _as_bool(raw.get("continua_paragrafo_precedente", False))
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
    candidates = data if isinstance(data, list) else data.get("articoli") or data.get("articles") or []
    return [art for item in candidates if (art := _normalize_article(item))]


def _last_narrative_snippet(article: dict[str, Any], max_chars: int = 240) -> str:
    for block in reversed(article.get("blocchi") or []):
        if block.get("tipo") == "paragrafo" and block.get("testo"):
            text = _one_line(block["testo"])
            return text[-max_chars:] if len(text) > max_chars else text
    full = _one_line(article.get("testo_completo") or "")
    return full[-max_chars:] if len(full) > max_chars else full


def _build_batch_prompt(page_numbers: list[int], total_pages: int, ctx: dict[str, Any] | None) -> str:
    header = f"Stai analizzando la pagina {page_numbers[0]} di {total_pages}." if len(page_numbers) == 1 else f"Stai analizzando in coppia le pagine {page_numbers[0]} e {page_numbers[1]} (su {total_pages})."
    if not ctx:
        return header
    return f"{header}\n\nCONTESTO PRECEDENTE:\n- Titolo: {ctx.get('titolo')}\n- Sezione: {ctx.get('sezione')}\n- Ultime parole: «...{ctx.get('ultime_parole')}»"


def _extract_open_context(articles: list[dict[str, Any]], prev_ctx: dict[str, Any] | None) -> dict[str, Any] | None:
    if not articles:
        return prev_ctx
    candidate = next((art for art in reversed(articles) if art.get("continua_dopo")), articles[-1])
    return {
        "titolo": candidate.get("titolo") or "",
        "sezione": candidate.get("sezione") or "",
        "continua_dopo": bool(candidate.get("continua_dopo")),
        "ultime_parole": _last_narrative_snippet(candidate),
    }


def process_pages_with_gemini(model: GeminiRestClient, images_bytes: list[bytes], page_numbers: list[int], total_pages: int, ctx: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    prompt = _build_batch_prompt(page_numbers, total_pages, ctx)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            text = model.generate_json(prompt, images_bytes)
            return parse_gemini_articles(text)
        except Exception:
            time.sleep(3.0 * attempt)
    return []


def _norm_title(title: str) -> str:
    title = title.lower().strip()
    title = re.sub(r"[\u2018\u2019`]", "'", title)
    title = re.sub(r"[\u201c\u201d]", '"', title)
    return re.sub(r"[\s:;,.…\-–—]+$", "", re.sub(r"\s+", " ", title))


def _is_continuation(article: dict[str, Any], previous: dict[str, Any] | None) -> bool:
    if previous is None:
        return False
    if article.get("continua_da_prima"):
        return True
    title = _norm_title(article["titolo"])
    prev_title = _norm_title(previous["titolo"])
    return bool(not title and not article.get("in_apertura") or title in CONTINUATION_TITLES or (prev_title and title == prev_title) or (previous.get("continua_dopo") and not article.get("in_apertura", True)))


def _merge_into(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key in TEXT_KEYS:
        if not target[key] and incoming[key]:
            target[key] = incoming[key]
    target["blocchi"].extend([dict(b) for b in incoming.get("blocchi", [])])
    target["testo_completo"] = _text_from_blocks(target["blocchi"])
    target["continua_dopo"] = bool(incoming.get("continua_dopo"))


def merge_articles(pages_articles: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    last_open: dict[str, Any] | None = None
    for articles in pages_articles:
        for article in articles:
            previous = merged[-1] if merged else None
            candidate = last_open if (last_open and _is_continuation(article, last_open)) else previous
            if _is_continuation(article, candidate):
                _merge_into(candidate, article)
                continue
            merged.append({**article, "blocchi": [dict(b) for b in article.get("blocchi", [])]})
            if article.get("continua_dopo"):
                last_open = merged[-1]
    return merged


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


def create_epub(articles: list[dict[str, Any]], output_path: Path, source_pdf_name: str, pdf_document: fitz.Document) -> Path:
    if not articles:
        raise ValueError("Nessun articolo da inserire nell'EPUB.")
    today = date.today().isoformat()
    book = epub.EpubBook()
    book.set_identifier(str(uuid.uuid4()))
    book.set_title(f"Internazionale {today}")
    book.set_language("it")
    book.add_author("Internazionale")

    if pdf_document.page_count > 0:
        cover_bytes = render_page_jpeg(pdf_document[0], dpi=150)
        cover_item = epub.EpubImage(uid="cover_image", file_name="images/cover.jpg", media_type="image/jpeg", content=cover_bytes)
        book.add_item(cover_item)
        book.set_cover("images/cover.jpg", cover_bytes)

    style = epub.EpubItem(uid="style", file_name="style.css", media_type="text/css", content=EPUB_CSS.encode("utf-8"))
    book.add_item(style)

    sections_map: dict[str, list[Any]] = {}
    spine: list[Any] = ["nav"]
    img_counter = 0

    for index, article in enumerate(articles, start=1):
        chapter_id = f"cap_{index:03d}"
        title = _one_line(article["titolo"]) or f"Articolo {index}"
        sezione = article.get("sezione") or "Attualità"
        occhiello = article.get("occhiello") or ""

        html_parts = []
        if sezione:
            html_parts.append(f'<p class="sezione">{escape(sezione)}</p>')
        if occhiello:
            html_parts.append(f'<p class="occhiello">{escape(occhiello)}</p>')
        html_parts.append(f"<h1>{escape(title)}</h1>")
        if article.get("sottotitolo"):
            html_parts.append(f"<h2>{escape(article['sottotitolo'])}</h2>")

        meta_bits = [bit for bit in (article.get("autore"), article.get("fonte"), article.get("data_originale")) if bit]
        if meta_bits:
            html_parts.append(f'<p class="meta"><i>{escape(" — ".join(meta_bits))}</i></p>')

        for block in article.get("blocchi", []):
            tipo = block["tipo"]
            if tipo == "immagine":
                img_counter += 1
                cropped = None
                try:
                    p_idx = max(0, min(pdf_document.page_count - 1, block.get("_page_index", 0)))
                    if block.get("bbox") and len(block["bbox"]) == 4:
                        cropped = crop_bbox_from_page(pdf_document[p_idx], block["bbox"])
                except Exception:
                    pass
                if cropped:
                    fname = f"images/img_{img_counter:03d}.jpg"
                    book.add_item(epub.EpubImage(uid=f"img_{img_counter:03d}", file_name=fname, media_type="image/jpeg", content=cropped))
                    html_parts.append(f'<img src="{fname}" class="articolo-img" alt="Immagine" />')
                cap = " — ".join([t for t in [block.get("didascalia"), block.get("credito")] if t])
                if cap:
                    html_parts.append(f'<p class="didascalia">{escape(cap)}</p>')
            else:
                text = escape(block.get("testo", "")).replace("\n", "<br/>")
                if tipo == "intertitolo":
                    html_parts.append(f"<h3>{text}</h3>")
                elif tipo == "citazione":
                    html_parts.append(f"<blockquote><p>{text}</p></blockquote>")
                elif tipo == "box":
                    inner = f"<h4>{escape(block.get('titolo_blocco'))}</h4>" if block.get('titolo_blocco') else ""
                    html_parts.append(f'<aside class="box">{inner}<p>{text}</p></aside>')
                elif tipo == "lista":
                    items = "".join(f"<li>{escape(i)}</li>" for i in block.get("testo", "").split("\n") if i.strip())
                    html_parts.append(f"<ul>{items}</ul>")
                elif tipo == "firma":
                    html_parts.append(f'<p class="firma"><i>{text}</i></p>')
                else:
                    if text:
                        html_parts.append(f"<p>{text}</p>")

        chapter = epub.EpubHtml(title=title[:200], file_name=f"{chapter_id}.xhtml", lang="it", uid=chapter_id)
        chapter.content = "\n".join(html_parts)
        chapter.add_item(style)
        book.add_item(chapter)
        spine.append(chapter)

        if sezione not in sections_map:
            sections_map[sezione] = []
        sections_map[sezione].append(chapter)

    book.toc = tuple((epub.Section(sec), tuple(chaps)) for sec, chaps in sections_map.items())
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(output_path), book, {"epub3_pages": False})
    return output_path


def extract_articles_from_pdf(pdf_path: Path, api_key: str, progress_callback=None) -> tuple[list[dict[str, Any]], fitz.Document]:
    model = build_gemini_model(api_key)
    document = fitz.open(pdf_path)
    total = document.page_count
    pages_articles = []
    open_context = None

    for start_idx in range(0, total, PAGES_PER_BATCH):
        batch_indices = list(range(start_idx, min(start_idx + PAGES_PER_BATCH, total)))
        page_numbers = [idx + 1 for idx in batch_indices]
        images_bytes = [render_page_jpeg(document[idx]) for idx in batch_indices]
        articles = process_pages_with_gemini(model, images_bytes, page_numbers, total, open_context)
        
        for a in articles:
            for b in a.get("blocchi", []):
                if b["tipo"] == "immagine":
                    p_rel = b.get("pagina", 1)
                    b["_page_index"] = batch_indices[0] if p_rel == 1 else batch_indices[-1]
                    
        pages_articles.append(articles)
        open_context = _extract_open_context(articles, open_context)
        if progress_callback:
            progress_callback(page_numbers[-1], total)

    merged = merge_articles(pages_articles)
    return merged, document
