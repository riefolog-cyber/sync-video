<!-- Estratto da README.md (righe 877-955). Torna al [README](../README.md). -->

## 📁 Struttura progetto

```
main.py                  ← Orchestratore (auto-detection, cache, timing)
config.py                ← Bootstrap auto-dipendenze + costanti + CLI
chunks.py                ← Finestre temporali condivise (semantic_sync + llm_sync)
ocr.py                   ← Fase 1: PDF → immagini → OCR
transcription.py         ← Fase 2: Audio → Whisper → trascrizione
timeline.py              ← Ancore "slide N" + riconciliazione timeline
semantic_sync.py         ← Sincronizzazione semantica (embeddings, DP)
llm_sync.py              ← Selezione slide con LLM (9Router) + cache
embedder.py              ← Interfaccia embedding (Protocol `Embedder`, backend fastembed)
video.py                 ← Fase 4: Assemblaggio MP4 (1080p)
pipeline_cache.py        ← Hash/caricamento/salvataggio cache (estratto da main)
pipeline_report.py       ← Formattazione tempi + report (estratto da main)
cache_maintenance.py     ← `--cache-du` / `--clean-cache` (peso e pulizia sicura)
test_sync.py             ← Suite di test unitari
test_llm_sync.py         ← Test modulo LLM
test_chunks.py           ← Test finestre temporali condivise
test_prompts.py          ← Test dei prompt e dei link del README
test_integration.py      ← Test di integrazione
genera_video.bat         ← Launcher 1-click (Windows)
crea_venv.bat            ← Crea .venv (Windows)
crea_venv.sh             ← Crea .venv (macOS/Linux)
requirements.txt         ← Dipendenze pip
ruff.toml                ← Configurazione lint (guardrail di stile)
mypy.ini                 ← Configurazione type-check
PROMPT_PRESENTAZIONE (PREDEFINITO).md ← Prompt NotebookLM: presentazione → podcast (flusso A, consigliato)
PROMPT_PODCAST.md            ← Prompt NotebookLM: podcast → presentazione (flusso B)
tessdata/                ← Modelli lingua Tesseract portatili
9router-maintenance/     ← Script manutenzione combo `comboact` di 9Router (vedi sotto)
docs/                    ← Documentazione (queste pagine) + diagramma architettura
sync-video-architecture (in docs/) ← Diagramma architettura (generato con archify)
```

### 🛠️ Sviluppo

Comandi verificati per chi modifica il codice:

```bash
# Test (suite completa, unittest — 315 test)
python -m unittest discover -s . -p "test_*.py"

# Type-check (mypy, 16 moduli sorgente; i test sono esclusi)
python -m mypy .

# Lint (ruff — pulito)
python -m ruff check .

# Lint + autofix
python -m ruff check . --fix
```

Il `ruff.toml` esclude le metriche di complessità (PLR09xx, PLC0415) perché
rappresentano il backlog di refactoring, non guardrail di stile: il check
default resta verde. Le 6 segnalazioni `BLE001` sono i `try/except Exception`
volutamente ampi (fallback robusti: LLM irraggiungibile, cache corrotta,
embedding fallito) e non vanno "stretti" senza motivo.

### File generati (temporanei, auto-puliti)

| File | Descrizione |
|---|---|
| `video_finale.mp4` | Output video |
| `transcript_raw.txt` | Trascrizione completa (debug) |
| `temp_slides/` | Slide renderizzate |
| `.cache/embedding_cache/` | Vettori embedding content-addressed (`.npz`, max 40 voci) |
| `.cache/` | Cache OCR, trascrizione, embedding |

La cache della trascrizione è indicizzata da **tutto ciò che cambia il testo
prodotto** — audio, lingua, modello, motore risolto (`auto` può essere OpenVINO
o faster-whisper) e i parametri del motore (`..._small_whisper_cpu_int8_beam1_batch8.json`).
Conseguenza pratica: cambiando `--whisper-beam`, `--whisper-batch`, il compute
type o il device la trascrizione viene **rifatta**, invece di riusare in
silenzio un testo prodotto con altre impostazioni. Al primo avvio dopo un
aggiornamento che cambia questi parametri, quindi, la trascrizione riparte da
zero una volta sola.

Anche la cache degli embedding è indicizzata dal **contenuto** (testi + identità
del modello), non dalla sessione: ripetere la stessa run riusa i vettori (0s di
embedding), cambiando `--semantic-model` vengono ricalcolati.

---

