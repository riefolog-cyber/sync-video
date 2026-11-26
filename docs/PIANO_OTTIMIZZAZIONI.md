# Piano di ottimizzazione — chiuso

Il lavoro di pulizia/ottimizzazione di `sync-video` è finito: tutte le voci
proposte sono state fatte o hanno una risposta esplicita. Questo file resta
come registro delle **decisioni**, non delle operazioni — i dettagli di ogni
cambiamento sono nei messaggi di commit e nei commenti del codice.

Ultimo aggiornamento: batch 32 (checkpoint, correzioni e diagnostica mista),
dopo il batch 31. Il piano resta un registro delle decisioni: i dettagli sono
nei messaggi di commit e nei test.

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


## Batch 11 — la revisione del codice (2026-11)

Una revisione delle ultime quattro batch ha trovato un difetto che rompeva la
suite — il prompt NotebookLM riformattato: le cancellette con due spazi in più
non venivano più riconosciute, quindi `test_prompts` non trovava nessun blocco
da incollare e 6 test fallivano — e sei problemi di comportamento. Corretti
tutti, i problemi hanno un test che li copre.

- **Il blocco delle run ha tre esiti.** "Occupato" (un'altra run al lavoro: si
  aspetta) e "non creabile" (cartella in sola lettura, ACL, disco pieno) erano
  lo stesso valore: il secondo faceva aspettare un quarto d'ora e poi
  abortiva, contro il commento del modulo ("meglio una run che può pestarsi
  che una run che non parte"). Ora una run che non può creare il file di
  blocco parte lo stesso, con un avviso.
- **Il blocco si libera quando la run finisce.** `main()` non lo rilasciava: per
  gli script che la chiamano nello stesso processo il lock sopravviveva alla
  run, e un rilancio da un'altra console aspettava 15 minuti per un lavoro già
  finito. La suite lo segnalava come `ResourceWarning: unclosed file
  run.lock`. Ora il blocco è legato alla vita dell'oggetto, e il file non
  cresce più di una riga per run (la scrittura in `a+` aggiungeva un PID a
  ogni avvio).
- **La potatura degli embedding agisce sulla cartella indicata.** Prima
  contava i file in una cartella e ne potava un'altra: `test_cache_maintenance`
  potava la cache vera e riportava sempre 0, quindi il test non poteva
  accorgersene.
- **L'attesa stampa un avviso al minuto**, non uno ogni 2 secondi (erano 450
  righe identiche in un quarto d'ora).
- **Il riuso del video include i thread** nell'impronta (finiscono nei flag di
  libx264, quindi un video con un altro numero di thread non è garantito
  identico), e l'audio copiato ha un ritentativo in AAC: un codec non ammesso
  in MP4 non lascia più la build senza via d'uscita.
- **L'indice dei timestamp si costruisce una volta** invece di una per ancora:
  `words_in_window` ricostruiva la lista dei tempi (e ricontrollava l'ordine) a
  ogni chiamata, quindi il guadagno promesso dal docstring non c'era.
- **La prova sul materiale reale distingue "non verificabile" (uscita 2) da
  "timeline cambiata" (1).** Il materiale non è versionato: su un altro
  checkout la prova diceva "materiale diverso" e usciva 1, come se il codice
  fosse rotto.
- **Le dipendenze di sviluppo stanno in `requirements-dev.txt`**: chi usa il
  programma non installa più mypy e ruff. `controlli.py` lo dice se mancano.

Come si verificano: `python -m ruff check .`, `python -m mypy src`,
`python -m unittest discover -s src -p "test_*.py"`.

## Batch 12 — revisione delle revisioni (2026-11)

Una seconda revisione del codice appena scritto ha trovato un difetto che
rompeva la suite (il conteggio dei test nelle doc, che non era stato aggiornato)
e una quindicina di problemi di comportamento. Ognuno ha un test che lo copre;
le correzioni più importanti sono quelle che facevano leggere dati sbagliati
senza avvisare.

**La cache degli embedding non perde più i vettori riusati.** Il riuso parziale
funzionava solo se mancava almeno un testo: quando la lista era cambiata ma
TUTTI i testi erano già in cache, il `mancanti=[]` (falso) faceva ricalcolare
tutto da zero. Inoltre la potatura a fine run non veniva mai chiamata (la
soglia di 20 scritture non scatta con le 3-18 voci di una run), quindi i tetti
di 300 MB e 30 giorni erano codice morto; ora `main()` la chiama una volta in
`finally`. L'indice del riuso non si ricostruisce più a ogni scrittura, e il
taglio per byte non butta una voce recente per tenere una vecchia piccola.

**L'ancoraggio completo non dipende più dal numero di blocchi.** Se OGNI
transizione è annunciata, la timeline viene dai tempi pronunciati e i blocchi
non servono; il vecchio controllo "blocchi < slide" bloccava proprio il caso
che il percorso tutto-ancorato doveva risolvere (trascrizione breve).

**`--review` rilegge la trascrizione giusta.** `main()` salva la chiave
effettiva in cima al report, ma `--review` la cercava dentro `anchors`:
trovava sempre `None` e prendeva la trascrizione più recente per mtime, che
dopo un escalation del beam non è quella della timeline. Il formato del test
è stato allineato a quello reale; i report vecchi restano leggibili grazie al
fallback annidato.

**L'aggiornamento accurato non scambia testo e parole disallineati.** Una
voce di cache vecchia poteva contenere il testo senza `words_raw`: il testo
veniva sostituito mentre i tempi restavano quelli veloci, e il confronto A/B
finiva per confrontare la trascrizione veloce con se stessa. Ora quella voce
viene trattata come mancante e testo e parole si scambiano insieme.

**Il PC piccolo viene avvisato anche con Whisper tiny.** L'avviso di RAM
taceva se l'utente aveva scelto `WHISPER_MODEL=tiny`, ma il modello da 4,3 GB
che occupa la RAM è quello di embedding, che resta invariato. Ora il silenzio
scatta solo se si è scelto il modello di embedding leggero.

**Spazio disco e referto usano la stessa fonte.** `verifica_pc` controllava
"small" scritto a mano e la radice del progetto come destinazione embedding;
ora usa `DEFAULT_WHISPER_MODEL`, la vera cartella dei modelli e la stessa
ripartizione per disco della guardia di bootstrap. Su dischi diversi ciascuno
deve ospitare solo il proprio modello, non il totale di entrambi.

**I comandi di ispezione funzionano su un PC vuoto.** Saltare pip per
`--version`/`--help` faceva morire l'import con `ModuleNotFoundError` prima di
poter stampare la versione: ora l'installazione viene saltata solo se i moduli
pesanti sono già presenti. `--review` non è più classificato come comando
senza effetti (scrive report e anteprime e usa ffmpeg).

**Altre correzioni.** `--require-full-anchors` non ferma più un deck di una
slide (zero transizioni da annunciare); `--strict-sync` con `--dry-run` o
`--preview` avvisa che la verifica dei frame non gira; i valori non finiti e
quelli fuori range arrivano rifiutati dall'argparse e dal `.env` invece che
dopo il bootstrap; `controlli.py` trova `_debug_hardware.py` anche se lanciato
da `src/`; la cache dell'encoder usa un file temporaneo unico; il blocco run è
anche un context manager e conta i proprietari in modo atomico fra thread.

Verifiche: `ruff` e `mypy` puliti, 1500 test verdi (8 nuovi). La verifica su
questo PC segnala ancora un KO `Motore` per OpenVINO non utilizzabile: è
l'ambiente della macchina di sviluppo, non una regressione del codice. Il KO
è stato riclassificato come AVV nel batch 13 (il ripiego su CPU funziona).

## Batch 13 — revisione degli artefatti e della diagnostica (2026-11)

Una terza revisione ha coperto le aree non toccate dalle due precedenti
(video, referto, aggiornamenti, OCR, diagnostica hardware) e ha riesaminato il
batch 12 trovando un'ottimizzazione che in realtà non funzionava.

**L'indice degli embedding non si ricostruisce più davvero a ogni scrittura.**
Nel batch 12 la mtime della cartella veniva letta DOPO `np.savez`, che l'aveva
appena cambiata: il confronto con la mtime salvata falliva sempre e l'indice
veniva rifatto intero. Ora la mtime si legge prima della scrittura, e un test
verifica che scrivere una seconda voce non riapra gli npz già indicizzati.

**La verifica dei frame non mente più.** Un frame non estratto, o una slide
fuori dal deck, veniva saltato in silenzio: `incomplete` restava False e
`--strict-sync` non si fermava, pur con segmenti mai controllati. Ora ogni
segmento non verificato finisce in `skipped` e la verifica è incompleta.

**Il riuso del video dice l'encoder vero.** La marca di riuso ora salva
l'encoder EFFETTIVO della run che ha scritto il file: dopo un ripiego da GPU a
CPU, un rilancio che riusa il video non può più dichiarare "GPU" nel riepilogo.

**Il controllo aggiornamenti non si blocca su pydub.** Le wheel pure-python
con `requires_python: null` (pydub 0.25.1) venivano scartate: pydub risultava
"irraggiungibile" a ogni avvio e la cache del controllo non veniva mai scritta.
Ora una wheel senza vincolo è accettata, come fa pip.

**Il referto non dà più falsi allarmi.** Un PC sano con OpenVINO installato ma non
utilizzabile ripiega su CPU e produce il video: era segnalato KO ("problema che
impedisce il video") ed è ora AVV con rimedio esplicito. Analogamente, ffmpeg
non dice più "accelerazione hardware disponibile" solo perché i tre encoder
sono compilati: la scelta passa da `choose_video_encoder`, che incrocia la GPU
vera. Mac Intel usa VideoToolbox (QSV non esiste su macOS).

**Diagnostica e tool affidabili.** `_debug_hardware.py` patchava
`hardware.ffmpeg_encoders` invece di `machine_setup.ffmpeg_encoders` (gli
scenari non simulavano niente) e confrontava il tier RAM con se stesso;
`controlli.py` uccideva il passo a 180s prima dei tre mypy da 300s;
`--no-update-check` ora dice nell'help che salta anche il bootstrap.
`analysis_sync.py` non si rompe più all'import con un audio dal nome diverso
da `podcast.m4a` o con parole raw senza `end`.

**Altre correzioni.** Temporaneo di rendering con nome unico (due run non si
pestano); durate nulle clampate a un frame invece di abortire l'encoding;
lettura cache transcript protetta da file che spariscono; token Whisper con
spazi non più buttati via; OCR misto (pagine native + pagine da immagine) con
Tesseract assente non passa più in silenzio; riepilogo difensivo su valori
sporchi; venv A/B sempre rimossa; CI notturna con job di riscaldamento cache
perché le tre versioni non-3.12 non skippino silenziosamente l'E2E.

Verifiche: `ruff` e `mypy` puliti (anche win32/darwin), 1508 test verdi.
Il batch 14 porta la suite a 1512 test.

## Batch 14 — contratti e diagnostica (2026-11)

Quarta revisione: nessun difetto spettacolare, ma diversi contratti scritti
che non venivano rispettati.

**Il riuso del video diceva ancora l'encoder sbagliato.** Il batch 13 salvava
`encoder_out[0]`, ma `_build_video_ffmpeg` accoda OGNI tentativo: con un
ripiego GPU→CPU il primo e' quello fallito. Ora si legge l'ULTIMO, come fa il
riepilogo, e un test percorre la catena completa build→marca→riuso.

**Mapping automatico e dubbi sui confini.** Dopo la correzione automatica del
numero di slide (copertina non numerata) il controllo annunci/confini usava
ancora offset 0: segnalava come sbagliate tutte le slide appena corrette. Ora
si calcola lo spostamento applicato (accoppiando le ancore per tempo) e lo si
passa al controllo; se la correzione non e' un offset unico il controllo viene
saltato invece di mentire.

**Offset negativo.** Le ancore venivano estratte e filtrate nello spazio PDF
prima di applicare `--slide-offset`: con il relatore che conta una pagina in
piu' (offset -1), l'ultimo annuncio "slide 15" veniva buttato e la slide finale
restava stimata. Ora estrazione e filtro usano lo spazio parlato.

**Dipendenze e diagnostica.** I minimi di requirements.txt (pymupdf, pytesseract,
pydub, moviepy, tqdm) ora sono dichiarati anche nel bootstrap, altrimenti un
pacchetto vecchio ma importabile non veniva mai aggiornato; i marker ARM
coprono anche `arm64` minuscolo di Apple Silicon; il rimedio CUDA non ha piu'
un punto attaccato al comando copiabile; il referto non da' piu' un KO falso
quando OpenVINO ripiega su CPU.

**Tool e documentazione.** `_debug_hardware.py` non pretende piu' di trovare
una NVIDIA su macOS; `controlli.py` lascia finire i mypy cross-platform;
`prova.bat` rispetta `--no-pause` anche sul fallimento; l'help di
`--no-update-check` dice che salta il bootstrap; la CI Windows verifica il
binario Tesseract e la CI notturna ha un job di warmup della cache. I documenti
che ancora parlavano di MoviePy, di 40 voci di cache, di `--review` come
comando di sola lettura e di `WHISPER_MODEL=tiny` come alleggerimento dei pesi
sono stati allineati al codice.

## Batch 15 — motori e casi limite (2026-11)

Quinta revisione, concentrata sulle parti mai lette riga per riga: motore
semantico, trascrizione, ancore e finestratura.

**Le chiavi di cache non collidono più.** `_embed_cache_key` usava un semplice
separatore: `["a", "b"]` e `["a\x01b"]` producevano la stessa chiave. Ora ogni
testo è preceduto dalla sua lunghezza. La riusata integrale, inoltre, non
riscrive più la voce con `seconds=0` (che faceva dire "0s risparmiati" su una
cache che aveva fatto risparmiare minuti).

**Verifica ancore più onesta.** Un'ancora con numero oltre le slide effettivamente
embeddate non può essere verificata: prima produceva comunque un offset negativo
dall'argmax di una matrice di zeri, avvelenando `unconfirmed` e l'inferenza del
derive. Ora viene saltata; con similarità tutte nulle resta nel conteggio ma non
propone spostamenti. `segment_verdict`, `_window_words`, `_clamp_monotono` e la
finestratura delle parole hanno ricevuto gli stessi guardi difensivi di
`TimeIndex` (parole non ordinate, indici fuori deck, slide 1 non a zero).

**Trascrizione.** Le due prove (veloce/accurata) ora hanno chiavi di cache
distinte anche con OpenVINO, che ignora il beam: prima la "accurata" rileggeva
il testo greedy e il report mentiva. `auto` verifica che il device OpenVINO
esista davvero prima di sceglierlo, invece di morire con "device not found" su
un PC con il modello IR ma senza iGPU. ffmpeg non eredita più la console
(`-nostdin` + stdin devnull), il fallback pydub forza il PCM a 16 bit e
`--no-ripresa` vale anche per la seconda decodifica.

**Ancore e annunci.** Il controllo annunci/confini applica l'offset PRIMA del
filtro di portata (prima "slide 15" con offset -1 su 14 pagine veniva scartata),
riconosce anche la forma "quattro ... alla slide" e deduplica gli annunci ripetuti.
I conteggi ("le 2 slide del documento") non alzano più il numero massimo e non
rubano la transizione vera. Il recupero dell'ultima transizione usa il numero di
pagine del deck, non l'ultima ancora nota. Decimali e intervalli ("1.5", "3-4")
non diventano più slide 15 e 34.

Verifiche: `ruff` e `mypy` puliti, 1523 test verdi.

## Batch 16 — sicurezza delle risorse e piattaforme (2026-11)

Sesta revisione trasversale: gestione delle eccezioni, costi ripetuti e
portabilità. Nessun difetto spettacolare, ma diversi modi in cui un errore
poteva lasciare il PC in uno stato peggiore di prima.

**Niente più processi orfani.** Il converter PPTX→PDF ora nasce in un gruppo
di processi nuovo e, su timeout, uccide l'intero albero (PowerPoint COM,
Electron, x2t), non solo il figlio diretto; il drenaggio dei pipe ha un tetto.
Gli stessi guardi sono arrivati a `_run_ffmpeg`, al drain di `controlli.py`
e al `taskkill` su Windows (prima un exit !=0 non attivava il ripiego).
La venv temporanea del test A/B, infine, viene rimossa in un `finally` anche
se il confronto o la scrittura del report falliscono.

**Scritture atomiche e cleanup.** Anche i `.npz` degli embedding ora usano
un temporaneo unico + `os.replace`, come tutte le altre cache: un crash non
lascia più un file troncato al nome canonico. `audio_clip.close()` è protetto
in entrambi i `finally`, così un errore di chiusura non maschera quello della
run né salta la potatura cache. Il documento PyMuPDF usa `with`, e la
preparazione del file di blocco non viene più scambiata per "run occupata".

**Portabilità.** Rilevamento GPU con `powershell`/`pwsh` come in OCR, output
dei subprocess decodificato come UTF-8 esplicito, pip con `text=True`, probe
Tesseract che cattura ogni `OSError`. Il fallback `.ppt/.pptx` usa
`Path.with_suffix()` invece di `rsplit(".")`, che su percorsi con un punto in
una cartella (`C:\Users\m.rossi\...`) cercava nel posto sbagliato.

**Costi e regressioni.** La correzione dei nomi propri ora costruisce un
indice delle modifiche a un carattere una volta per vocabolario invece di
confrontare ogni parola con ogni nome (2.6s → molto meno su 20k parole).
Corretti inoltre il fallback pydub per il PCM 8 bit (il silenzio diventava
un tono a -1.0), i conteggi che rientravano nelle nuove grammatiche di annuncio e
coda, e diversi test che passavano senza esercitare la correzione.

Verifiche: `ruff` e `mypy` puliti, 1527 test verdi (il batch 17 porta la suite a 1528).

## Batch 17 — cache vive e verità dei messaggi (2026-11)

Settima revisione: il codice funzionava, ma diversi messaggi e diverse cache
non dicevano la verità su cosa fosse successo.

**Cache che andavano perse.** `pin_overrides.json` (il pin aggiornato dopo un
A/B approvato) non era nella lista delle chiavi housekeeping: la pulizia di
fine run lo cancellava e il bootstrap successivo rimetteva il vecchio pin,
disinstallando l'aggiornamento. Stesso destino per la cache delle zone cieche
in `--clean-cache`, che non era nelle chiavi attive: costava ~25s per zona alla
run dopo. La parziale di trascrizione, inoltre, non includeva device e compute
type: un parziale CUDA/float16 poteva essere cucito con una coda CPU/int8.

**Messaggi veritieri.** `--dry-run` non stampa più "GPU" per un encoder mai
usato; il riepilogo usa la stessa cifra della console per il tempo di
sincronizzazione (A/B incluso); `--review` senza report esce con codice 1;
`--prefetch-models` e la pagina di primo avvio non dicono "tutti i modelli
pronti" se un download è fallito; l'aggiornamenti non dice "tutti aggiornati"
subito dopo "PyPI irraggiungibile". Il tempo di encoding non appare più quando
il video è stato riusato, e il riepilogo thread/batch usa i valori CLI reali.

**Altre correzioni.** `pip check` non ignora più un conflitto stampato su
stdout; la marca di riuso video non può più far fallire una run riuscita per
una cache in sola lettura; lo stato di ripresa si azzera tra due `main()` nello
stesso processo; i temporanei degli embedding orfani vengono potati; la chiusura
del file di blocco non rilancia l'OSError che aveva già fatto degradare la run.
Documentazione allineata su codici di uscita, blocco concorrente (errore, non
avviso) e cartella `verify_frames` vuota (non prova di successo se la verifica
era incompleta).

Verifiche: `ruff` e `mypy` puliti, 1528 test verdi.

## Batch 18 — CI notturna (2026-11)

La CI notturna (`SYNC_VIDEO_RUN_SLOW=1`) era rossa su tutte e quattro le
versioni Python per due test lenti, mentre i job veloci erano verdi:

- il test E2E `--no-ripresa` cercava ancora il vecchio testo del messaggio
  ("decodifico da capo"), cambiato nel batch 16 in "ignoro il parziale";
- il test lento di conversione PPTX→PDF scriveva un PDF finto a byte, che
  `_pdf_valido()` (PyMuPDF) scartava: il test finiva per invocare il converter
  vero, assente su CI. Ora scrive un PDF minimo valido;
- riabilitando il parziale con device/compute nel batch 17 è emerso un
  doppione al giunto della ripresa (57 → 58 parole): `_collect_words`
  confrontava solo i timestamp. Ora scarta anche le parole già salvate con
  stesso testo entro 0.6s.

Verifiche: **1528 test verdi anche con `SYNC_VIDEO_RUN_SLOW=1`** (job nightly),
oltre ai 6/6 controlli del job veloce.

## Batch 19 — revisione trasversale (2026-11)

Ottava revisione, condotta in parallelo su cinque aree (cache, CLI, video,
timeline, diagnostica). Nessun difetto spettacolare, ma ventinove modi in cui
il programma diceva una cosa e ne faceva un'altra, o perdeva lavoro gia'
fatto. Ventuno test nuovi coprono i percorsi principali.

**Cache che non si perde e controlli che non uccidono la run.**
`--clean-cache` calcolava le chiavi da tenere PRIMA di bootstrap/machine_setup:
su una macchina CUDA la trascrizione viva della run precedente veniva cancellata
come orfana e il log diceva "conservata". Ora si conservano tutte le
trascrizioni dell'audio corrente, qualsiasi sia il decodifice scelto. Il
segnale del tentativo OpenVINO, la cache degli aggiornamenti e la sua
invalidazione dopo un upgrade sono diventate best-effort come tutte le altre
cache: una cartella in sola lettura non ferma piu' la pipeline prima che
cominci. Una cache scaduta con PyPI non raggiungibile ristampava aggiornamenti
gia' installati: le voci si rivalidano anche su quella strada. La pulizia
conteggiava i byte dei modelli e i frame anche quando la cancellazione era
fallita, e il A/B scriveva il referto senza temporaneo.

**Comandi che dicono il vero.** `--whisper-device`/`--openvino-device`
accettavano qualsiasi stringa (un refuso moriva dopo OCR e download): ora la
validazione e' immediata e la case viene normalizzata, anche per il `.env`.
L'handler di `--log-file` sopravviveva a una seconda `main()` nello stesso
processo e continuava a scrivere nel file vecchio: ora si rimuove a ogni
chiamata. `VIDEO_ENCODER=auto` annunciava "GPU accelerata" prima che l'encoder
fosse scelto. Il riepilogo diceva "video non generato (dry-run)" anche quando
il video era stato riusato (stessi argomenti, `t_video=0`). Il referto su Mac
diceva "nessun encoder accelerato" pur usando VideoToolbox.

**Video, frame e anteprime.** Il percorso MoviePy scriveva direttamente sul
file finale: un encoding interrotto lasciava un MP4 troncato e il backup del
video buono non veniva ripristinato. Ora anche MoviePy usa temporaneo unico +
`os.replace` e ripristina in caso di errore. Una slide non leggibile che nessun
segmento usa rendeva incompleta la verifica e abortiva `--strict-sync` con
"0 segmenti non verificati". Le anteprime dei confini dubbi si accumulavano
quando la run dopo aveva meno dubbi della precedente. `analysis_sync.py`,
fermato a una timeline di un altro deck con un `IndexError`, ora esce 2
("non verificabile"); un frame non estratto non diventa piu' "slide 1"
dall'argmax di un vettore di zeri; i denominatori contano solo cio' che e'
stato davvero guardato; il fallback ffmpeg controlla il codice di uscita e
non lascia PNG parziali.

**Timeline e diagnostica.** L'aggancio alle pause poteva accorciare l'ULTIMA
slide sotto il minimo di leggibilita' (nessun vicino dopo): ora la fine
dell'audio fa da successivo. Le menzioni implicite ("prossima slide")
duplicavano tutte le menzioni esplicite, gonfiando i conteggi del report. Il
riuso parziale degli embedding scriveva `seconds≈0`, facendo dire "0s
risparmiati" su una cache che aveva fatto risparmiare minuti. `diagnostica.py`
leggeva `.cache` scritto a mano invece della cache rediretta; un `ts`
non numerico in `hardware.json` faceva morire il probe degli encoder a
startup; una slide con poco testo nativo ("Domande?") bloccava la run senza
Tesseract anche se il PDF era digitale. Il controllo "batch su RAM" e
l'encoder di `_debug_hardware.py` confrontavano formule con se' stessi: ora
leggono la configurazione e le scelte reali.

Verifiche: `ruff` e `mypy` puliti (90 file, win32/linux/darwin), 1549 test
verdi (21 nuovi), `_debug_hardware.py` e controlli 6/6.

## Batch 20 — revisione delle revisioni (2026-11)

Quarta revisione incrociata: prima si e' guardato cio' che il batch 19 aveva
lasciato indietro, poi le aree mai lette riga per riga (aggiornamenti,
sottotitoli, riuso video, pulizia cache). Quattordici difetti, quindici test
nuovi. Nessun cambiamento nella qualita' del riconoscimento.

**Aggiornamenti che raccontano la verita'.** `pip check` nomina chi RICHIEDE
una dipendenza, non il pacchetto aggiornato: guardare solo l'inizio della riga
faceva passare come sano un upgrade di pillow che rompeva fastembed, e
dichiarava fallito un conflitto senza pacchetti aggiornati. Ora conta ogni
pacchetto aggiornato che compare nella riga. "✅ Installati" veniva stampato
PRIMA del health check, quindi un upgrade fallito mostrava un successo verde:
ora si stampa solo dopo. Lo script `aggiornamenti.py` usciva 0 anche quando
l'utente accettava un aggiornamento e pip falliva: ora ritorna 1 (la pipeline
principale, che non deve mai fermarsi per un aggiornamento, ignora il valore).
Il messaggio di bootstrap elencava TUTTI i pacchetti, compresi quelli appena
installati con ✅: ora solo i falliti veri.

**File prodotti e materiali.** `--output video.v2.mp4` scriveva `video.srt`
invece di `video.v2.srt`: due video con lo stesso nome a parte si
sovrascrivevano i sottotitoli. Ora si toglie solo l'ultima estensione. Un solo
timestamp fuori ordine faceva invertire un cue e `build_srt` lo scartava:
l'intero file .srt spariva senza avviso; le parole vengono ordinate per tempo.
Due campi sporchi nel report (`slide: "x"`, `ripresa_da: "n/d"` o `Infinity`)
cancellavano l'intero riepilogo: ora usano gli stessi convertitori difensivi
gia' usati altrove. `find_audio_file` e `find_presentation_file` non avevano
il guard `OSError` degli altri helper: una cartella sparita faceva morire gli
script diagnostici.

**Riuso e pulizia.** L'impronta del video hashava i byte dell'audio ma non
l'estensione: rinominare `podcast.m4a` in `.wav` (stessi byte, ma copia AAC
contro ricodifica) riusava il video sbagliato (versione v4 dell'impronta).
`segna_video` sopprimeva l'errore di scrittura e lasciava la marca del video
precedente: ora la rimuove e avvisa. `--clean-cache` azzerava `frame_check`
anche quando NON aveva rimosso frame (il verificatore conserva solo i PNG dei
segmenti sbagliati): l'unica prova che il video era stato controllato spariva.
Il conteggio "parziali vecchi" contava i file della coppia `.json` +
`_resto.flac` come due parziali. La copia dei modelli Tesseract di sistema non
era atomica: un taglio a meta' lasciava un `.traineddata` troncato che non
veniva mai riscritto. Infine il controllo encoder del `_debug_hardware.py`
del batch 19 dava KO su ogni GPU quando `VIDEO_ENCODER` era impostato nel
`.env` (scelta documentata, non regressione): la macchina simulata ora non ha
override.

Verifiche: `ruff` e `mypy` puliti (91 file, win32/linux/darwin), 1564 test
verdi (15 nuovi), `controlli.py` 6/6 e debug hardware 17/17.

## Batch 21 — orchestrazione e strumenti (2026-11)

Undicesima revisione: si e' letta l'orchestrazione di `main.py` da cima a
fondo, la trascrizione di ripiego, la diagnostica hardware e gli script che
l'utente lancia a mano (compresi i `.bat`). Ventuno difetti, sedici test
nuovi. Anche qui una correzione del batch precedente (ricerca dei materiali)
non era completa.

**Report e anteprime coerenti con il video.** Dopo l'auto-riparazione dei
confini il report, il riepilogo e le anteprime parlavano ancora del video
VECCHIO, mentre sottotitoli e capitoli usavano quello nuovo: i dubbi vengono
ora ricalcolati sulle durate corrette. `--preview` tornava prima di scrivere
report e riepilogo: con `--skip-slides` il JSON descriveva il deck intero e
il giorno dopo `--review` sbagliava. Con ZERO dubbi le anteprime della run
precedente restavano sul disco (il caso "dubbi piu' numerosi" era coperto,
"nessun dubbio" no). `--prefetch-models` usciva 0 anche se tutti i download
erano falliti. La verifica frame non era cronometrata e, con riparazione, il
suo tempo finiva nella riga "Encoding Video": ora ha una voce propria e il
tempo di encoding misura solo l'encoding.

**Trascrizione e ricerca materiali.** Il recupero delle zone cieche
ri-encodingava la clip in AAC, cambiando proprio le parole dell'annuncio che
doveva recuperare: ora usa FLAC lossless come il taglio della ripresa. Su un
portatile ibrido NVIDIA+iGPU il log diceva "iGPU OpenVINO attiva" mentre la
decodifica girava su CUDA; il ripiego CUDA→CPU non avvisava della lentezza.
Un segnale di crash GPU restante faceva partire su CPU per sempre, anche
dopo reboot: ora si cancella quando una trascrizione arriva in fondo.
`find_audio_file`/`find_presentation_file` avevano il guard `OSError`
sull'intero ciclo: un solo file bloccato faceva dire "nessun audio" con il
podcast buono accanto (il test l'ha scoperto) — ora si salta il singolo
candidato.

**Setup e diagnostica.** Il primo avvio con CUDA non controllava che
faster-whisper fosse installato (lo fa solo la run successiva) e finiva in
cuda/float16 con ImportError dopo OCR. Il device OpenVINO veniva scelto prima
del provisioning: su iGPU Intel la prima run partiva in CPU. Il referto
consigliava di installare OpenVINO su PC senza iGPU, e diceva "OpenVINO
scelto" su un PC configurato con `TRANSCRIBER=whisper`; con un segnale di
crash aggiungeva DUE voci "Motore" contraddittorie.

**Launcher e script.** `genera_video.bat`, `prova.bat` e `pulisci.bat`
terminavano con `endlocal`, azzerando l'ERRORLEVEL: il fallimento veniva
stampato ma il codice di uscita era 0. `check_embedding_models.py` considerava
401/403/429 come "nessun aggiornamento", distruggeva la baseline e usciva 0,
e non passava timeout alle chiamate HuggingFace (blocco infinito).
`prova_materiale_reale.py` usciva 1 ("timeline cambiata") quando la timeline
non c'era affatto. `confronta-embedding.ps1` cercava la vecchia frase
"similarità media" e non trovava mai il punteggio, dicendo sempre "nessun
modello valutabile".

Verifiche: `ruff` e `mypy` puliti (92 file, win32/linux/darwin), 1580 test
verdi (16 nuovi), `controlli.py` 6/6 e debug hardware 17/17.

## Batch 22 — segnali, storico e qualita' dei test (2026-11)

Quinta revisione incrociata: il segnale di crash GPU, il prefetch, lo storico
tempi, `--review` e i test stessi. Venti difetti, tredici test nuovi (12 qui + 1 nel batch 21). Uno era
stato INTRODOTTO dal batch 21 e trovato subito dal controllo della suite.

**Il segnale di crash GPU e' di nuovo affidabile.** Il `finally` aggiunto nel
batch 21 cancellava il segnale di un ALTRO audio (il file e' unico in cache):
ora `_clear_attempt(audio_path)` lo tocca solo se l'identita' corrisponde. Il
ripiego deciso proprio da un segnale di crash tornava prima del cleanup e
condannava l'audio alla CPU per sempre: anche li' si pulisce a fine run.
`--transcriber openvino` (la modalita' in cui si forza la GPU) non scriveva
affatto il segnale: ora lo scrive. Cambiando `OPENVINO_MODEL_ID` nella stessa
cartella si riusava il vecchio IR con il nome del nuovo: la cartella ora
registra quale ID l'ha prodotta.

**Prefetch e tempi veritieri.** Se il modello principale manca ma il fallback
carica, il prefetch stampava "Embedding pronto" e usciva 1: ora il fallback
e' un successo. Con riparazione, il tempo del primo encoding e della seconda
verifica sparivano dalla tabella (il secondo valore SOSTITUIVA il primo): ora
si sommano, e il nuovo bucket "Verifica frame" finisce anche in
`timing_history.jsonl`. `--preview` scriveva un riepilogo che dichiarava un
video mai generato e saltava la pulizia delle anteprime a zero dubbi: ora
dice "non creato" e pulisce. `--clean-cache` con .pptx faceva l'hash del
PPTX invece del PDF convertito e cancellava la cache OCR viva.

**`--review` non rassicura a vuoto.** Con report vecchio e trascrizione
assente diceva "niente da rivedere" senza aver controllato: ora avvisa ed
esce 3. Il ricalcolo usava solo l'offset manuale, azzerando dubbi veri dopo
una correzione automatica del mapping: ora usa l'offset EFFETTIVO salvato
dalla run (e sui report vecchi con mapping corretto non dichiara un esito
pulito). Con slide ripetute takeva la prima occorrenza e piazzava l'anteprima
sul confine sbagliato: ora usa l'ultima. Il riepilogo scritto prima del video
restava senza data: ora usa quella della run.

**La suite si controlla da sola.** Il batch 21 aveva passato `timeout=` a
`HfApi.list_models()`, che non lo accetta: il watcher e5 usciva SEMPRE 3 e
nessun test lo vedeva perche' usavano API finte. Ora il timeout e' solo su
`model_info` e un test usa la firma reale. Un test del batch 19 era vacuo
(PyPI irraggiungibile faceva tornare prima della scrittura che doveva
provare): ora esercita davvero la correzione. `docs/verifica.md`, il
docstring di `prova_materiale_reale.py` e il commento della CI dicevano che
i test reali non esistevano: corretti. Il report A/B dello script standalone
era l'unica copia non atomica. Infine il confronto PowerShell non verificava
che il produttore del log emettesse ancora la frase che sa leggere.

Verifiche: `ruff` e `mypy` puliti (93 file, win32/linux/darwin), 1592 test
verdi (13 nuovi), `controlli.py` 6/6 e debug hardware 17/17.

## Batch 23 — capitoli e qualita' della prova (2026-11)

Sesta revisione incrociata, con un caso reale segnalato dall'utente: i titoli
dei capitoli dell'ultimo video erano pieni di errori ("L'inggtanno",
"cunosita'", "IEEARSI DIRLA", "SFFSITE"). Ventuno difetti, quattordici test
nuovi; tre test vecchi sono stati rafforzati perche' non esercitavano cio' che
dicevano.

**I capitoli non sono piu' solo l'OCR.** `build_capitoli` prendeva il titolo
dalla sola OCR della slide: su pagine infografiche l'OCR restituisce testo
"plausibile" ma sbagliato, e il vecchio filtro di qualita' lo lasciava passare.
Ora, quando c'e' la trascrizione, si usa la prima frase DETTA nella slide come
sorgente alternativa: il deck nasce dal podcast, quindi chi parla legge il
titolo vero. L'OCR buono resta prioritario; quello rumoroso (maiuscole a
tuoni, lettere isolate) viene scartato dal punteggio piu' severo. Il caso
"Slide N" rimane come ripiego perche' non c'e' un titolo leggibile.

**Regressioni del batch 22.** `--clean-cache` su un .pptx senza converter
sollevava `RuntimeError` non gestito e non puliva nulla; l'ID del modello
OpenVINO veniva controllato solo dentro il download, che i chiamanti saltano
quando la cartella e' completa, quindi il modello vecchio restava in uso con
il report che annunciava quello nuovo; il segnale di crash GPU veniva scritto
senza guardia e sovrascriveva quello di un altro audio; il ripiego CPU lo
cancellava anche quando la decodifica falliva. `--preview` ora marchia il
report e `--review` rifiuta di estrarre frame da un video vecchio seguendo
quei tempi; quando il controllo non gira dice la ragione vera.

**Mapping e diagnostica.** Due ancore sfasate separate da una confermata
venivano fuse in un'unica "run" e corrette come spostamento continuo; le
ancore con numero fuori dal deck rubavano il primo numero della frase dopo;
"prossima slide 4" con il 4 come ultima parola inventava la slide 5 nello
stesso istante. La diagnostica del refine usava come chiave ora il numero
slide ora l'indice di segmento: le due voci collidevano e il report perdeva
una slide. Lo script A/B scriveva nello stesso file di `updates.py` senza i
campi che lo validano, distruggendo il report buono e facendo ripartire il
test a ogni avvio; usava anche un `.tmp` condiviso. `_debug_hardware` lasciava
due cartelle in %TEMP% a ogni controllo, e il bundle diagnostico cercava solo
`video_finale.mp4` anche dopo `--output lezione.mp4`.

**I test si controllano da soli.** Tre test "di regressione" erano vacui:
quello della correzione dei nomi ricostruiva l'indice dentro il test quello
del motore faceva asserzioni negative su frasi che il codice non emette
quello del confronto embedding usava una riga inventata. Ora passano dalla
funzione di produzione. Il test del backup MoviePy non creava il backup, e
quello del riepilogo si aspettava l'assenza della data. Le doc dicevano che
tutti i test fossero finti e che `--prefetch-models` scaricasse anche l'IR
OpenVINO su ogni macchina: corretti.

Verifiche: `ruff` e `mypy` puliti (93 file, win32/linux/darwin), 1607 test
verdi (14 nuovi), `controlli.py` 6/6 e debug hardware 17/17.

## Batch 24 — capitoli, segnali e CI (2026-11)

Settima revisione incrociata. Ventitré difetti, undici test nuovi. Il primo
intervento e' stato pero' fuori dal codice: la CI del batch 23 e' morta per un
**504 di Chocolatey** durante l'installazione di Tesseract, non per una
regressione.

**CI resiliente.** Il passo Tesseract riprova tre volte e, se il feed resta
giu', usa winget con `--silent`; il controllo finale e' sul binario e sul suo
exit code, non sull'exit code di choco e non su una pipe di PowerShell che
avrebbe mascherato un binario rotto.

**I capitoli finalmente leggibili.** Il batch 23 correggeva solo gli errori
che diventavano "Slide N": le parole con errori minuscoli ("cunosita'" contro
"curiosita") passavano ancora, e i titoli ufficiali in maiuscolo (NASA, FAQ)
venivano distrutti dalla penalità. Ora il confronto usa la similarita' fra
parole OCR e parole dette, gli acronimi sono riconosciuti come titoli veri, e
l'aggiunta di lettere/spazi non richiede piu' una radice comune di 4 caratteri.

**Segnale GPU per audio.** Il file-segnale e' unico ma ora contiene PIU'
record: un crash su un podcast non impedisce a un secondo podcast di
registrare il proprio, e cancellare il proprio non tocca gli altri. Qualsiasi
eccezione Python sulla GPU (non solo Ctrl+C) cancella il segnale, perche' e'
un ripiego, non un crash nativo. Il marcatore dell'ID OpenVINO corrotto non
fa piu' crashare setup/prefetch.

**Riparazione e stato.** La finestra della riparazione guidata dai frame non
si estende piu' oltre il tempo del frame che la contraddice, e piu' mismatch
sullo stesso confine restringono la finestra invece di allargarla. La
diagnostica del refine tiene distinte le occorrenze della stessa slide
(`--skip-slides`). `--dry-run` marchia il report come "senza video" e
`--review` rifiuta di estrarre frame da un video vecchio. Con `--strict-sync`,
se la riparazione ha spostato i confini e la verifica resta incompleta, SRT,
capitoli e riepilogo vengono riscritti PRIMA dell'abort. I contatori di tempo
e il DPI effettivo si azzerano fra due `main()` nello stesso processo.

**Test che realmente verificano.** Cinque test di regressione dei batch
precedenti erano vacui: il fallback CUDA non raggiungeva il ramo CUDA su CI,
il controllo di `faster_whisper` non raggiungeva il suo ramo, l'ancora
implicita non veniva controllata, il cleanup del PPTX non verificava la
chiamata al converter, e la frase del riepilogo era scritta a mano nel test.
Ora passano dalla funzione di produzione, con i probe forzati.

Verifiche: `ruff` e `mypy` puliti (94 file, win32/linux/darwin), 1619 test
verdi (11 nuovi), `controlli.py` 6/6 e debug hardware 17/17.

## Batch 25 — capitoli, report e script (2026-11)

Ottava revisione incrociata. Ventiquattro difetti, undici test nuovi. La CI
del batch 24 e' stata verificata verde prima di partire.

**Capitoli: le correzioni del batch 24 avevano introdotto due regressioni.**
Il confronto di similarita' considerava le parole IDENTICHE come prova di un
errore OCR: il relatore che ripeteva il titolo ufficiale (NASA, BISOGNI
PRIMARI, "Prima") faceva sostituire quel titolo con un pezzo di narrazione.
Ora solo una parola DIVERSA ma quasi uguale ("cunosita'"/"curiosita") vale da
prova. D'altro canto la scorciatoia per gli acronimi faceva passare come
titoli anche SFFSITE, IEEARSI DIRLA, ST e RRAR: ora gli acronimi devono
essere plausibili (3-5 lettere, no sequenze di tre vocali/consonanti o
consonanti raddoppiate) e il rumore torna a "Slide N".

**Report e blocco.** Il report finale veniva riscritto solo con dubbi o
verifiche: nei run normali con `--skip-slides` restava su disco il deck
intero mentre SRT, capitoli e riepilogo descrivevano quello mostrato. Ora la
scrittura finale e' incondizionata. Un abort `--strict-sync` prima del
rendering salvava un report senza il marchio "senza video": `--review`
poteva estrarre frame da un video vecchio seguendo una timeline mai
renderizzata; ora l'abort marca il report. Il blocco run era rilasciato da
`__del__`: un'eccezione trattenuta dal chiamante ne teneva vivo il traceback
e il blocco restava preso per tutta la vita del processo. Ora un wrapper
`main()` lo rilascia in un `finally`. `--dry-run` pulisce le anteprime come
giu' facevano `--preview` e la run normale. `--prefetch-models` e
`--openvino-download` rilasciano il blocco durante il download, cosi' una
run vera non aspetta 15 minuti per un'operazione che non è una pipeline.

**Script e CI.** La CI non controllava l'exit code di `ffmpeg -version` e il
tag di release collideva su un retry (stesso `GITHUB_RUN_NUMBER`). Il test
A/B tagliava i vettori alla lunghezza minima, dichiarando EQUIVALENTE una
candidata che non embeddava tutti i testi; il worker con la forma
`{slides, blocks}` embeddava solo le slide. `analysis_sync.py` cercava sempre
`video_finale.mp4` e con `--output lezione.mp4` stampava "0/0 frame coerenti"
uscendo 0: ora accetta `--video` ed esce 2 se il video manca. `--force` del
watcher e5 non segna piu' i nuovi candidati come gia' visti. `riga_baseline`
legge l'originale prima di scrivere quando `--scrivi` e `--confronta`
puntano lo stesso file. Docs e `.bat` allineati su cache rediretta, codici di
uscita e flag reali di `prova_materiale_reale.py`.

**Test che verificano davvero.** Cinque test "di regressione" dei batch
precedenti controllavano la funzione sbagliata o il testo sorgente: ora patchano
`_scrivi_attempts`, esercitano la guardia `--dry-run` a runtime, richiedono
`True` esplicito per il marcatore corrotto e passano dalla funzione di
produzione per i contatori di tempo.

Verifiche: `ruff` e `mypy` puliti (96 file, win32/linux/darwin), 1630 test
verdi (11 nuovi), `controlli.py` 6/6 e debug hardware 17/17.

## Batch 26 — acronimi, review e parziali (2026-11)

Nona revisione incrociata. Ventinove difetti, quattordici test nuovi. Ancora
una volta, diversi problemi sono regressioni delle correzioni del batch 25.

**Capitoli: acronimi e titoli tecnici.** La regola dell'ultimo batch scartava
PDF, SQL, XML, CSS (nessuna vocale) e titoli maiuscoli legittimi come
SPRING BOOT, SCRUM, STRUTTURE (tre consonanti iniziali); nel contempo
l'euristica lasciava passare INTE. Ora gli acronimi brevi senza vocale sono
ammessi, i titoli maiuscoli di piu' parole restano tali se non contengono
tre vocali consecutive, e i rumori veri (IEEARSI DIRLA, SFFSITE, RRAR)
tornano a "Slide N". Un timestamp sporco in una parola non fa piu' morire
la generazione dei capitoli dopo che l'SRT e' gia' stato scritto.

**`--review` non piu' contrastatoria.** Non lascia piu' il collegamento a
un'anteprima appena cancellata quando l'estrazione fallisce; con video
mancante pulisce le anteprime e toglie i collegamenti invece di dire
"senza anteprima" e poi stamparli; tollera i campi sporchi del report che
`riepilogo` gia' tollerava; non esce 0 se non riesce a salvare report e
riepilogo. `--solo-riepilogo` conserva il verdetto "video non creato" di un
dry-run/preview. `scrivi_sottotitoli` non restituisce piu' un SRT vecchio
quando la scrittura di quello nuovo e' fallita.

**Parziali e blocco.** L'impronta del parziale non includeva il percorso:
due file con lo stesso nome in cartelle diverse potevano scambiarsi le
parole già trascritte. Ora il percorso assoluto entra nell'impronta e
`_prepara_ripresa` verifica il campo `audio` salvato; un parziale con
timestamp non numerico viene scartato e si riparte da capo invece di
esplodere in `float()`. La diagnostica del refine tiene solo l'ULTIMA
decisione per slide, non quella vecchia del raffinamento prima della
riparazione. Il wrapper `main()` usa uno stato per THREAD: due run
nello stesso processo non si rilasciano il blocco a vicenda.

**Script e docs.** Il confronto A/B su forme diverse non solleva piu' un
traceback ma dà un verdetto; `riga_baseline` confronta i percorsi risolti
(assoluto/relativo/maiuscole); la tabella dei codici di uscita documenta
finalmente il `3` di `--review`. Cinque test "di regressione" dei batch
precedenti erano vacui (controlli sul testo sorgente o patch inutilizzate):
ora esercitano le funzioni reali.

Verifiche: `ruff` e `mypy` puliti (97 file, win32/linux/darwin), test verdi,
`controlli.py` 6/6 e debug hardware 17/17.

## Batch 27 — rumore maiuscolo, verdetto A/B e parziali (2026-11)

Decima revisione, sul codice del batch 26 ancora prima del commit. Sei difetti,
nove test nuovi. Ancora una volta le correzioni precedenti avevano aperto
falle nuove.

**Titoli maiuscoli.** La scarsa regola del batch 26 lasciava passare qualsiasi
frammento di tre lettere (`RRR`, `SFF`, `MMM`) e qualunque riga maiuscola di
piu' parole (`SFFSITE QQQ`). Ora gli acronimi noti (anche senza vocali:
`PDF`, `SQL`, `XML`, `CSS`, e i brevi `AI`, `CD`, `TV`) sono esplicitamente
ammessi, i frammenti noti e le sequenze rumorose di OGNI parola della riga
tornano a "Slide N", mentre i titoli tecnici legittimi sopravvivono.

**`--review`.** Anche il testo del dubbio usava `int()`/`float()` grezzi: un
report con campi sporchi moriva dopo aver generato le anteprime. Ora usa gli
stessi convertitori sicuri del resto del modulo. `scrivi_riepilogo` restituisce
`None` invece di sollevare quando la scrittura fallisce: il ritorno viene
controllato e `--review` non esce piu' 0 se il riepilogo non e' stato scritto.

**Stato e parziali.** La cancellazione per numero slide del batch 26
cancellava anche la seconda occorrenza di una slide con `--skip-slides`: ora
il raffinamento guidato dall'indice rimuove solo la vecchia decisione presa
per numero sullo stesso confine, e le due occorrenze restano distinte. Un
`coperto_fino` NaN/Infinito passava il controllo sul timestamp delle parole e
finiva nel report come punto di ripresa: viene scartato come un timestamp
non valido.

**Verdetto A/B.** Su forme di embedding diverse lo script usciva 1 senza
scrivere il report: il verdetto DIVERGENTE non veniva persistito e il test
costoso veniva ripetuto. Ora il report viene scritto in entrambi i percorsi,
con i campi che `updates` rilegge; `cache_dir` viene serializzato come stringa.
Un test del batch 22 controllava solo due stringhe nel sorgente dello script:
ora esegue la scrittura e verifica che il report finale esista e che non
restino file temporanei.

Verifiche: `ruff` e `mypy` puliti (98 file, win32/linux/darwin), test verdi,
`controlli.py` 6/6 e debug hardware 17/17.

## Correzione mirata — audio sostituito durante la run (2026-11)

Il video del 26/11 era fuori sincrono non per un errore degli annunci ma
perche' il podcast e' stato rigenerato mentre la pipeline lavorava: stesso
nome e stessa durata, ma contenuto diverso. La run aveva letto l'hash del file
all'inizio e usato una trascrizione di 2477 parole che finiva a 860s; il
podcast finale ne ha 3334 e arriva a 1197s. Mancavano gli annunci della parte
finale, quindi la slide 15 restava ferma per 402s.

Due controlli nuovi:

1. `_audio_cambiato_durante_la_run` rilegge l'hash dell'audio prima di salvare
   il report e interrompe la run (anche `--dry-run`/`--preview`) se il file non
   e' piu' quello di partenza. Il report viene marcato `senza_video`.
2. `_coda_audio_senza_parole` misura quanti secondi finali dell'audio non sono
   coperti dalla trascrizione. Oltre 60s avvisa, e con `--strict-sync` ferma la
   run prima del rendering. Il dato finisce in `sync_report.json`.

Sette test nuovi in `test_audio_race.py`. Un test del riuso video era flaky
(su filesystem con mtime grossolano non raggiungeva il confronto dell'hash):
ora forza un mtime diverso. Dopo la correzione la pipeline e'
stata rilanciata sul podcast attuale: 14 annunci su 14, 15 slide bilanciate
(la 15 dura 76,9s), verifica frame 15/15 e giudizio "ALTO".

## Batch 28 — qualita' dei capitoli e revisione incrociata (2026-11)

Undicesima revisione. Miglioramento richiesto: il `.capitoli.txt` dell'ultima
run conteneva frammenti OCR (`fatti. Il Problema`, `muss. e ion.`,
`15 La via d'uscita`) e frasi narrative invece di titoli.

**Generazione dei capitoli.** Il titolo ora si prende dall'inizio del testo
OCR, non dalla "riga migliore": si tolgono numero di pagina e prefissi rumorosi
(`muss. e ion. =`), si riuniscono le colonne spezzate da `|`, si taglia alla
prima intestazione di sezione (`Il Caso`, `L'Esito`, ...) o al primo separatore
di titolo. Se la prima riga e' un asse di grafico si provano le righe seguenti.
Quando l'OCR non e' affidabile si usa la frase pronunciata subito dopo
«passiamo alla slide N», prestata alla slide GIUSTA (non a quella successiva).
Ventitré test nuovi in `test_capitoli_qualita.py` e `test_audio_race.py`,
comprese le regole storiche che la prima stesura aveva rotto.

**Revisione.** Numerosi difetti, quasi tutti regressioni delle correzioni
appena fatte:
- la coda senza parlato veniva misurata sul primo greedy, poi sostituito dalla
  trascrizione finale; ora si misura dopo la scelta definitiva e non aborta in
  `--strict-sync` se tutti i confini sono annunciati (coda di musica);
- `NaN`/`Infinito` in un timestamp disattivavano il controllo;
- l'audio era ricontrollato una sola volta, 350 righe prima del rendering:
  ora `_verifica_audio_invariato` gira anche prima di ogni `build_video`;
- l'abort salvava solo `senza_video`, e `--review`/`--solo-riepilogo` raccon-
  tavano di un `--preview` mai avvenuto: ora c'e' `motivo_interruzione`, la
  pagina vecchia viene aggiornata subito e il riepilogo mostra il motivo e
  l'avviso sulla coda anche con una pagina sola;
- i timestamp non finiti, il file audio sparito e le parole sporche hanno Test
  dedicati; i test vacui sui percorsi sono stati rafforzati.

Documentazione aggiornata (`funzionamento.md`, conteggio 1683), docstring del
riuso video resa onesta sul percorso rapido.

## Titoli riassuntivi dei capitoli (2026-11)

Il titolo preso dall'inizio della slide diceva l'argomento ma non **cosa**
tratta quel capitolo. Ora `build_capitoli` passa il titolo per
`_titolo_riassuntivo`: dal corpo della pagina estrae l'elemento distintivo
(il caso tra parentesi, la sentenza, la commissione, il contesto), lo pulisce,
non lo ripete se contiene parole lunghe già presenti nel titolo e lo aggiunge
dopo «:». Limite portato a 100 caratteri, quello vero di YouTube, così il
titolo non viene tagliato a metà parola. Cinque test nuovi coprono caso,
sentenza, frase generica non ripetuta, assenza di sintesi e limite di lunghezza.

## Batch 29 — titoli riassuntivi e revisione incrociata (2026-11)

Dodicesima revisione, sulla funzionalita' dei titoli riassuntivi appena
introdotta. Undici difetti, undici test nuovi.

**Titoli.** I motivi con due punti catturavano 61 caratteri e venivano scartati
dal controllo sui 60: tre capitoli reali restavano senza sintesi; ora il
limite e' coerente e la cifra viene troncata a confine di parola. I titoli
dell'annuncio e quelli uniti raggiungevano 101 caratteri: `_tronca_titolo`
budgeta l'ellissi e li tiene entro 100, come YouTube. Le virgolette
sbilanciate (`caso La ‘scaltra peruviana`) e l'ellissi rimasta in mezzo al
titolo sono state eliminate. I cognomi brevi («Rossi») entrano nella sintesi,
mentre un frammento che ripete piu' della meta' del titolo viene scartato.
Quando l'OCR non e' leggibile e il titolo arriva dal parlato non si aggiunge
una sintesi di narrazione.

**Report e abort.** Le correzioni dei nomi nel report erano lo snapshot del
primo greedy, incompleto rispetto alla trascrizione finale: ora si riscrive
con lo stato finale. I due abort `--strict-sync` rimasti indietro passano
`output_path` e un `motivo_interruzione` stabile, riconosciuto da `--review` e
dal riepilogo. Ogni abort prima del rendering rimuove `timeline_finale.json`,
che altrimenti descriveva un video mai generato. La coda non coperta non viene
piu' arrotondata a 60 nel riepilogo.

**Trascrizione.** `_firma_audio` ora hashera l'intero file, senza memoizzazione
per percorso/dimensione/mtime: due finestre da 256 KB non bastavano a
distinguere un audio sostituito nel mezzo. Il parziale salvato porta la firma
completa e `_prepara_ripresa` la verifica, così non si cuciscono piu' parole di
un'altra versione dello stesso file.

## Batch 30 — titoli e abort a prova di regressione (2026-11)

Tredicesima revisione. Numerosi difetti, quasi tutti sulle correzioni del
batch 29.

**Titoli.** Due sintesi reali finivano con una preposizione orfana
(«...nelle aule di tribunale nei»): il taglio ora si ferma a confine di clausola
e butta le parole funzionali finali. Le virgolette venivano conteggiate male
perche' «» e' anche l'apostrofo italiano: ora si contano solo le virgolette
vere, e la pulizia gira anche dopo la troncatura e nei capitoli fusi. Il
capitolo 14 perdeva il proprio «Il Caso» per una soppressione troppo ampia;
la sintesi e' di nuovo cercata nel corpo della slide, che e' fonte diversa
dalla narrazione. Soglia di dedup e guardia sulle sottostringhe aggiustate per
non buttare la parola distintiva.

**Abort e report.** L'hash completo dell'audio veniva ricalcolato due volte a
ogni checkpoint e fino a cinque volte per ripresa: `percorso_parziale` e
`_leggi_parziale` accettano la firma gia' calcolata. La timeline veniva rimossa
anche dopo il rendering: ora `_abort_senza_video` distingue i due casi e la
conserva se il video esiste; `analysis_sync.py` esce 2 (non verificabile) invece
di 1 (guasto). Le correzioni dei nomi della trascrizione scartata dall'A/B non
finiscono piu' nel report, la pagina di abort non dice piu' «video assente» con
la data del video vecchio.

**Test e docs.** Test vacui rafforzati (parziale, review, riuso video, limite
100 caratteri), regex che ora riconosce davvero «L'Esito», doc e conteggi
aggiornati, guardrail Ruff di nuovo mirato ai moduli con prosa.

## Batch 31 — titoli, abort e qualita' dei test (2026-11)

Quattordicesima revisione. Difetti quasi tutti sulle correzioni del batch 30.

**Titoli.** Il taglio di clausola distruggeva i nomi composti
(«Rossi e Bianchi» diventava «Rossi») e cancellava parole vere che iniziano
con un apostrofo («d'appello»): ora il taglio vale solo oltre il budget e
sceglie l'ultimo separatore, e si tolgono solo gli apostrofi orfani. L'apostrofo
tipografico davanti a una cifra (`L’80`) non e' piu' una virgolette, e le
virgolette si abbinano con uno stack invece che per conteggio. L'intestazione
`L’Esito` con apostrofo tipografico viene finalmente riconosciuta.

**Abort e report.** Le correzioni dei nomi della trascrizione scartata
restavano nel report quando tutte venivano filtrate: ora l'helper puro
`_correzioni_effettive` dichiara l'assenza. La timeline poteva essere avvelenata
da un parziale scritto dopo la sostituzione dell'audio a meta' trascrizione:
il vecchio parziale viene buttato. `analysis_sync` distingue una run interrotta
da una timeline mai scritta (entrambe exit 2, ma con diagnosi diversa) e non
tratta piu' la cache mancante come guasto (exit 1). I confini stimati con
`--skip-slides` non prendono piu' tutti la stessa diagnosi, e il riepilogo non
avvisa piu' di confini non verificabili quando tutti sono annunciati.

**Test e docs.** Test rafforzati (virgolette, nomi composti, `numeri_slide`,
parziale, correzioni, riuso video), conteggio storico e tabella exit code
aggiornati, eccezioni Ruff ridotte ai soli moduli con prosa.

## Batch 32 — checkpoint, correzioni e diagnostica mista (2026-11)

Quindicesima revisione. Difetti quasi tutti sulle correzioni del batch 31.

**Checkpoint.** `_salva_parziale` cancellava il PROPRIO parziale a ogni altro
checkpoint: il guard confrontava il nome con il prefisso del nome due volte,
mentre la firma contiene gia' lo stem. I checkpoint ora alternavano scrittura
e cancellazione, e un crash poteva ripartire da zero. Ora il confronto e'
corretto e il parziale corrente viene riscritto.

**Report e diagnostica.** `_correzioni_effettive` validava la forma ORIGINALE
(o una sottostringa casuale): report e riepilogo potevano dichiarare correzioni
mai applicate. Si controlla solo il risultato, a confine di parola. Con diagnose
miste (normali per slide + guidate dall'indice) la tabella dei tempi e il
riepilogo perdevano le decisioni non indicizzate: ora ogni occorrenza prende
la propria. I contatori di embedding non vengono piu' azzerati prima della
sincronizzazione, così la verifica delle ancore compare nei tempi. Se
`build_video` solleva, il report viene marcato `senza_video` con motivo stabile
e la timeline non rimane a descrivere il video vecchio.

**Titoli.** `l’esito` in mezzo a una frase non taglia piu' il titolo (solo
l'intestazione in maiuscola e' un confine); l'apostrofo orfano non lascia una
consonante; la virgoletta chiusa attaccata a una cifra non viene scambiata per
apostrofo.

**Docs/test.** Documentazione dei codici di uscita, del modello OpenVINO e del
comando `controlli.py` corretta; test rafforzati su questi percorsi.

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

Stato all'ultimo lancio (batch 32): ruff pulito, mypy pulito su tre
piattaforme, 1723 test verdi, `_debug_hardware.py` e controlli 6/6.