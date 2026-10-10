<!-- Estratto da README.md (righe 615-728). Torna al [README](../README.md). -->

## 🎮 Comandi

```bash
# Default (cerca presentazione.pdf + podcast.* nella cartella)
python main.py

# File personalizzati
python main.py --pdf slides.pdf --audio registrazione.mp3

# Anteprima timeline (niente video)
python main.py --preview

# Dry-run: genera timeline senza produrre video
python main.py --dry-run --debug

# Forza nuova analisi (ignora cache)
python main.py --no-cache

# Whisper con modello più piccolo (più veloce) o più grande (più preciso)
python main.py --whisper-model small
python main.py --whisper-model large-v3

# Trascrizione: forza OpenVINO (iGPU, più veloce) o solo faster-whisper
python main.py --transcriber openvino
python main.py --transcriber whisper

# Esegui i test (147 unit test: pipeline, LLM, chunks, integrazione)
python -m unittest test_sync test_integration test_llm_sync test_chunks
```

### Opzioni principali

| Opzione | Default | Descrizione |
|---|---|---|
| `--pdf` | `presentazione.pdf` | PDF o PPTX |
| `--audio` | auto | File audio (mp3/m4a/wav) |
| `--output` | `video_finale.mp4` | File video output |
| `--flow` | auto-detect | `slide-audio` o `audio-slide` |
| `--dry-run` | — | Solo timeline, niente video |
| `--preview` | — | Mostra timeline e esci |
| `--no-cache` | — | Ignora cache |
| `--debug` | — | Log dettagliato |
| `--transitions` | `0.0` | Dissolvenza tra slide (s) |
| `--lang` | `ita` | Lingua OCR |
| `--whisper-model` | `small` | tiny/base/small/medium/large/large-v3 |
| `WHISPER_MODEL` (env) | `small` | Modello usato da `genera_video.bat` (es. `set WHISPER_MODEL=tiny` per la bozza veloce) |
| `VERIFY_VIDEO` (env) | `1` | Controllo del video finito attivato da `genera_video.bat` (pochi secondi): `set VERIFY_VIDEO=0` per disattivarlo |
| `--transcriber` | `auto` | `auto`/`openvino`/`whisper` (OpenVINO ~1.5x più veloce) |
| `--whisper-beam` | `1` | Beam size faster-whisper. `1` = decodifica greedy, default **misurato**: ~2.3× più veloce con le ancore `slide N` entro 0.15s da beam 5. Con `1` la pipeline **sceglie da sola**: decodifica veloce se le slide sono vincolate dalle ancore, altrimenti rifà la trascrizione a beam `5` (vedi sopra). `2`-`5` = scelta manuale, nessuna correzione automatica |
| `--no-auto-beam` | — | Disattiva la scelta automatica del beam: usa esattamente `--whisper-beam` (o `AUTO_BEAM=0`) |
| `AUTO_BEAM_PINNED_RATIO` (env) | `0.5` | Frazione di slide vincolate da ancore sotto la quale scatta la decodifica accurata (`1.1` = forza sempre il percorso accurato). `WHISPER_BEAM_ACCURATE` (env, default `5`) sceglie il beam di quella decodifica |
| `AUTO_BEAM_AB_MARGIN` (env) | `0.0` | Quanto deve vincere la decodifica **accurata** (in `avg_z`) per essere preferita alla veloce. `0.0` = basta non perdere. La **risoluzione misurata** del punteggio è ~`0.05-0.10` (vedi sotto): con `AUTO_BEAM_AB_MARGIN=0.05` la veloce subentra solo se il vantaggio è fuori dalla banda di rumore |
| `--whisper-batch` | `8` (`4` sotto i 6 GB di RAM) | Segmenti decodificati insieme (stesso modello e stessa decodifica: cambia solo il throughput). `0` = sequenziale. Ripiega da solo se il decoder a batch non è disponibile |
| `WHISPER_BEAM` / `WHISPER_BATCH` (env) | `1` / `8` | Override dei due parametri senza toccare la riga di comando |
| `EMBED_THREADS` (env) | `min(12, core fisici)` | Thread ONNX per gli embedding |
| `WHISPER_THREADS` (env) | core fisici, senza tetto | Thread per faster-whisper |
| `VIDEO_THREADS` (env) | `min(12, core fisici)` | Thread di encoding **solo con libx264**: con un encoder accelerato lavora la GPU |
| `OCR_WORKERS` (env) | `min(6, core fisici)` | Thread per la conversione delle pagine in OCR |
| `EMBED_BATCH` (env) | `16` / `32` / `64` per RAM <6 / <12 / ≥12 GB | Testi per volta nel modello di embedding |
| `--video-encoder` | `auto` | `auto` sceglie NVENC/QSV/AMF/VideoToolbox se la GPU li supporta, altrimenti `libx264` su CPU. Se l'encoder accelerato fallisce a runtime **ripiega automaticamente** su libx264 |

> **I thread non hanno tutti lo stesso default**, e il tetto non è 8.
> Oggi sono differenziati perché i due limiti misurati sono diversi: la
> **banda memoria** (embed/video: `min(12, core fisici)` — sullo Snapdragon X
> Elite 12 thread erano più lenti di 8) e il **throughput puro** (whisper:
> segue i core fisici senza tetto rigido, perché è l'unica fase che scala
> davvero con i core). L'OCR ha un tetto proprio (`min(6, core fisici)`)
> perché è I/O + CPU.
>
> Il default si calcola sui **core fisici**, non su quelli logici: i fratelli
> SMT non danno throughput, solo context switch. Il override via env ha
> sempre precedenza. La riga di riepilogo a fine run dice cosa è stato usato
> e da dove (`auto da 14 fisici`). I core fisici si rilevano senza
> `psutil`: `ctypes` su Windows, `/proc/cpuinfo` su Linux, `sysctl` su macOS.
>
> Se la trascrizione è il collo di bottiglia della tua run, conviene misurare:
>
> ```powershell
> $env:WHISPER_THREADS="14"   # 14 = core fisici della i7-12700H (6P+8E)
> $env:EMBED_THREADS="14"
> $env:VIDEO_THREADS="14"
> .\.venv\Scripts\python.exe main.py --no-cache
> ```
>
> Misura tempo e qualità (`--whisper-beam 1` veloce vs `5` accurato): il beam
> influenza la timeline, i thread no. Tieni il valore che misuri migliore in
> `.env`.
| `--openvino-device` | `GPU` | Device OpenVINO (`GPU` iGPU o `CPU`) |
| `--openvino-download` | — | Scarica modello OpenVINO IR (una tantum) |
| `--prefetch-models` | — | Scarica **tutti** i modelli ML (embedding, pesi Whisper, OpenVINO IR) e esce, senza toccare PDF o audio. Utile dopo un clone o un cambio di macchina: tiene i download fuori dalla prima run reale |
| `--semantic-model` | e5-large | Modello embedding |
| `--semantic-window` | `4.0` | Secondi per blocco |
| `--semantic-min-duration` | `3.0` | Durata minima slide (s) |
| `--semantic-temperature` | `0.15` | Competizione softmax (più bassa = picchi più netti) |
| `--semantic-min-z` | `0.45` | Soglia sul **picco medio normalizzato** (z-score per slide, la stessa matrice usata dal posizionamento): è la misura che distingue un allineamento giusto da slide mescolate. Sotto soglia la timeline **non** viene scartata: viene segnalata (escalation al LLM, gate `--strict-sync`, report e riepilogo finale). Tarata su dati reali: ordine giusto 0.61-0.75, slide mescolate 0.30-0.43, invertite 0.18, non correlate ≤0.09, quasi-duplicate 0.006 |
| ~~`--semantic-min-sim`~~ | — | **Rimosso.** Era una soglia sulla similarità media *grezza* e non poteva mai scattare: misurata sui dati reali, anche un testo senza senso dà 0.75 di coseno contro una slide e il parlato reale non scende mai sotto 0.75, quindi la soglia (0.10) era fuori scala di un fattore ~7. Un presidio che non presidia è peggio di non averlo. La decisione è su `--semantic-min-z`, che è libero da scala. Se hai lo `--semantic-min-sim` in uno script, toglilo: il flag non esiste più |
| `--llm` | `off` | Selezione slide con LLM: `off` (solo embedding locale, **il default**), `auto` e `9router` (**oggi equivalenti**: l'unico provider è 9Router, quindi entrambi usano la stessa cascata e ripiegano sull'embedding locale). Libero: slide per chunk. Ordinato: solo le slide senza ancora esplicita |
| `--llm-model` | — | Override modello LLM (es. `comboact`, `cf/@cf/mistralai/mistral-small-3.1-24b-instruct`) |
| `--llm-chunk` | `30.0` | Secondi per chunk inviato all'LLM |
| `--llm-wait-timeout` | `0.0` | Se 9Router è necessario ma spento: secondi massimi di attesa prima del fallback embedding. `0` = attesa illimitata (pausa + avviso, riprende appena 9Router risponde) |
| `--llm-review` | — | Dopo la timeline LLM nel flusso libero, secondo passaggio LLM che ri-verifica la selezione chunk→slide e avvisa (senza modificare la timeline) sui chunk sospetti. Risultato cachato. |
| `--llm-local-threshold` | `2` | Nel flusso ordinato, numero massimo di slide senza ancora che il **raffinamento locale** (embeddings, ~secondi, nessun 9Router) può gestire da solo. Il motore locale gira comunque **sempre per primo**; oltre questa soglia si chiede anche all'LLM (9Router, che si avvia da solo se spento) di migliorare la timeline, e se non riesce si usa quella locale già calcolata. `0` = chiedi sempre all'LLM |
| `--require-full-anchors` | — | Nel flusso ordinato, **interrompi** se il podcast non annuncia TUTTE le slide (ancore `slide N` incomplete) invece di generare un video con durate stimate. Utile in batch/CI (`genera_video.bat`) |
| `--min-anchor-coverage` | `0.5` | Nel flusso slide → podcast, **interrompi prima della sincronizzazione** se la frazione di transizioni con ancora `slide N` è sotto la soglia (default 50%: con `--no-confirm` la pausa interattiva viene saltata e senza gate un podcast condensato generava un video degradato in silenzio). `0` disattiva. Più morbido di `--require-full-anchors`, che pretende il 100% |
| `--strict-sync` | — | Modalità "non consegnare un video sospetto". Blocca PRIMA della generazione se un segmento di durata anomala risulta disallineato dal contenuto (il parlato somiglia a un'altra slide) o se la revisione LLM (`--llm-review`) contesta la mappa chunk→slide; blocca DOPO la generazione (il video resta su disco, ma l'esito è un errore) se la verifica frame vs slide trova segmenti con la slide sbagliata. Attiva automaticamente `--verify-video`. Default: avviso soltanto. Il report dei segmenti è salvato comunque in `.cache/sync_report.json` |
| `--verify-video` | — | Dopo la generazione estrae un frame a metà di ogni segmento e lo confronta con la slide attesa: è l'unico controllo sull'ARTEFATTO (la timeline può essere coerente e il video comunque sbagliato). I frame restano in `.cache/verify_frames/` e l'esito finisce in `sync_report.json`. `genera_video.bat` lo attiva di default (pochi secondi in più) |
| `--no-auto-repair` | — | Disattiva la **riparazione automatica**. Quando la verifica del video trova un segmento con la slide sbagliata, la pipeline sposta da sola quel confine (motore embedding, solo nella direzione indicata dall'evidenza) e rigenera il video, poi lo ricontrolla. Con questo flag l'esito resta un avviso e il video non viene rifatto |
| `--force-setup` | — | Rileva l'hardware da capo e riscrive i fatti in `.cache/machine_setup.json` (utile se hai cambiato GPU o spostato la cartella). La *decisione* sul motore viene comunque ricalcolata a ogni run |
| `--no-auto-setup` | — | Salta il rilevamento hardware: il motore è solo quello passato con `--transcriber` |
| `--whisper-device` | `cpu` | Device faster-whisper (`cpu`/`cuda`). **Sceglilo a mano solo se sai cosa fai**: in quel caso un fallimento di CUDA non viene ripiegato su CPU in silenzio, perché un fallback non richiesto sarebbe una sorpresa |
| `--whisper-compute-type` | `int8` | Precisione faster-whisper (`int8` CPU, `float16` CUDA) |
| `--openvino-model-dir` | `.cache/whisper_openvino_small` | Cartella del modello OpenVINO IR |
| `--engine` | `ffmpeg` | Motore di rendering video (`ffmpeg` veloce, `moviepy` richiesto per `--transitions`) |
| `--ocr-workers` | `min(6, core fisici)` | Thread per l'OCR (sovrappone `OCR_WORKERS`) |
| `--dpi` | `300` | Risoluzione di rendering delle slide per l'OCR |
| `--slides-dir` | `temp_slides` | Cartella delle slide renderizzate |
| `--skip-slides` | — | Esclude un elenco di slide (indici 1-based, es. `1,4,9`) dal flusso |
| `--no-free-ordered-fallback` | — | Nel flusso libero, non ripiegare sull'allineamento ordinato quando la selezione semantica fallisce: interrompi |
| `--no-confirm` | — | Non chiedere conferma interattiva (per batch/CI) |
| `--no-update` | — | Al controllo aggiornamenti: notifica senza installare |
| `--no-update-check` | — | Non controllare gli aggiornamenti su PyPI (default di `genera_video.bat`) |
| `--semantic-cache-dir` | `.cache/embedding_model` | Cartella dei modelli embedding |
| `--log-file` | — | Scrivi anche il log su file |

---

