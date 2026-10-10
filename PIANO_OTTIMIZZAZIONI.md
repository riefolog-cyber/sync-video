# Piano di ottimizzazione — chiuso

Il lavoro di pulizia/ottimizzazione di `sync-video` è finito: tutte le voci
proposte sono state fatte o hanno una risposta esplicita. Questo file resta
come registro delle **decisioni**, non delle operazioni — i dettagli di ogni
cambiamento sono nei messaggi di commit e nei commenti del codice.

Ultimo aggiornamento: chiusura del piano (P2 #11 archiviata come non
consigliata, primo avvio automatico di Python su Windows).

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
| P2 #11 | `llm_sync.py` fuori dal percorso di default | ❌ **non consigliata**, vedi sotto |
| P2 #12 | Typing graduale dei test (deny-list mypy) | ✅ |
| P2 #13 | Riparato `_physical_cpus()` (era morto in silenzio) | ✅ whisper 20→14 thread |
| P2 #14 | Adattamento a ogni PC: RAM, encoder GPU, batch | ✅ |
| P2 #15 | `main.py --help` rotto da un `%` non escapato | ✅ |
| P2 #16 | Rilevamento GPU su Linux a cascata | ✅ |
| #17 | Primo avvio automatico anche di Python (Windows) | ✅ |
| #18 | Controllo dello spazio disco prima del primo download | ✅ |
| #19 | Avviso se i modelli di default sono pesanti per il PC | ✅ |

## Decisioni da non riaprire

**P2 #11 — `llm_sync.py` come percorso sperimentale: non consigliata.**
`--llm` ha già `default="off"`, quindi il comportamento di default è già
quello locale. Per escludere il modulo dal percorso di default servirebbe
renderlo lazy in `main.py` (lo importa per 11 nomi a livello di modulo) e
escluderlo dai test: guadagno reale minimo, costo di manutenzione certo.

**Split fisico di `main.py` / `semantic_sync.py` / `llm_sync.py`: non
eseguito.** `test_sync.py` importa **20** funzioni private da `main` (20
nomi distinti, tutti privati; 21 su tutti i test — verificato con AST).
Spezzare romperebbe centinaia di test per un beneficio non visibile
all'utente. A 20 dipendenze un'estrazione a shim, come quella di P2 #9,
sarebbe plausibile: resta rimandata per una ragione di merito (beneficio
incerto), non per una barriera tecnica.

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

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy .
.\.venv\Scripts\python.exe -m unittest discover -s . -p 'test_*.py'
.\.venv\Scripts\python.exe .\_debug_hardware.py
```

Stato all'ultimo lancio: ruff pulito, mypy pulito, test verdi,
`_debug_hardware.py` 15/15.