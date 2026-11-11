# Slide2Video 🎬

[![CI](https://github.com/riefolog-cyber/sync-video/actions/workflows/ci.yml/badge.svg)](https://github.com/riefolog-cyber/sync-video/actions/workflows/ci.yml)

Sincronizza automaticamente una presentazione PDF con un podcast audio e genera un video in cui ogni slide appare al momento giusto.

**Pipeline:** PDF → OCR → Trascrizione (Whisper) → Ancore "slide N" → Sincronizzazione semantica (embeddings) → Video MP4

```mermaid
graph LR
    A[PDF] -->|OCR Tesseract| B[Testi Slide]
    C[Audio] -->|Whisper| D[Trascrizione]
    D -->|riferimenti 'slide N' / 'pagina N' / 'la prossima'| E[Ancore]
    B --> F[Sincronizzazione semantica - embeddings]
    D --> F
    E -->|vincoli di precisione| F
    F -->|reconcile_timeline| G[Timeline Finale]
    G --> H[Video MP4]
```

---

## 🚀 Avvio rapido

```bash
# Opzione 1: Doppio click su genera_video.bat

# Opzione 2: Terminale
genera_video.bat

# Il bootstrap installa automaticamente TUTTE le dipendenze:
# pacchetti pip, Tesseract OCR, ffmpeg, modelli ML
# Su Windows installa anche Python, se sul PC non c'e' ne' uno
# (via winget: serve Windows 10 1809 o Windows 11), scegliendo la
# versione in base all'architettura: 3.12 su x86-64, 3.11 su ARM64.
# Output: video_finale.mp4
#
# Installa/aggiorna anche i pacchetti sotto la versione minima richiesta: un
# pacchetto troppo vecchio puo' far fallire l'import di un ALTRO pacchetto (es.
# numpy vecchio -> pandas non si importa -> pytesseract sembra guasto).
```

> ## 🪟 Funziona solo su Windows
>
> L'avvio automatico, gli script e la scelta del motore di trascrizione sono
> pensati per **Windows 10/11**. È l'unica piattaforma verificata: scarichi lo
> ZIP, fai doppio clic su `genera_video.bat`, e il PC si prepara da solo.
>
> **Su macOS non funziona**, e non è una questione di configurazione: i `.bat`
> non esistono, Python va installato a mano, serve Homebrew per Tesseract e
> ffmpeg, e soprattutto **sui Mac con chip Apple non c'è alcun motore di
> trascrizione** — `faster-whisper` non pubblica wheel per quella CPU e
> OpenVINO non esiste su macOS. Manca il pezzo centrale del programma.
>
> **Su Linux** non è supportato per lo stesso motivo.

> Su Windows non devi installare niente a mano: scarichi lo ZIP, fai doppio
> clic su `genera_video.bat`, e il programma prepara il PC da sé.
> Dettagli e come disattivare l'installazione automatica:
> [docs/setup.md](docs/setup.md#primo-avvio-automatico).

> ## ♻️ Rilanci: il programma riusa quello che ha già fatto
>
> Rilanciando **senza cambiare nulla** il programma non rifà il lavoro:
>
> - **il video già renderizzato viene riusato** (nessuna ricodifica, ~71 s
>   risparmiati): l'impronta copre il contenuto delle slide, i tempi, il
>   contenuto dell'audio e i parametri di rendering. Se hai toccato il file di
>   output, o vuoi rifare tutto, usa `--no-cache`;
> - **l'audio viene copiato** nel video (niente ricodifica lossy) quando la
>   sorgente è già un MP4/AAC; con mp3 o wav viene convertito in AAC 160 k;
> - le **trascrizioni, gli OCR e gli embedding** si riprendono dalle cache, e
>   il riuso degli embedding è **per testo**: una parola cambiata ricalcola
>   solo i blocchi nuovi, non tutti.
>
> Due run sulla stessa cartella non si pestano: la seconda **aspetta fino a
> 15 minuti** che la prima finisca e poi si ferma con un avviso (il blocco è
> del sistema operativo: se la prima muore, il blocco si libera da solo).
>
> La **verifica del video** (`--verify-video`) lascia in
> `.cache/verify_frames/` **solo i fotogrammi dei segmenti sbagliati**: sono
> gli unici da guardare, e una cartella vuota vuol dire che tutti i segmenti
> erano corretti.

### 🔒 Ambiente dedicato (consigliato)

Il programma funziona anche col Python di sistema, ma quel Python è **condiviso con tutti gli altri programmi del PC**: un `pip install` fatto altrove può cambiare una versione e impedire l'avvio. Misurato il 16/09/2026: un'altra installazione ha messo `numpy 1.24.3` e `pandas` (dipendenza di pytesseract) non si importava più, quindi la pipeline non partiva nemmeno per le fasi che non usano l'OCR.

```bash
# Una volta sola: crea la cartella .venv e installa i pacchetti del progetto
crea_venv.bat          # Windows
```

> Il progetto è **solo Windows**: non c'è uno script `.sh` equivalente.

Da quel momento `genera_video.bat`, `aggiornamenti.bat`, `prova.bat`, `controlli.bat` e `check_embedding_models.bat` usano `.venv` da soli: non serve cambiare nulla a mano.

| Comando | Cosa fa |
|---|---|
| `crea_venv.bat` | Crea `.venv` (o completa i pacchetti mancanti se esiste) |
| `crea_venv.bat --ricrea` | Cancella `.venv` e la ricrea da zero |
| `set SYNC_VIDEO_NO_VENV=1` | Usa il Python di sistema, ignorando `.venv` (Windows) |

La venv **riusa i modelli già scaricati** (cartella `.cache` e cache di HuggingFace): non riscarica nulla, e la prima run resta veloce. Dentro `.venv` `pip check` non segnala conflitti; nel Python globale ne convivono diversi (pacchetti di altri progetti).

> **Cambio di PC / hardware**: il motore di trascrizione **non viene mai
> ripreso da disco**. In `.cache/machine_setup.json` si salvano solo i *fatti*
> hardware (impronta della macchina + lista delle GPU), che sono lenti da
> ottenere ma stabili; la *decisione* ("usa CUDA", "usa OpenVINO") è ricalcolata
> a ogni run e poi **validata contro il runtime reale** (il device esiste? il
> pacchetto è installato? il modello c'è?). Se non regge, ripiega su CPU
> avvisando. Perché: la decisione dipende da fatti che cambiano (che pacchetti
> sono installati, che device espone il runtime *adesso*), mentre l'hardware no.
> Persistendola, copiare la cartella da un PC con GPU NVIDIA a uno senza chiudeva
> la run con `CUDA driver version is insufficient` **dopo** OCR e rendering, senza
> che nulla avesse notato che la GPU non c'era più.
>
> Come difesa aggiuntiva, se faster-whisper non riesce a costruire il modello su
> CUDA, ripiega da solo su CPU (int8) e la pipeline prosegue; se però avevi
> chiesto `--whisper-device cuda` esplicitamente, non lo fa di nascosto e ti
> avvisa. `.env` resta per gli override espliciti tuoi (che hanno precedenza);
> se cambi GPU restando sullo stesso sistema, `--force-setup` rileva di nuovo.

> **Precisione assoluta**: il programma non distribuisce mai le slide uniformemente. La timeline nasce dai **tempi pronunciati dal relatore** quando ogni cambio di slide è annunciato ("passiamo alla slide 3", "passiamo alla pagina 3", "passiamo alla prossima"); per i cambi **non** annunciati li stima il motore embedding (offline, senza LLM) dal contenuto del parlato. Se non c'è nessun riferimento riconoscibile il flusso viene **assunto** (`slide-audio`) e la run prosegue in modalità stimata, con avviso nel log e nel riepilogo; per fermarsi sempre usa `--require-flow`.

### 🔀 Flussi supportati

| Flusso | Segnale nell'audio | Esempio |
|---|---|---|
| `slide-audio` | *"Passiamo alla slide 3"* | Speaker nominano il numero slide: in cifre (*"slide 3"*), in cardinali (*"slide tre"*), in ordinali (*"la terza diapositiva"*, *"la sesta slide"*) o con la parola *"pagina"* (*"passiamo alla pagina 3"*) |
| `audio-slide` | *"Passiamo al blocco successivo"* | Dibattito, poi slide generate dopo |

Il numero può anche **non essere detto**: *"passiamo alla prossima slide"* vale
"l'ultima slide annunciata + 1" (il programma lo deduce, ma solo con un verbo
di transizione vicino: *"a pagina 3 del libro"* è una citazione, non un cambio
di slide).

Override manuale: `python src/main.py --flow audio-slide` (oppure `--flow slide-audio`)

> **Auto-detection automatica**: il sistema sceglie da solo fra i due flussi.
> Se il podcast segue i vincoli del prompt NotebookLM (`slide N` o
> `blocco successivo`) usa il flusso corrispondente e rispetta le ancore.
>
> **Un audio senza riferimenti riconoscibili produce comunque il video.** Se
> la trascrizione non contiene né "slide N" né "blocco successivo" il flusso
> viene **assunto** (`slide-audio`), le ancore restano zero e i confini delle
> slide li decide il contenuto del parlato (timeline stimata): un avviso nel log
> lo dichiara. Per confini esatti rigenera il podcast annunciando le slide, come
> sotto; per **fermarti** invece di procedere usa `--require-flow`.

### 📝 Prompt NotebookLM (come generare presentazione e podcast)

Un solo processo di input:

| Prompt | Flusso | Quando usarlo |
|---|---|---|
| [`PROMPT_PRESENTAZIONE (PREDEFINITO).md`](<PROMPT_PRESENTAZIONE (PREDEFINITO).md>) | deck → podcast con ancore `slide N` | Confini **misurati** (è lo speaker a dichiarare il tempo). Podcast più strutturato. Fragile a un errore operativo: se il deck cambia dopo il podcast, le ancore puntano a pagine che non esistono più |

Il prompt richiede che ogni pagina abbia una sezione di parlato **sviluppata**:
una sezione che si limita a elencare produce una pagina che nel video scorre
veloce (vedi `starved_slides` nel report).

**Il prompt.** Contiene tutto quello che serve perché il video sia
sincronizzato, più il tono del dibattito. Non chiede più l'elemento visivo per
pagina: la scelta grafica resta a NotebookLM. Il testo decorativo fuori corpo
però pesa ancora sull'OCR, che legge tutto ciò che appare sulla pagina e
diluisce il segnale. Le regole protettive sono sorvegliate da `test_prompts.py`:
se una frase sparisce dal file, i test falliscono; se la si toglie di proposito,
va registrata in `REGOLE_RIMOSSE` con il motivo, e da lì il test verifica che non
rientri.


### ✅ Ricetta validata (5 run verdi consecutive)

Procedura congelata: deck da 12–15 slide generato dal prompt, **presentazione come UNICA fonte** per il podcast, annunci `slide N` su tutte le transizioni, sezioni da 45s in su. Gate `--min-anchor-coverage` (default 50%, variabile d'ambiente `MIN_ANCHOR_COVERAGE` per cambiarlo): la run si ferma se le transizioni annunciate sono sotto soglia. Con il flusso **assunto** (nessun riferimento riconoscibile) il gate non si applica: il video esce in modalità stimata — `--require-flow` e `--require-full-anchors` restano per chi vuole fermarsi sempre.

| Run | Slide | Ancore | Durata min | Fiducia | Frame-check | Dubbi manuali |
|---|---|---|---|---|---|---|
| 1 | 12 | 11/11 | 45s | ALTO | 12/12 | 2 (non allarmi) |
| 2 | 15 | 13/14 | 58s | ALTO | 15/15 | 2 (non allarmi) |
| 3 | 14 | 12/13 | 37s | ALTO | 14/14 | 2 (non allarmi) |
| 4 | 15 | 13/14 | 58s | ALTO | 15/15 | **nessuno** |
| 5 | 15 | 11/14 | 45s | ALTO | 15/15 | **nessuno** |

Con la procedura sopra (tutte le transizioni annunciate) la fiducia è **ALTO**:
i confini sono quelli pronunciati dal relatore, e il programma non ha dovuto
stimarli dal contenuto. Quando qualche transizione **non** è annunciata, la
fiducia si legge nel riepilogo finale (*"Quanto è sicuro il programma"*).

Se i materiali seguono le regole (fonte unica, tutte le pagine annunciate, sezioni sviluppate), il risultato si ripete: non modificare prompt né soglie senza rieseguire `python -m unittest discover -s src -p "test_*.py"`.


> **Consiglio**: nominare la slide quando si cambia argomento (*"passiamo alla slide 3"*) regala ancore deterministiche ad alta precisione. Senza di esse il semantico allinea comunque per contenuto, ma per stima. Prompt NotebookLM: [`PROMPT_PRESENTAZIONE (PREDEFINITO).md`](<PROMPT_PRESENTAZIONE (PREDEFINITO).md>).
>
> ⚠️ **Il nemico non è dimenticare la slide: è richiamarla.** Se il conduttore
> torna su una pagina già trattata e ne ripete il numero (*"guarda sempre slide 3,
> applicano mercato"*), la pagina viene mostrata in ritardo e resta "appiccicata"
> alla precedente per tutto quel tempo. Misurato su un podcast reale: 3 confini su 8
> spostati di **74s, 85s e 53s**.
>
> Oggi è risolto su due livelli. **Alla fonte**, il prompt dedicato vieta i
> richiami e chiede di non ripetere mai un numero: è la soluzione vera, perché
> previene il difetto invece di correggerlo. **In difesa**, l'estrattore qualifica
> ogni menzione: distingue un conteggio quantificato (*"le 13 slide di questo
> documento"*, che va ignorato) da un riferimento vero, e fra i riferimenti sceglie
> la **prima** occorrenza, non l'ultima. Le due cose servono perché il semplice
> "prima menzione" rotolava indietro il video di oltre un minuto nel caso del
> conteggio, e su quel caso c'è un test dedicato
> (`test_early_total_slide_count_does_not_poison_real_anchor`).
>
> Se vedi `Slide richiamate più volte (richiamo ignorato...)` è un'informazione,
> non un allarme: segnala che il podcast richiama delle pagine e che il programma
> ha scelto il confine giusto ignorando il richiamo.



---

## 📚 Documentazione

Questo README copre avvio rapido e uso quotidiano. Il resto e' in [`docs/`](docs/):

| Pagina | Contenuto |
|---|---|
| [`docs/setup.md`](docs/setup.md) | Setup su un altro PC |
| [`docs/flussi.md`](docs/flussi.md) | Due flussi di lavoro (e i loro avvisi) |
| [`docs/comandi.md`](docs/comandi.md) | Comandi |
| [`docs/verifica.md`](docs/verifica.md) | Verifica post-run: la prova sul materiale reale e `analysis_sync.py` |
| [`docs/diagrammi.md`](docs/diagrammi.md) | Diagrammi (archify) |
| [`docs/struttura.md`](docs/struttura.md) | Struttura progetto |
| [`docs/funzionamento.md`](docs/funzionamento.md) | Come funziona |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | Troubleshooting |
| [`docs/PIANO_OTTIMIZZAZIONI.md`](docs/PIANO_OTTIMIZZAZIONI.md) | Registro delle ottimizzazioni (batch 1→10: cosa è stato fatto e misurato) |

| [`docs/sync-video-architecture.html`](docs/sync-video-architecture.html) | Diagramma architettura |

> **Prova sul materiale vero**: `python src/scripts/prova_materiale_reale.py`
> lancia la pipeline sul tuo PDF e sul tuo podcast e confronta la timeline con
> un riferimento salvato: esce con 0 se è **identica**, con 1 se un confine si
> è spostato (e di quanto). È il modo più rapido per capire se una modifica
> ha cambiato qualcosa. Dettagli: [docs/verifica.md](docs/verifica.md).

---

