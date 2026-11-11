# Piano di ottimizzazione — chiuso

Il lavoro di pulizia/ottimizzazione di `sync-video` è finito: tutte le voci
proposte sono state fatte o hanno una risposta esplicita. Questo file resta
come registro delle **decisioni**, non delle operazioni — i dettagli di ogni
cambiamento sono nei messaggi di commit e nei commenti del codice.

Ultimo aggiornamento: batch 10 (la prova sul materiale reale), dopo il batch 5
(audit completo del codice: 6 difetti corretti e 7 ottimizzazioni misurate) e la
chiusura del piano (P2 #11 archiviata come non consigliata, primo avvio automatico
di Python su Windows).

## Batch 5 — audit completo (2026-11)

Un audit del codice letto riga per riga (con misure sui dati reali in
`.cache/`) ha trovato 6 difetti e 7 ottimizzazioni. Le correzioni hanno un
test che le copre; le ottimizzazioni sono misurate sul run di riferimento
(OCR 8 s / trascrizione 207 s / sync 80 s / video 88 s). Sono i tempi DEL PRIMO
batch: da allora la sincronizzazione e' scesa a 3-8 s quando tutte le
transizioni sono ancorate e il video non viene piu' ricodificato (71 s -> 43 s).

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

## Batch 6 — le voci aperte, chiuse (2026-11)

Le cinque voci lasciate aperte dal batch 5 sono state fatte. Ognuna ha un
test che la copre.

**L'embedding dei blocchi quando tutte le transizioni sono ancorate.** Se OGNI
cambio di slide e' annunciato a voce, la timeline la fa il podcast: la DP
viene poi sovrascritta dalle ancore, quindi embeddare i 300+ blocchi non
posizionava niente (36 s su 79 s di sync, misurati). Ora, con tutte le
transizioni ancorate, si salta l'embedding dei blocchi e si costruisce la
timeline dagli stessi tempi pronunciati: **la timeline e' identica** (verificato
sulla run reale, file per file) e la sincronizzazione scende da 60 s a 3 s.
Si perde la MISURA del segnale (picco medio, concordanza), non la
sincronizzazione: il riepilogo lo dice ("i cambi di slide sono tutti quelli
annunciati a voce") invece di fare finta. Il controllo che il parlato dopo
l'annuncio parli davvero della slide annunciata resta, e il suo costo non
cambia (e' un'altra catena, molto piu' piccola).

**Le zone cieche non si ritrascrivono due volte.** Ogni zona ritrascritta ha
un'impronta del suo CONTENUTO; le impronte stanno nella cache della
trascrizione ricucita. Una zona con lo stesso contenuto non viene
ritrascritta (~25 s l'una risparmiati a ogni run): se una parola cambia,
l'impronta cambia e la zona viene rifatta davvero.

**Un solo ffmpeg per tutti i frame della verifica.** Fin qui era un ffmpeg
per segmento (decine di processi, decine di aperture del container). Ora si
aprono N input dello stesso video, ognuno con la sua seek, e si esce con N
file: i frame risultano identici bit per bit (verificato sul video di prova)
e il percorso uno per uno resta come ripiego.

**La cache della trascrizione non si perde quando cambia il nome della chiave.**
I parametri che determinano il testo sono ora dentro la cache: una cache con
gli stessi parametri ma un nome diverso vale quanto quella con il nome
giusto. Il 02/11 quel cambio di nome era costato 3 min 27 s di Whisper; ora
costa zero.

**Le run concorrenti non si pestano piu'.** Blocco esclusivo del sistema
operativo su `.cache/run.lock`: se una run e' gia' in corso sulla stessa
cartella, la seconda si ferma e lo dice. Il blocco e' del SO, quindi se la run
muore il blocco muore con lei (nessun file da pulire a mano); due run nello
stesso processo non si pestano e passano.

## Batch 7 — ottimizzazioni senza toccare la qualità (2026-11)

Regola del batch: **stesso risultato, meno lavoro**. Nessuna di queste voci
toca il riconoscimento (modello, DPI, campionamento dei confini): ognuna ha
un test che confronta l'uscita.

- **L'audio non viene piu' ricodificato.** Se la sorgente e' gia' un MP4/AAC,
  nel video viene **copiata** (bit-identica): niente ricodifica lossy (che
  peggiorava l'audio) e 10-25s risparmiati su un podcast da 20 minuti.
- **Il video gia' renderizzato si riusa.** L'impronta copre il contenuto
  delle slide, i tempi, il contenuto dell'audio e i parametri di rendering:
  se coincidono, il video e' identico byte per byte e non si ricodifica
  (71s risparmiati a ogni rilancio senza modifiche). Se il file di output e'
  stato toccato, si rifa. `--no-cache` disattiva il riuso.
- **I fotogrammi della verifica si conservano solo per i segmenti sbagliati.**
  Sono gli unici da guardare: 33 MB di PNG a ogni verifica risparmiati, il
  confronto identico (gli stessi pixel).
- **Il calcolo "quali pagine il podcast non nomina"** era O(parole × finestre):
  ogni parola viene tokenizzata una volta e messa nelle finestre che la
  contengono (due ricerche binarie al posto di una scansione). Stesso
  risultato.
- **Le slide si tokenizzano una volta sola** invece di due (il controllo dei
  segmenti anomali e quello della prima sezione li rifacevano).
- **Il numero di versione non interroga piu' git due volte** (un processo per
  volta).
- **La cache non crea piu' la cartella in lettura** (una `mkdir` a ogni
  lettura, e una cartella che spunta per un file che non c'e').
- **La cache degli embedding si pota per byte e per eta'**, non per numero di
  file: una voce da 386 testi pesa 1,5 MB e una da 15 testi 48 KB, e contando
  i file si potava per prima proprio la voce che costa di piu' da ricalcolare.
  Tetto: 300 MB e 30 giorni.

## Batch 8 — qualita' del riconoscimento (2026-11)

Il riconoscimento e' il cuore del progetto: qui si aggiungono due cose che
lo rendono **piu' preciso**, non piu' veloci.

**"Passiamo alla pagina 3" diventa un confine misurato.** La grammatica delle
ancore conosceva "slide N" e "diapositiva N" (piu' le varianti fonetiche
che Whisper produce). Se il relatore dice "pagina", quei confini non venivano
misurati: venivano *stimati* dal contenuto, con la durata incerta. Ora
"pagina N" e' riconosciuto — **solo con un verbo di transizione vicino**
("passiamo alla pagina 3"): "pagina" e' anche la parola con cui si cita una
pagina di un libro ("a pagina 3 del libro"), e un confine sbagliato sposta
la slide di minuti. Il numero di una frase "pagina N" non viene piu'
agganciato alla parola "slide" che viene dopo (difetto preesistente).

**La trascrizione si corregge con il vocabolario del deck.** I nomi propri
storpiati dall'ASR si correggevano con un dizionario scritto a mano (6 nomi di
un deck solo). Ora il vocabolario viene dal **deck stesso**: le parole
iniziali maiuscole dell'OCR che ricorrono piu' volte sono i nomi propri, e
una parola della trascrizione che nel deck non c'e' e sta a una lettera di
distanza da uno di essi viene corretta. Sicurezza: una parola gia' nel deck
non si tocca, una parola a distanza 1 da DUE nomi del deck resta com'e' (e'
ambigua), e una maiuscola che compare una volta sola non entra (e' l'inizio
di una frase). Misurato sul materiale di esempio: **33 nomi corretti** (il
dizionario a mano ne copriva 6) e **timeline identica** (i confini vengono
dalle ancore, quindi le correzioni non li spostano).

**Il tool di verifica** (`analysis_sync.py`, quello che si lancia a mano dopo
la run) estrae i frame con una sola invocazione di ffmpeg invece di una per
frame.

**Il programma dice il DPI effettivo.** Il tetto di 16 megapixel per pagina
abbassa in silenzio il dpi (300 richiesti -> 280 su un deck 16:9), e
`--dpi` sembrava fare una cosa e ne faceva un'altra. Ora lo dice. Il rendering
NON cambia: e' solo trasparenza.

## Batch 9 — le ancore senza numero, il report, e il blocco che aspetta (2026-11)

**"Passiamo alla prossima slide" diventa un'ancora.** La grammatica delle
ancore chiedeva un **numero**: chi non lo diceva ("passiamo alla prossima")
si ritrovava con quei confini *stimati* dal contenuto invece che *misurati*.
Ora il numero implicito vale "l'ultima slide menzionata + 1", con tre condizioni
insieme: un aggettivo ("prossima/successiva"), la parola slide/diapositiva/
pagina accanto, e un verbo di transizione vicino. Senza il verbo, "la
prossima pagina del capitolo" non e' un cambio di slide. Il numero implicito
e' nella numerazione del RELATORE (che puo' essere sfalsata rispetto al deck:
la correzione del mapping sposta poi tutte le ancore dello stesso delta).

**Il report della run racconta la run.** In `sync_report.json` finiscono
adesso: le **correzioni dei nomi** fatte (quali e quante), il **DPI effettivo**
del rendering e i **tempi per fase**. Con la cache e i tempi in un posto solo
si puo' capire perche' una run e' stata lenta senza rileggere i log.

**Il tool di revisione (`--review`) estrae i fotogrammi in blocco.** Le
anteprime dei confini dubbiosi costavano due ffmpeg per dubbio: ora e' una
sola invocazione per tutti.

**Il blocco delle run concorrenti aspetta invece di fermarsi.** Se un'altra run
e' gia' al lavoro sulla stessa cartella, la seconda ASPETTA (un quarto d'ora):
di solito e' un rilancio mentre la prima finisce, non due run in competizione.
Il blocco e' del sistema operativo, quindi quando la prima finisce (o muore)
il blocco si libera da solo e la seconda parte.

Verifiche: 1110 test verdi (5 nuovi), ruff e mypy puliti su tre piattaforme,
run reale con **timeline identica**.

## Batch 10 — la prova sul materiale reale (2026-11)

La suite (1110 test) e' **tutta finta**: l'OCR di un PDF finto, la
trascrizione di un audio finto, un video costruito al volo. Le cose che
contano — le ancore, i confini, il riconoscimento — si verificano solo sul
materiale vero, e fino a ieri lo verificavo a mano (una volta sola).

`src/scripts/prova_materiale_reale.py` rende la verifica **ripetibile**: lancia
la pipeline sul PDF e sull'audio della cartella e confronta la timeline con un
riferimento (file golden, versionato nel repo). Esce con 0 se la timeline e'
identica, con 1 se un confine si e' spostato (e dice di quanto). Se il
materiale non e' quello del riferimento lo dice e non confronta: confrontare
materiali diversi non significa niente.

Da questo momento ogni batch di qualita' puo' essere provato **sul deck
vero**, non solo sui test finti. Il riferimento (hash del materiale, numero di
ancore, i 15 segmenti) diventa la documentazione viva di cio' che il programma
deve produrre.


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