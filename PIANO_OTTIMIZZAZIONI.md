# Piano di ottimizzazione — stato e avanzamento

Documento vivo del lavoro di pulizia/ottimizzazione del progetto `sync-video`.
Ogni voce ha uno stato: ✅ fatto e committato, 🚧 in corso, ⬜ proposto.

Ultimo aggiornamento: P2 #10 e P2 #12 completati (commit "interfaccia Embedder + typing graduale dei test").

---

## P0 — Correttezza e ordine (quick win)

| # | Voce | Stato |
|---|------|-------|
| 1 | Fix mypy: guard `t.ident is not None` in `main.py` (thread-exit helpers) | ✅ commit `3914214` |
| 2 | Rimuovere file fantasma `Audio` (0 byte, confondeva `find_audio_file`) | ✅ commit `3914214` |
| 3 | Spostare `sync-video-architecture.html` in `docs/` | ✅ commit `3914214` |
| 4 | Commit delle modifiche in sospeso | ✅ commit `3914214` |

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
| 9 | Estrarre moduli "foglia" da `main.py`: `pipeline_cache.py`, `pipeline_report.py` | ✅ commit `2e35405` (718 test verdi) |
| 10 | Astrarre l'embedding-backend: Protocol `Embedder` + `load_embedder()` | ✅ fatto (vedi header) |
| 11 | `llm_sync.py` come percorso sperimentale (fuori da bootstrap/test default) | ⬜ proposto |
| 12 | Typing graduale dei test (`mypy` sui test annotati) | ✅ fatto (vedi header) |

### P2 #9 — Dettaglio (fatto)
`main.py` continua a re-esportare `_file_hash`, `_load_cache`, `_save_cache`,
`_clean_orphan_cache`, `_format_time`, `_print_timing`, `_slide_list_text` dai
nuovi moduli, così `from main import X` nei test resta valido senza modifiche.
~400 righe spostate fuori da `main.py`, zero test rotti.

### P2 #10 — Dettaglio (fatto)
Obiettivo: poter provare un backend alternativo (hf-transformers + ONNX
quantizzato) senza riscrivere `semantic_sync.py`.

- `embedder.py`: Protocol `Embedder` (`embed()` + `embed_id`) + `_FastEmbedEmbedder`
  che wrappa i due passi di `semantic_sync` (`_load_embed_model` + `_make_embed_fn`)
  SENZA duplicare la logica di caricamento/fallback.
- `load_embedder(model, cache_dir, alternate_name)` delega a `semantic_sync`.
- `check_fastembed_upgrade.py` rifattorizzato per usare `load_embedder`.
- Il backend resta fastembed 0.5.1: il Protocol serve a POTERLO sostituire.

### P2 #12 — Dettaglio (fatto)
`mypy.ini` passa da `exclude = test_.*\.py$` a una regex che ESCLUDE solo i test
storici non annotati. I test annotati entrano nel check di CI:
`test_aggiornamenti`, `test_prima_sezione`, `test_numerazione`,
`test_cache_maintenance`, `test_auto_threads`, `test_slow_check`, `test_embedder`.
Man mano che un test storico viene annotato, il nome esce dalla lista di esclusione.

---

## Non fatto di proposito (scelta consapevole)

- **Split fisico di `main.py` / `test_sync.py` / `semantic_sync.py` / `llm_sync.py`**
  troppo invasivo: `test_sync.py` importa ~40 funzioni private da `main`.
  Spezzare romperebbe centinaia di test per un beneficio non visibile all'utente.
  Documentato come proposta futura, non eseguito.

---

## Verifica (da rilanciare prima di ogni commit)

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy .
.\.venv\Scripts\python.exe -m unittest discover -s . -p 'test_*.py'
```
