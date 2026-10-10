<!-- Estratto da README.md (righe 956-1075). Torna al [README](../README.md). -->

## 🔧 Come funziona

1. **OCR** — Ogni pagina PDF → immagine (DPI 300) → Tesseract.
2. **Trascrizione** — Audio → Whisper (faster-whisper) con timestamp al decimo di secondo, **decodifica a batch** e beam 1 (~2.3× più veloce a parità di ancore: vedi sopra). Stopword rimosse, parole di transizione ("passiamo", "slide", "blocco"...) preservate.
3. **Auto-detection** — Scansione trascrizione per decidere il flusso (`slide-audio` o `audio-slide`).
4. **Ancore "slide N"** — Riferimenti espliciti → ancore deterministiche ad alta precisione. Riconosce numeri in cifre (*"slide 3"*), cardinali (*"slide tre"*, *"numero due"*), **ordinali** (*"la terza diapositiva"*, *"la quinta slide"*) in entrambi i generi, con articolo o "numero" in mezzo, e varianti fonetiche di trascrizione (*"nonna slide"* → slide 9, *"sla e due"* → slide 2, *"asl cinque"* → slide 5, *"sallay 2"* / *"slaib6"* → slide 2/6 con numero incorporato).
5. **Verifica mapping ancore** — Se la numerazione parlata è sfasata rispetto al PDF (es. copertina esclusa: lo speaker dice "slide 1" mostrando la slide 2), corregge il numero di slide delle ancore mantenendone i tempi esatti. Prima l'**euristica deterministica** (embeddings locali, offline, sempre attiva): rileva un offset sistematico coerente su tutte le ancore e lo applica senza 9Router. Fallback **LLM** se l'offset non è sistematico: legge il contenuto del parlato dopo ogni "slide N" e decide il numero reale di slide. Tempi sempre rispettati, mai spostati.
   **Guardia sulle derive** (entrambi i motori, anche sulle cache): si applicano solo le derive *coerenti* della numerazione, cioè run contigue di **almeno 2 ancore sfasate dello stesso delta** (copertina esclusa, slide del PDF saltata a metà narrazione). I **rimappi isolati** vengono rifiutati: subito dopo un annuncio il parlato è già quello della slide *successiva*, quindi ogni test di contenuto la preferisce alla slide annunciata (tipico delle copertine/diapositive di transizione, il cui OCR non può confermarle). La **"slide 1" parlata non è mai rimappabile**: la slide 1 reale è sempre a 0.0s e il suo riferimento non è un confine di transizione.
6. **Sincronizzazione semantica** — Embedding (e5-large via fastembed, offline ONNX) + programmazione dinamica monotona. Assegna ogni blocco audio alla slide semanticamente più vicina, con competizione softmax (temperatura 0.15).
7. **Anti-flicker** — Garanzia di durata minima (default `max(8s, 2×--semantic-min-duration)`) su ogni slide della timeline ordinata: le slide troppo corte (tipiche di quelle senza ancora, posizionate a ridosso dell'ancora successiva) vengono allungate spostando **solo i confini non ancorati**. Le ancore "slide N" pronunciate restano esatte al decimo di secondo; se nessun confine è spostabile la slide resta corta e viene segnalata, senza inventare posizioni.
8. **Riconciliazione** — Validazione: tempi crescenti, durate positive, ultima slide entro fine audio. Se invalida → interruzione.
9. **Video** — Slide ridimensionate a 1080p, assemblate con MoviePy (fps=5, buffer 3.0s anti-troncamento).
10. **Pulizia** — File temporanei e cache orfana rimossi automaticamente.

### Come lavora il codice per scenario

Le fasi 1-3 (OCR, trascrizione, auto-detection) sono comuni: da lì la
pipeline prende una strada diversa a seconda del segnale presente nell'audio.

| Scenario | Segnale | Cosa fa |
|---|---|---|
| **A. `slide-audio`** | "Passiamo alla slide 3" | Estratte ancore deterministiche "slide N" (cifre, cardinali o **ordinali**: "la terza diapositiva") → vincoli ad alta precisione. Verifica mapping ancore: se la numerazione parlata è sfasata rispetto al PDF, l'**euristica deterministica** (embeddings locali, offline) corregge gli offset sistematici subito, senza 9Router; fallback LLM se l'offset non è sistematico. Sincronizzazione semantica (embedding e5-large offline): ogni blocco audio → slide più vicina, DP monotona. Con `--llm` attivo e slide SENZA ancora → **flusso ibrido**: il **motore embedding locale gira sempre per primo** (pochi secondi, embedding in cache) e le ancore restano esatte; l'**LLM** (9Router, che si avvia da solo se spento) interviene solo come *escalation* per posizionare meglio le slide senza ancora, cioè oltre `--llm-local-threshold` slide mancanti (default 2) o quando il motore stesso dichiara il segnale debole. Se l'LLM non produce una timeline coerente si **riusa quella locale già calcolata** (nessun ricalcolo). Riconciliazione (tempi crescenti, durate positive); se impossibile → **interruzione**. Slide in ordine 1→N. Un log diagnostico distingue riferimenti trovati/usati/scartati e segnala le slide senza ancora esplicita. |
| **B. `audio-slide`** | "Passiamo al blocco successivo" | Stesse ancore (numeriche o ordinali), stessa pipeline ordinata (+ flusso ibrido LLM come in A); la slide cambia sulle transizioni di blocco non numerate. |
| **C. `free`** | nessuno | Riordino libero: la slide segue il contenuto del podcast, anche ripetuta, durata minima ~8s (anti-flicker). **Con LLM** (`--llm auto`): chunk 30s inviati a 9Router (combo `comboact` → Mistral 24B → Gemma 31B); se 9Router è spento il processo si mette in pausa con avviso e riprende da solo appena torna online (o premi `S` / `--llm-wait-timeout` per il fallback embedding; senza terminale interattivo si interrompe con errore chiaro). **Senza LLM** (`--llm off`): solo embedding locale in modalità libera. `--llm-review` ri-verifica e avvisa senza modificare la timeline. |

Il raggruppamento in finestre temporali è condiviso da `chunks.py`
(`build_windows`): finestre corte (4s) per la semantica, larghe (30s) per
l'LLM; ogni motore filtra e formatta a modo suo.

**In tutte le strade**: timeline → durate → MoviePy (slide 1080p, fps 5,
buffer 3s) → `video_finale.mp4`, con pulizia finale della cache orfana.
Se il segnale è insufficiente il programma **si interrompe con avviso**,
mai inventando distribuzioni uniformi.

### 🧠 Che cos'è un embedding (in parole semplici)

Un **embedding** è la "traduzione" di un testo in una lista di numeri che
ne cattura il **significato**. L'idea chiave: **testi che vogliono dire cose
simili hanno liste di numeri simili**.

- "il ciclo dell'acqua" e "come l'acqua evapora e poi piove" → numeri **vicini**
- "il ciclo dell'acqua" e "la guerra dei cent'anni" → numeri **lontani**

La pipeline usa questa tecnica per sincronizzare: trasforma ogni slide e ogni
pezzo di parlato in numeri, poi mostra ogni slide nel momento in cui il parlato
ha i numeri più simili. È un modello **offline** (gira sul tuo PC, nessuna
connessione) e **senza IA online**: il "cervello" che decide quando cambiare
slide è tutto locale.

Esempio: se nella trascrizione a 3 minuti si parla di "riciclaggio della
plastica" e una slide parla di "riciclo dei rifiuti", il modello riconosce che
sono simili e mostra quella slide in quel momento.

> **Nota sulla fiducia**: il riepilogo finale chiude sempre con la fiducia
> misurata del motore, es. `Fiducia del motore: alta (picco medio 0.73 su
> soglia 0.45, cosine 0.83)`. Se il parlato non segue chiaramente l'ordine
> delle slide (es. le slide si somigliano molto tra loro) il valore scende
> sotto la soglia e il riepilogo avvisa che la sincronizzazione è **stimata,
> non garantita**, elencando cosa controllare a mano. Per un allineamento
> certo, fai pronunciare le ancore esplicite "slide N" (vedi i workflow qui
> sotto).
>
> **Il frame check ha la precedenza sulla fiducia del motore.** Se
> `--verify-video` ha controllato *tutti* i segmenti e non ha trovato
> mismatch, l'artefatto è verificato e l'avviso "da controllare a mano" non
> viene aggiunto: sarebbe chiedere di controllare un video già guardato. Il
> riepilogo lo dichiara comunque, perché le durate restano stimate.
>
> Attenzione: `avg_z` è un proxy, e diventa pessimista quando le slide sono
> **poche e lunghe** (misurato: 8 slide su 30 minuti, ~226 s per slide →
> `avg_z` 0.38 con frame check 8/8). Lo z-score di una slide è normalizzato su
> tutta la linea temporale: più la slide è "a casa sua" a lungo, più il
> parlato delle sezioni vicine le somiglia e il segnale di picchio si diluisce.
> Per questo la soglia 0.45 non va ricalibrata sulla sola base di questi
> casi: è il frame check a decidere, non il motore.

### Modello embedding

- **Default (definitivo)**: `intfloat/multilingual-e5-large` (1024 dim, ~2.2 GB) — scelto tramite test A/B su podcast reale (10 slide, senza LLM): similarità media 0.791 vs 0.380 (MiniLM) e 0.421 (mpnet), con durate tutte bilanciate (104-188s) e zero slide anomale (gli altri producevano slide da 8-20s e da 332s).
- **Fallback**: `paraphrase-multilingual-mpnet-base-v2` (768 dim, ~1.0 GB) — usato automaticamente se e5-large non si carica. Tenere in cache: è la rete di sicurezza della pipeline.
- **Cache**: `.cache/embedding_model/` è condivisa e i modelli si scaricano al primo utilizzo (serve internet una tantum). MiniLM (240 MB) e mpnet (1.0 GB) restano in cache anche se non più default: non disturbano, e per tornare al vecchio comportamento basta un override temporaneo:
  ```powershell
  $env:EMBEDDING_MODEL="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
  python main.py
  ```
- **Ambiente**: `EMBEDDING_MODEL` e `EMBEDDING_MODEL_ALTERNATE` sovrascrivibili permanentemente.

### Quale prompt usare

Un solo processo di input: **presentazione → podcast**. Il podcast deve
pronunciare le ancore "slide N" **in cifre** a ogni sezione: senza ancore il
pipeline non può sapere dove cambia la slide e passa al flusso libero (che usa
l'LLM — un avviso in console lo segnala).

Il prompt [`PROMPT_PRESENTAZIONE (PREDEFINITO).md`](../<PROMPT_PRESENTAZIONE (PREDEFINITO).md>)
guida sia la generazione della presentazione (Studio → Slide Deck, dalle tue
fonti) sia il podcast che la segue nell'ordine, con la presentazione come
UNICA fonte. Ancore strette: cifre, "slide" chiara, mai "la slide successiva",
recupero salti.

**Procedura (Presentazione → Podcast):**

1. Genera la **presentazione** con NotebookLM (Studio → Slide Deck) usando il prompt dedicato nel file, e mettila nelle fonti come **UNICA fonte (rimuovi le altre)**.
2. Genera il **podcast** con il prompt del file: segue l'ordine della presentazione, con le ancore.
3. Verifica le ancore prima di lanciare il pipeline:
   ```bash
   grep -c "slide" transcript_raw.txt   # deve essere ≥ N-1 (una per transizione)
   ```
   Se è 0 il prompt non è stato seguito: rigenera il podcast.
4. `python main.py` → auto-detection `slide-audio`, ancore deterministiche, nessuna chiamata a 9Router (o `--llm off` per escluderlo del tutto).

**Se il podcast non pronuncia le ancore** (dibattito libero): il pipeline
avvisa e usa il flusso libero (LLM via 9Router). Fallback senza LLM:
`python main.py --flow slide-audio --llm off` (allineamento monotono embedding,
meno preciso senza ancore ma deterministico).

Risultato su test reali (NotebookLM, flusso A con presentazione come UNICA fonte): 11/11, 13/14 e 12/13 ancore, durate 37s–2m05s senza pavimenti anti-flicker, fiducia del motore alta (picco 0.53–0.74 su soglia 0.45) e frame-check pieno. Sincronizzazione misurata, non stimata.

---

