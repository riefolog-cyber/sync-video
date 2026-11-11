# Piano di ottimizzazione — chiuso

Il lavoro di pulizia/ottimizzazione di `sync-video` è finito: tutte le voci
proposte sono state fatte o hanno una risposta esplicita. Questo file resta
come registro delle **decisioni**, non delle operazioni — i dettagli di ogni
cambiamento sono nei messaggi di commit e nei commenti del codice.

Ultimo aggiornamento: batch 5 (audit completo del codice: 6 difetti corretti e
7 ottimizzazioni misurate), dopo la chiusura del piano (P2 #11 archiviata come
non consigliata, primo avvio automatico di Python su Windows).

## Batch 5 — audit completo (2026-11)

Un audit del codice letto riga per riga (con misure sui dati reali in
`.cache/`) ha trovato 6 difetti e 7 ottimizzazioni. Le correzioni hanno un
test che le copre; le ottimizzazioni sono misurate sul run di riferimento
(OCR 8 s / trascrizione 207 s / sync 80 s / video 88 s).

**Difetti corretti**

- Il controllo sulla **prima sezione** era codice morto: leggeva il verdetto con
  la chiave sbagliata (`slide_ids[0]` invece della posizione 0), quindi non
  parlava mai, nemmeno con `--strict-sync`. Ora c'è `_first_section_verdict`.
- `timeline_finale.json`: se la scrittura falliva, il file **vecchio** restava
  e `analysis_sync.py` lo leggeva come timeline di quella run (falsi mismatch).
  Ora il file vecchio viene rimosso e la run avvisa.
- `sync_report.json` descriveva i segmenti **prima** di `--skip-slides` e
  della riparazione automatica: `--review` tagliava i frame sui confini
  sbagliati. Ora i segmenti vengono ricostruiti sulla sequenza finale.
- `_extract_frame`: un ffmpeg fallito che lasciava un file parziale passava
  come frame valido. Ora si controlla il codice di uscita e il file parziale
  viene rimosso.
- Il **refine** saltava un confine in silenzio se l'embedder restituiva una
  forma inattesa: ora lo registra nel referto.
- **OCR**: con la lingua mancante ogni pagina falliva 3 volte con backoff
  (~30 s su 40 slide) e il referto finale diceva "presentazione solo
  immagini?". Ora un preflight solo, e il messaggio giusto.

**Ottimizzazioni**

- Le finestre di parlato delle ancore venivano embeddate **una per ancora**,
  fuori dalla cache: un solo batch, in cache (riuso fra run).
- La cache degli embedding è **per lista**: bastava una parola cambiata per
  rifare 386 embedding (~15 s). Ora il riuso è **per testo** (stesso testo +
  stesso modello = stesso vettore): si ricalcolano solo i blocchi nuovi.
- La potatura della cache era un LRU su mtime **senza aggiornare l'mtime sui
  colpimenti**: le voci più utili venivano potate per prime. Ora il tocco
  all'uso.
- `_letterbox_references` decodificava **tutte** le slide insieme (>4 GB su un
  deck da 150 pagine): ora due passate, una slide alla volta.
- La decodifica audio teneva **tre copie** del PCM (~240 MB su 20 minuti,
  ~1.4 GB su due ore): ora una sola, e pipe/processo chiusi sempre.
- Il punteggio del refine passava da ~2×48 dot product scalari a un prodotto
  matriciale.

**Aperto (deciso, non fatto)**

- L'embedding dei blocchi si rifà anche quando tutte le transizioni sono
  ancorate (36 s su 79 s di sync): la timeline la decidono le ancore, ma i
  vettori servono anche alla metrica di qualità, che ha soglie validate.
  Cambiarla è una decisione, non un refactor.
- Le **zone cieche** vengono ritrascritte a ogni run (~25 s l'una): manca una
  cache "negativa" delle zone già tentate.
- La verifica del video estrae **un ffmpeg per segmento**: si può fare un
  invoco solo per tutti i frame.
- La chiave della cache trascrizione cambia formato quando cambia il percorso
  di decodifica: la cache valida viene potata e la run rifa 3 min di Whisper.
- Due run sulla stessa cartella si pestano (slide, cache, frame di verifica):
  manca un blocco per le run concorrenti.


## Stato delle voci

| # | Voce | Stato |
|---|------|-------|
| P0 #1 | Guard `t.ident is not None` in `main.py` | ✅ `3914214` |
| P0 #2 | File fantasma `Audio` rimosso | ✅ (non era mai tracciato in git) |
| P0 #3 | `sync-video-architecture.html` in `docs/` | ✅ `3914214` |
| P0 #4 | Modifiche in sospeso committate | ✅ `3914214` |
| P1 #5 | `--cache-du` / `--clean-cache` sicuri | ✅ `e3ff04c` |
| P1 #6 | Auto-tuning dei thread | ✅ `29e55b8`, probe core fisici riparato |
| P1 #7 | Split del README in `docs/` | ✅ fatto, default corretti nelle pagine |
| P1 #8 | CI: cache pip/HF + nightly full | ✅ `630aa75` |
| P2 #9 | Moduli foglia `pipeline_cache` / `pipeline_report` | ✅ `2e35405` + `test_pipeline_cache.py` |
| P2 #10 | Protocol `Embedder` + `load_embedder()` | ✅ pronto per un backend alternativo |
| P2 #11 | `llm_sync.py` fuori dal percorso di default | ✅ **rimosso del tutto** (con 9Router, modello LLM e revisione LLM) |
| P2 #12 | Typing graduale dei test (deny-list mypy) | ✅ |
| P2 #13 | Riparato `_physical_cpus()` (era morto in silenzio) | ✅ whisper 20→14 thread |
| P2 #14 | Adattamento a ogni PC: RAM, encoder GPU, batch | ✅ |
| P2 #15 | `main.py --help` rotto da un `%` non escapato | ✅ |
| P2 #16 | Rilevamento GPU su Linux a cascata | ✅ |
| #17 | Primo avvio automatico anche di Python (Windows) | ✅ |
| #18 | Controllo dello spazio disco prima del primo download | ✅ |
| #19 | Avviso se i modelli di default sono pesanti per il PC | ✅ |
| #20 | Test che le tabelle delle doc corrispondano al codice | ✅ |

## Decisioni da non riaprire

**P2 #11 — `llm_sync.py`: rimosso del tutto.**
La selezione via LLM (9Router online, modello `comboact`, revisione `--llm-review`)
non era mai usata nel percorso reale: `--llm` aveva già `default="off"` e il
percorso senza ancore che la richiedeva è stato a sua volta rimosso. Tenere il modulo
significava trascinare `requests`, una cascata di endpoint e ~2000 righe di test
per un percorso spento. Rimosso insieme a `9router-maintenance/`,
`aggiornamenti_9router.bat` e `docs/9router.md`.

**Selezione libera `free` e secondo prompt (`PROMPT_PODCAST.md`): rimossi.**
Il flusso libero allineava le slide per contenuto senza ancore, con
l'anti-flicker interno al motore, e il prompt "podcast → presentazione" lo
alimentava. Non era più usato e non era verificabile (nessuna ancora da
contare). Il caso senza ancore, però, oggi non è più un errore: il flusso viene
**assunto** (`slide-audio`) e la timeline è stimata per contenuto dal motore
embedding, con avviso nel log e nel report (`--require-flow` ripristina
l'interruzione). Rimossi quindi il ramo `free`, gli argomenti
`--flow free`/`--no-free-ordered-fallback`, `PROMPT_PODCAST.md` e i relativi
riferimenti. Le due funzioni di gate che distinguevano il flusso rilevato da
quello effettivo (`_anchor_gate_applies`, il ramo libero di `_thin_slide_advice`)
se ne sono andate con il fallback che le rendeva necessarie.

**Split fisico di `main.py` / `semantic_sync.py`: non eseguito.**
`test_sync.py` importa molte funzioni private da `main`. Spezzare romperebbe
centinaia di test per un beneficio non visibile all'utente. A molte dipendenze
un'estrazione a shim, come quella di P2 #9, sarebbe plausibile: resta rimandata
per una ragione di merito (beneficio incerto), non per una barriera tecnica.

**Downgrade automatico del modello su RAM bassa: non eseguito di proposito.**
Il batching, i thread e l'encoder si adattano alla RAM; i *pesi* no. Su un
PC piccolo il programma **avvisa** (#19) e dice cosa mettere in `.env`, ma non
abbassa il modello di nascosto: un risultato peggiore che nessuno ha scelto è
peggio di un avviso, e i confronti con la baseline (che usa `small`)
perderebbero significato.

**`psutil` non è in `requirements.txt`.** Tutti i probe hardware funzionano
senza: il progetto usa la libreria standard (`ctypes` su Windows,
`/proc/cpuinfo` su Linux, `sysctl` su macOS). Il giorno in cui i probe senza
dipendenze non bastassero, `psutil` è il passo successivo — ma allora va
dichiarato, perché un probe che si appoggia a una libreria non installata
fallisce in silenzio (è già successo: vedi P2 #13).

## Come verificare

Tutto in un comando, con un solo esito finale: `controlli.bat` (Windows) o
`python src/controlli.py`. Esegue i passi qui sotto, piu' il type-check sulle tre
piattaforme e `_debug_hardware.py`. Se preferisci vederli uno per uno:

```powershell
.\.venv\Scripts\python.exe -m ruff check src
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m unittest discover -s src -p 'test_*.py'
.\.venv\Scripts\python.exe src\_debug_hardware.py
```

**`mypy .` da solo non basta.** La CI gira su `ubuntu-latest`, quindi il
type-check va fatto anche per le altre piattaforme:

```powershell
.\.venv\Scripts\python.exe -m mypy --platform linux .
.\.venv\Scripts\python.exe -m mypy --platform darwin .
```

È già dentro `_debug_hardware.py` (controllo 6). Non è pignoleria: un
`if sys.platform == "win32"` attorno a un import passa in locale su Windows
e fa fallire la CI su tutte e tre le versioni di Python, perché mypy
valuta la condizione e sul ramo non-Windows il nome risulta non definito.
È successo, e i test in CI non erano nemmeno arrivati a girare.

Stato all'ultimo lancio: ruff pulito, mypy pulito, test verdi,
`_debug_hardware.py` 16/16.