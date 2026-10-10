# Piano di ottimizzazione — stato e avanzamento

Documento vivo del lavoro di pulizia/ottimizzazione del progetto `sync-video`.
Ogni voce ha uno stato: ✅ fatto e committato, 🚧 in corso, ⬜ proposto.

Ultimo aggiornamento: P2 #14 (adattamento a ogni PC: RAM, encoder GPU,
batch, avviso lentezza), P2 #16 (rilevamento GPU su Linux a cascata),
P2 #15 (`--help` rotto), P2 #13 (probe core fisici: whisper 20→14 thread),
P2 #12 (deny-list mypy), P2 #9 (debito di test chiuso), #7 (split README
fatto e corretto); conteggi del piano corretti contro il codice reale.

---

## P0 — Correttezza e ordine (quick win)

| # | Voce | Stato |
|---|------|-------|
| 1 | Fix mypy: guard `t.ident is not None` in `main.py` (thread-exit helpers) | ✅ commit `3914214` |
| 2 | Rimuovere file fantasma `Audio` (0 byte, confondeva `find_audio_file`) | ✅ fatto (file non tracciato, vedi nota) |
| 3 | Spostare `sync-video-architecture.html` in `docs/` | ✅ commit `3914214` |
| 4 | Commit delle modifiche in sospeso | ✅ commit `3914214` |

**Nota #2 — il file `Audio`:** rimosso dalla working tree, ma **non è
`3914214` ad averlo cancellato*: il file non era mai tracciato in git
(`git log --all -- Audio` è vuoto) ed è in `.gitignore`. Il commit
`3914214` contiene solo il guard mypy, il rename in `docs/` e la
modifica a `test_prompts.py`. La rimozione è quindi irreversibile
solo nel senso che il file non era tracciato: nessun `git checkout`
potrebbe riportarlo, e nessun clone lo vedrà mai.

---

## P1 — Ottimizzazione e costi

| # | Voce | Stato |
|---|------|-------|
| 5 | `--cache-du` / `--clean-cache` sicuri + peso `.cache` nel riepilogo | ✅ commit `e3ff04c` |
| 6 | Auto-tuning adattivo dei thread (`WHISPER/EMBED/VIDEO_THREADS`) + riga riepilogo | ✅ commit `29e55b8`, **probe core fisici riparato** (vedi P2 #13) |
| 7 | Split README (1090 righe) in `docs/` | ✅ fatto, con correzioni (vedi nota) |
| 8 | CI: cache pip/HF + nightly full via `SYNC_VIDEO_RUN_SLOW` | ✅ commit `630aa75` |

**Nota #7 — README:** la voce era rimandata perché il primo tentativo di split
automatico era stato annullato (`git checkout README.md`): spezzare la
narrativa costa più di quanto rende. Lo split è poi stato fatto a mano — il
README resta il documento di avvio rapido (14 righe di indice in più), il
resto è in `docs/`.

Lo split è stato **corretto subito**, perché la prima stesione delle pagine
riportava il default dei thread come `min(8, cpu_count())` "uguale per tutti i
tre" — cioè esattamente il numero che due giorni prima era stato corretto per
il probe dei core fisici. Un documento che contraddice il codice è peggio di
nessun documento: `docs/comandi.md` e `docs/setup.md` ora riportano i default
verificati contro il codice (whisper senza tetto, embed/video `min(12, core
fisici)`, OCR `min(6, …)`, `--llm` `off`) e la nuova sezione su cosa viene
adattato al PC.

---

## P2 — Architettura e manutenibilità

| # | Voce | Stato |
|---|------|-------|
| 9 | Estrarre moduli "foglia" da `main.py`: `pipeline_cache.py`, `pipeline_report.py` | ✅ commit `2e35405` (718 test verdi) + `test_pipeline_cache.py` |
| 10 | Astrarre l'embedding-backend: Protocol `Embedder` + `load_embedder()` | ✅ fatto (vedi header) |
| 11 | `llm_sync.py` come percorso sperimentale (fuori da bootstrap/test default) | ⬜ proposto |
| 12 | Typing graduale dei test (`mypy` con deny-list dei file storici) | ✅ fatto (vedi header) |
| 13 | Riparare `_physical_cpus()` (l'auto-tuning thread era morto in silenzio) | ✅ fatto (vedi sotto) |
| 14 | Adattamento a ogni PC: RAM, encoder GPU, batch, avviso lentezza | ✅ fatto (vedi sotto) |
| 15 | `main.py --help` rotto da un `%` non escapato (bug del 07/10) | ✅ fatto (vedi sotto) |
| 16 | Rilevamento GPU su Linux a cascata (`lspci` → `nvidia-smi` → sysfs) | ✅ fatto (vedi sotto) |

### P2 #16 — Dettaglio (fatto): GPU non rilevata su Linux
`lspci` sta nel pacchetto `pciutils`, che **non è installato di default** su
molte distro (Debian/Ubuntu server, Alpine, container minimali). `_run`
ingoia l'eccezione, la lista GPU resta vuota, e una macchina **NVIDIA finiva
su faster-whisper CPU senza dirlo**: 5-10× più lenta, in silenzio.

Cascata a tre fonti: `lspci` (nomi ricchi) → `nvidia-smi -L` (segnale
affidabile da solo) → `/sys/class/drm/card*/device/uevent` (funziona senza
installare nulla, dà solo il vendor). Se non c'è nessuna fonte, la lista
resta vuota **e non si inventa nulla**: vuoto = ripiego su CPU, che è la
scelta conservativa giusta quando non si sa.

Un bug è stato scoperto dai test in fase di scrittura: `PCI_ID` nel sysfs è
in **maiuscolo** (`0x10DE`) mentre la tabella dei vendor era in minuscolo, e
senza il `lower()` una GPU NVIDIA restava `unknown` — cioè il fallback che
doveva chiudere il buco non lo chiudeva. Test: `test_gpu_linux.py` (8 test),
che verifica anche che le stringhe sysfs passino `_classify_gpu` come quelle
di `lspci`.

### P2 #14 — Dettaglio (fatto): adattamento a ogni PC
Il progetto adattava motore GPU e thread, ma **ignorava la RAM** e
codificava **sempre il video su CPU** con libx264, anche sulle macchine con
la GPU che il progetto stesso rileva e usa per la trascrizione.

Nuovo modulo `hardware.py` (solo libreria standard, nessuna dipendenza
aggiunta, nessun import del progetto: puo' essere importato da `config`
in fase di import senza cicli).

| Cosa | Prima | Adesso |
|---|---|---|
| Encoding video | `libx264` (CPU) sempre | NVENC / QSV / AMF / VideoToolbox se la GPU c'è, con ripiego automatico su libx264 |
| RAM | mai rilevata | `GlobalMemoryStatusEx` / `/proc/meminfo` / `sysctl hw.memsize` |
| Batch embedding | 64 fisso | 16 sotto 6 GB, 32 sotto 12 GB, 64 oltre |
| Batch whisper | 8 fisso | 4 sotto 6 GB, 8 oltre |
| Avviso "trascrizione lenta" | solo se OpenVINO era un upgrade (cioè sulle macchine veloci) | su **qualsiasi** macchina che sta davvero su CPU, con stima scalata sui core e azione suggerita |

**La scelta dell'encoder è piu' insidiosa di quanto sembri:** `ffmpeg
-encoders` elenca quello che è *compilato*, non quello che *funziona*.
Su una macchina AMD, `h264_qsv` risulta disponibile ma fallisce a runtime.
Ogni catena può quindi scendere solo su encoder validi per quella famiglia
(l'AMD non ripiega mai su QSV), e in più `video._build_video_ffmpeg` ritenta
una volta su libx264 se l'encoder accelerato fallisce: **un video lento è
sempre meglio di nessun video**.

Override: `--video-encoder` / `VIDEO_ENCODER`. La decisione non viene
persistita (come il motore di trascrizione: dipende da fatti che cambiano);
il probe degli encoder è cachato in `.cache/hardware.json`, registrato fra le
chiavi housekeeping per non essere cancellato da `--clean-cache`.

Verificato con `_debug_hardware.py` (15 controlli su NVIDIA/Intel/AMD/Apple/
senza GPU, batch per 3 fasce di RAM, encoding reale e ripiego a runtime) e
con `test_hardware.py` (35 test).

### P2 #15 — Dettaglio (fatto): `--help` non funzionava più
`main.py --help` crashava con `ValueError: incomplete format`: argparse
formatta le stringhe `help` con `help_text % params`, e la help di
`--min-anchor-coverage` conteneva un `100%` letterale. Introdotto dal commit
`4065d72`, mai notato perché l'errore compare **solo** lanciando `--help`,
mai usando l'argomento.

Correzione: `100%%`. Aggiunto `test_cli.py` e separato `config.build_parser()`
da `parse_args`, così un test chiama `format_help()` e intercetta il problema
prima che arrivi a un utente.

### P2 #13 — Dettaglio (fatto): il probe dei core fisici era rotto
Il #6 era marcato ✅ ma **non funzionava**: su una CPU con SMT i thread
finivano sui fratelli hyperthreading invece che sui core veri.

Causa: `_physical_cpus()` si reggeva su `psutil` (assente da
`requirements.txt` **e** dal venv) e sul fallback `wmic` (rimosso da
Windows 11 24H2). Entrambi fallendo, tornava `None`, e `auto_thread_budget`
ripiegava sui core **logici**.

Misurato su questa macchina (i7-12700H, 14 fisici / 20 logici):
`psutil` non installato, `wmic` non trovato → `_physical_cpus() = None` →
**whisper con 20 thread su 14 core**. Con il fix: `fisici = 14` →
**whisper con 14 thread**, esattamente il valore che il proget stesso
dice di aver misurato come migliore su questa CPU.

`embed`/`video` erano salvi per caso: il tetto di 12 li copriva. `whisper`
non ha tetto ed era la fase piu' costosa.

Correzione: catena di probe **senza dipendenze nuove** (nessun `psutil`
richiesto), ognuno difensivo: psutil → ctypes
`GetLogicalProcessorInformationEx` su Windows (oltre 64 logici rinuncia,
non indovina) → `/proc/cpuinfo` su Linux → `sysctl hw.physicalcpu` su
macOS → `wmic` (Windows vecchi). Costo: ~0,1 ms per chiamata.
Test: `TestPhysicalCpuProbe` in `test_auto_threads.py`.

### P2 #9 — Dettaglio (fatto)
`main.py` continua a re-esportare `_file_hash`, `_load_cache`, `_save_cache`,
`_clean_orphan_cache`, `_format_time`, `_print_timing`, `_slide_list_text` dai
nuovi moduli, così `from main import X` nei test resta valido senza modifiche.
~255 righe uscite da `main.py` (80 aggiunte, 255 rimosse), zero test rotti.

Correzione di una stima precedente: si diceva "~400 righe spostate", ma il
diff reale è `main.py | 80 + / 255 -` a fronte di `pipeline_cache.py` (+119) e
`pipeline_report.py` (+118): sono ~255 righe, non 400. `main.py` resta a
~3400 righe.

**Debito chiuso dopo l'estrazione:** `save_final_timeline`, `save_sync_report`
e la policy `KEEP_CACHE_STEMS` erano state estratte senza alcun test diretto.
Coperti da `test_pipeline_cache.py` (24 test), che fissa anche il fatto che
`pipeline_cache.KEEP_CACHE_STEMS` e `cache_maintenance.KEEP_STEMS` restino
uguali: se divergono, `--clean-cache` e la pulizia di run salvano cose diverse.

### P2 #10 — Dettaglio (fatto)
Obiettivo: poter provare un backend alternativo (hf-transformers + ONNX
quantizzato) senza riscrivere `semantic_sync.py`.

- `embedder.py`: Protocol `Embedder` (`embed()` + `embed_id`) + `_FastEmbedEmbedder`
  che wrappa i due passi di `semantic_sync` (`_load_embed_model` + `_make_embed_fn`)
  SENZA duplicare la logica di caricamento/fallback.
- `load_embedder(model, cache_dir, alternate_name)` delega a `semantic_sync`.
- `check_fastembed_upgrade.py` rifattorizzato per usare `load_embedder`.
- Il backend resta fastembed 0.5.1: il Protocol serve a POTERLO sostituire.

**Stato reale della migrazione** (chiarimento, la versione precedente di
questo documento elencava `analysis_sync` fra i convertiti: non lo è):
migrato `check_fastembed_upgrade`; restano sul percorso vecchio
`analysis_sync.py` (uso sperimentale, non bloccante) e `semantic_sync.py`
stesso, che per definizione possiede il fallback.

### P2 #12 — Dettaglio (fatto)
`mypy.ini` esclude i test storici non annotati. La lista è una **deny-list**
esplicita (`test_(bootstrap|chunks|…)`), non un allow-list: un test **nuovo**
entra nel check da solo, senza registrazione.

Motivo della scelta: con l'allow-list precedente
(`test_(?!aggiornamenti|prima_sezione|…)`) un `test_foo.py` creato oggi
veniva **escluso in silenzio** — senza errore, senza avviso — quindi il
typing graduale si auto-sabotaggiava proprio sui file nuovi, che sono
 quelli da tipizzare per primi. La deny-list inverte il default a favore
di "nuovo file = controllato".

Stato attuale: `test_aggiornamenti`, `test_auto_threads`,
`test_cache_maintenance`, `test_embedder`, `test_numerazione`,
`test_pipeline_cache`, `test_prima_sezione`, `test_slow_check` sono nel
check. Man mano che un test storico viene annotato, il suo nome esce
dalla deny-list.

---

## Non fatto di proposito (scelta consapevole)

- **Split fisico di `main.py` / `test_sync.py` / `semantic_sync.py` / `llm_sync.py`**
  troppo invasivo: `test_sync.py` importa 20 funzioni private da `main`
  (verificato con AST, non con grep: 20 nomi distinti, tutte private;
  21 distinti su tutti i test). Spezzare romperebbe centinaia di test per
  un beneficio non visibile all'utente. Documentato come proposta futura,
  non eseguito.

  *Correzione di una stima precedente:* si diceva "~40 funzioni private".
  Le 40 erano le istruzioni `from main import`, non i simboli distinti: il
  numero reale è 20. La stima gonfiata rendeva la rinuncia più costosa di
  quanto sia: a 20 dipendenze, un'estrazione a shim come quella di #9
  sarebbe plausibile anche per `main.py`. Resta comunque rimandata, ma per
  una ragione valida (beneficio incerto per l'utente) e non per una barriera
  di 40 dipendenze.

---

## Verifica (da rilanciare prima di ogni commit)

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy .
.\.venv\Scripts\python.exe -m unittest discover -s . -p 'test_*.py'
```

Stato all'ultimo lancio: ruff pulito, mypy `Success: no issues found in
33 source files`, **801 test OK**, `_debug_hardware.py` 15/15.
