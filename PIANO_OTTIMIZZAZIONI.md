# Piano di ottimizzazione — stato e avanzamento

Documento vivo del lavoro di pulizia/ottimizzazione del progetto `sync-video`.
Ogni voce ha uno stato: ✅ fatto e committato, 🚧 in corso, ⬜ proposto.

Ultimo aggiornamento: P2 #9 (debito di test chiuso) e P2 #12 (deny-list
invertita) completati; conteggi del piano corretti contro il codice reale.

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
| 6 | Auto-tuning adattivo dei thread (`WHISPER/EMBED/VIDEO_THREADS`) + riga riepilogo | ✅ commit `29e55b8` |
| 7 | Split README (1090 righe) in `docs/` | ⬜ rimandato (approccio cambiato, vedi nota) |
| 8 | CI: cache pip/HF + nightly full via `SYNC_VIDEO_RUN_SLOW` | ✅ commit `630aa75` |

**Nota #7 — README:** il primo tentativo di split automatico è stato annullato
(`git checkout README.md`) per non spezzare la narrazione. Il README resta un
documento unico: è già ben sezionato, e dividerlo ha più costi (link rotti,
duplicazione) che benefici. Lasciato come miglioramento futuro, non prioritario.

---

## P2 — Architettura e manutenibilità

| # | Voce | Stato |
|---|------|-------|
| 9 | Estrarre moduli "foglia" da `main.py`: `pipeline_cache.py`, `pipeline_report.py` | ✅ commit `2e35405` (718 test verdi) + `test_pipeline_cache.py` |
| 10 | Astrarre l'embedding-backend: Protocol `Embedder` + `load_embedder()` | ✅ fatto (vedi header) |
| 11 | `llm_sync.py` come percorso sperimentale (fuori da bootstrap/test default) | ⬜ proposto |
| 12 | Typing graduale dei test (`mypy` con deny-list dei file storici) | ✅ fatto (vedi header) |

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
28 source files`, **747 test OK**.
