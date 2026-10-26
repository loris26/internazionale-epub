import streamlit as st
from pathlib import Path
import tempfile
import os
import logging
from convertitore import extract_articles_from_pdf, create_epub

st.set_page_config(page_title="Internazionale to EPUB", page_icon="📚", layout="centered")

# --- CLASSE PER CATTURARE I LOG E MOSTRARLI IN STREAMLIT ---
class StreamlitLogHandler(logging.Handler):
    def __init__(self, placeholder):
        super().__init__()
        self.placeholder = placeholder
        self.logs = []

    def emit(self, record):
        # Aggiunge il log e mantiene solo le ultime 20 righe per non appesantire l'interfaccia
        msg = self.format(record)
        self.logs.append(msg)
        if len(self.logs) > 20:
            self.logs.pop(0)
        self.placeholder.code("\n".join(self.logs), language="text")

st.title("📚 Convertitore Internazionale in EPUB")
st.markdown("Carica il PDF della rivista *Internazionale* per generare un EPUB strutturato.")

try:
    default_key = st.secrets.get("GEMINI_API_KEY", os.getenv("GEMINI_API_KEY", ""))
except FileNotFoundError:
    default_key = os.getenv("GEMINI_API_KEY", "")
    
api_key = st.text_input("Inserisci la tua Gemini API Key", type="password", value=default_key)

uploaded_file = st.file_uploader("Scegli il file PDF di Internazionale", type=["pdf"])

if uploaded_file is not None:
    if not api_key:
        st.warning("⚠️ Per favore, inserisci la Gemini API Key per procedere.")
    else:
        if st.button("🚀 Avvia Conversione"):
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            st.markdown("### 🖥️ Log di processo (Tempo reale)")
            log_placeholder = st.empty()
            
            # Attacchiamo il nostro Log Handler al logger di 'convertitore'
            log = logging.getLogger("convertitore")
            log.setLevel(logging.INFO)
            st_handler = StreamlitLogHandler(log_placeholder)
            st_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S"))
            log.addHandler(st_handler)

            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
                tmp_file.write(uploaded_file.read())
                tmp_pdf_path = Path(tmp_file.name)

            output_epub_path = tmp_pdf_path.with_suffix(".epub")

            def update_progress(current, total):
                progress = min(1.0, current / total)
                progress_bar.progress(progress)
                status_text.info(f"Analisi pagine in corso: {current} di {total}...")

            try:
                status_text.info("Connessione a Gemini e analisi del PDF in corso...")
                articles, document = extract_articles_from_pdf(tmp_pdf_path, api_key, progress_callback=update_progress)
                
                if articles:
                    status_text.info("Creazione dell'eBook EPUB e ritaglio immagini...")
                    create_epub(articles, output_epub_path, tmp_pdf_path, document)
                    document.close()
                    
                    progress_bar.progress(1.0)
                    status_text.success(f"✅ Conversione completata! Estratti {len(articles)} articoli in totale.")
                    
                    with open(output_epub_path, "rb") as f:
                        st.download_button(
                            label="📥 Scarica EPUB Generato",
                            data=f,
                            file_name=f"Internazionale_{Path(uploaded_file.name).stem}.epub",
                            mime="application/epub+zip",
                            use_container_width=True
                        )
                else:
                    st.error("❌ Nessun articolo estratto dal PDF.")
            except Exception as e:
                st.error(f"❌ Errore durante la conversione: {e}")
            finally:
                if tmp_pdf_path.exists():
                    tmp_pdf_path.unlink()
                # Rimuoviamo l'handler a fine processo per evitare duplicazioni se si clicca di nuovo
                log.removeHandler(st_handler)
