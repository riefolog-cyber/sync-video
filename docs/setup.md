<!-- Estratto da README.md (righe 249-540). Torna al [README](../README.md). -->

## 🖥️ Setup su un altro PC

**Su Windows, se hai scaricato lo ZIP: non devi installare niente a mano.**
Metti `presentazione.pdf` e `podcast.m4a` nella cartella e fai doppio clic su
`genera_video.bat`: il programma installa Python 3.11 (via winget), Tesseract,
ffmpeg e i modelli, e poi genera il video. Vedi
[Primo avvio automatico](#primo-avvio-automatico).

Git serve solo se vuoi clonare il repository; dallo ZIP non ti serve.

1. **Installa Python 3.10+** da [python.org](https://python.org) — spunta **"Add Python to PATH"**. *(Solo se non usi l'avvio automatico, o se sei su Linux/macOS.)*
2. **Installa Git** da [git-scm.com](https://git-scm.com) (se non presente, e solo se vuoi clonare).
3. **Clona il repository**:
   ```bash
   git clone https://github.com/riefolog-cyber/sync-video.git
   cd sync-video
   ```
4. **Aggiungi i tuoi file**: `presentazione.pdf` e `podcast.m4a` nella stessa cartella.
5. **Lancia `genera_video.bat`** — tutto il resto è automatico. A fine run il
   programma controlla da solo il video prodotto (un fotogramma a metà di ogni
   slide), **corregge da sé** i confini che non tornano e rigenera il video, poi
   stampa un riepilogo in parole semplici: quante slide, quanto durano, cosa è
   stato corretto e cosa conviene controllare a mano.

### Cosa viene adattato al tuo PC

Al primo avvio il programma guarda l'hardware e sceglie da sé. Non devi
configurare nulla: se vuoi sapere cosa ha deciso, la riga del riepilogo a fine
run lo dice.

| Cosa | Come viene scelto |
|---|---|
| Motore di trascrizione | GPU NVIDIA → CUDA (float16); iGPU Intel → OpenVINO su GPU; altrimenti CPU (int8) |
| **Encoding del video** | NVENC / QSV / AMF / VideoToolbox se la GPU li supporta, altrimenti `libx264` su CPU |
| Thread whisper | core **fisici**, senza tetto rigido |
| Thread embedding / video | `min(12, core fisici)` — il tetto serve a non saturare la banda memoria |
| Thread OCR | `min(6, core fisici)` — è I/O + CPU |
| Batch embedding | `16` sotto 6 GB di RAM, `32` sotto 12 GB, `64` oltre |
| Batch whisper | `4` sotto 6 GB di RAM, `8` oltre |

**Cosa NON si adatta, e perché.** I *modelli* restano quelli di default
(`whisper small`, `multilingual-e5-large`) su ogni macchina. Il batch si
riduce, ma i pesi no: e5-large tiene ~4,3 GB in memoria e `small` altri
~500 MB, ovunque. Su un PC con poca RAM il programma **avvisa** all'avvio e
dice cosa mettere in `.env` per alleggerire — non abbassa il modello di
nascosto, perché un risultato peggiore che nessuno ha scelto è peggio di un
avviso, e i confronti con la baseline del progetto (che usa `small`)
perderebbero significato.

**Nessuna di queste scelte può far fallire la run.** Se l'encoder accelerato
non funziona (driver vecchio, device non supportato) il video viene rifatto
automaticamente su `libx264`; se il modello di trascrizione non parte sul
device richiesto si ripiega su CPU. Un video lento è sempre meglio di nessun
video.

Le variabili d'ambiente hanno sempre precedenza sul rilevamento automatico
(`WHISPER_THREADS`, `EMBED_THREADS`, `VIDEO_THREADS`, `OCR_WORKERS`,
`EMBED_BATCH`, `WHISPER_BATCH`, `VIDEO_ENCODER`): vedi
[comandi.md](comandi.md).

> **Le GPU su Linux.** Il rilevamento usa `lspci`, ma `lspci` sta nel
> pacchetto `pciutils` che non è installato di default su molte distro. Se
> manca si prova `nvidia-smi` e poi il sysfs (`/sys/class/drm/`): senza questi
> ripieghi una macchina con GPU NVIDIA restava sulla CPU senza dirlo. Se
> sospetti che il rilevamento abbia mancato la tua GPU, verifica con
> `--force-setup`.

### Primo avvio automatico

Scaricato lo ZIP del repository e fatto doppio clic su `genera_video.bat`, su
Windows **non devi installare niente a mano**. Il programma, in ordine:

1. **Python 3.11** — se non c'è nessun Python sul PC, lo installa con `winget`
   (circa 25 MB, una volta sola, per il tuo utente senza chiedere privilegi
   di amministratore). È l'unica cosa che non può fare il bootstrap interno,
   perché il bootstrap gira *dentro* Python: a installarlo è il `.bat`, che
   gira in `cmd.exe`.

   La **versione dipende dall'architettura** di quel PC:

   | Architettura | Versione | Perché |
   |---|---|---|
   | x86-64 (Intel, AMD) | **3.12** | È quella della CI, non ci sono motivi per evitarne un'altra |
   | ARM64 (Snapdragon, ecc.) | **3.11** | È la versione collaudata su Windows ARM, dove `faster-whisper` e OpenVINO non sono installabili (CTranslate2 non pubblica wheel `win_arm64`) e la trascrizione va su CPU |

   L'architettura è letta da `%PROCESSOR_ARCHITEW6432%` quando disponibile,
   perché su Windows ARM un `cmd.exe` x64 emulato si dichiara `AMD64`: leggere
   quella variabile da sola installerebbe il pacchetto sbagliato.

   Se sul PC c'è già un Python con i pacchetti del progetto, viene usato
   quello e non si installa niente.
2. **Tesseract OCR** e **ffmpeg** — con `winget` / `apt-get` / `brew`, se mancano.
3. **I modelli** (~6.4 GB di embedding + ~490 MB di Whisper), annunciati
   prima di essere scaricati.
4. **L'ambiente** `.venv`, con i pacchetti del progetto isolati dagli altri
   programmi del PC.

Riepilogo: **Tesseract, ffmpeg, i modelli, i pacchetti, l'encoder video e i
thread** si adattano da soli. Python richiede winget (cioè Windows 10
1809/Windows 11) e può essere saltato.

Per **non** farlo installare e decidere tu:

```powershell
$env:SYNC_VIDEO_NO_PYTHON_INSTALL=1
.\genera_video.bat
```

Se winget non è disponibile (Linux, macOS, Windows 10 precedente, macchine
gestite) il programma lo dice esplicitamente e indica la pagina di
installazione: **non** fallisce con un errore incomprensibile.

> Su **Linux e macOS** l'installazione automatica di Python **non** esiste:
> il repo richiede di installare Python a mano, poi lancia `crea_venv.sh`
> (o `crea_venv.bat`). Tutto il resto è automatico anche lì.

### Cosa viene installato automaticamente al primo avvio

| Componente | Dimensione | Metodo |
|---|---|---|
| Pacchetti pip (12 su x86-64) | ~250 MB | `pip install` |
| Tesseract OCR | ~40 MB | `winget` / `apt-get` / `brew` |
| ffmpeg | ~80 MB | `winget` / `apt-get` / `brew` |
| Modello embedding e5-large | **~2.1 GB** di disco | fastembed (download automatico) |
| Modello Whisper `small` (CPU) | ~460 MB | faster-whisper (download automatico) |
| Modello Whisper OpenVINO | ~75 MB (`tiny`) / ~470 MB (`small`) | `--openvino-download` (facoltativo) |
| Lingua Tesseract ITA | inclusa | `tessdata/ita.traineddata` |

> **Le dimensioni sono misurate, non stimate.** I valori sopra sono i byte
> reali occupati dopo una run completa con le versioni pinnate in
> `requirements.txt` (`fastembed==0.5.1`): la copia
> `models--qdrant--multilingual-e5-large-onnx` dentro
> `.cache/embedding_model/` pesa 2,10 GB, e il Whisper `small` nella cache
> HuggingFace 464 MB.
>
> Una versione precedente di questa pagina riportava 6,4 GB per l'embedding,
> scomposti in due copie (2,1 + 4,3 GB). **Quella scomposizione non si
> riproduce**: sulla macchina in cui è stata rimisurata c'è una sola copia,
> da 2,10 GB. Non è chiaro se dipenda da una versione diversa di fastembed o
> da una misura fatta su una macchina che aveva scaricato di più.
>
> Finché la cosa non è chiara, **il controllo dello spazio nel programma
> continua a usare 6,4 GB**, che è il caso peggiore: sbagliare in eccesso
> dà un avviso inutile, sbagliare in difetto fa fallire il download a metà
> con un errore che non spiega la causa.
>
> **Il modello Whisper CPU è nella cache HuggingFace**, non in `.cache/`: con
> `HF_HOME` personalizzato va cercato lì. La copia OpenVINO è **facoltativa** e
> non viene scaricata dal percorso di default.
>
> Al primo avvio il programma **annuncia questi download prima di farli**,
> perché un download da qualche gigabyte in silenzio è indistinguibile da un
> blocco. Se i modelli sono già in cache non stampa nulla.

> **La trascrizione è il collo di bottiglia** (≈85% del tempo su un podcast
> reale). I due acceleratori sono attivi **di default** e non richiedono setup:
>
> 1. **Decoding a batch** (`--whisper-batch 8`): stesso modello, stessi pesi,
>    stessa decodifica, solo più segmenti elaborati insieme.
> 2. **Beam size 1** (`--whisper-beam 1`): decodifica greedy.
>
> Misurato su podcast reale (17m38s, Snapdragon X Elite, small int8, 8 thread):
>
> | configurazione | tempo |
> |---|---|
> | beam 5 sequenziale | 91.5 s (su 240 s di audio) |
> | beam 1 sequenziale | 74.6 s |
> | **beam 1 + batch 8 (default)** | **40.1 s** |
>
> Sull'audio intero: 2m59s invece di ~6m40s stimati, cioè **~2.3× più veloce**.
> **Precisione:** confrontando le 10 ancore `slide N` con quelle prodotte da
> beam 5, lo scarto medio è 0.050s e il massimo **0.150s** — 20 volte sotto la
> durata minima di una slide (3s), nessuna ancora persa o aggiunta. Le ancore
> sono ciò che vincola la timeline, quindi la sincronizzazione non cambia.
> Se serve il testo più accurato possibile: `--whisper-beam 5` (il batch resta
> attivo, nessuna perdita); per disattivare il batch: `--whisper-batch 0`.
>
> **La scelta del beam è automatica.** Il beam è un parametro della
> *trascrizione*, ma le ancore `slide N` si conoscono solo *dopo* aver
> trascritto: la decisione non può essere presa in anticipo. Quindi la pipeline
> trascrive veloce (greedy) e poi corregge:
>
> - **timeline vincolata dalle ancore** (≥ metà delle slide annunciate): il testo
>   rifinisce confini già decisi, quindi la decodifica veloce resta. Nessun costo.
> - **timeline decisa dal contenuto** (flusso libero, o meno della metà delle
>   slide annunciate): il testo è l'unico segnale di sincronizzazione, quindi la
>   trascrizione viene rifatta a beam `5`.
>
> Il costo della seconda trascrizione si paga **una volta sola per audio**: la
> cache è per chiave, quindi la run successiva ritrova entrambe le trascrizioni
> (verificato: seconda run 0s). Con un deck ancorato non succede nulla di tutto
> questo. Disattivabile con `--no-auto-beam` (o `AUTO_BEAM=0`); si può forzare il
> percorso accurato con `AUTO_BEAM_PINNED_RATIO=1.1`. Con `--whisper-beam 2` o
> superiore la scelta automatica non interviene: hai già scelto l'accuratezza.
>
> **La scelta viene misurata, e poi seguita.** Poiché le due trascrizioni
> esistono entrambe, la pipeline ne confronta la qualità di allineamento sulle
> stesse slide e **senza ancore** (è il caso in cui il testo decide) e usa
> quella col segnale migliore:
>
> - l'accurata vince (di almeno `AUTO_BEAM_AB_MARGIN`, default `0`) → resta l'accurata;
> - vince la **veloce** → si usa la veloce: i minuti della decodifica accurata
>   non si pagano più, né per il testo né per il resto della pipeline;
> - l'accurata ha trovato **ancore** che la veloce non aveva → resta l'accurata
>   (le ancore sono riferimenti espliciti, più affidabili di un proxy di somiglianza);
> - confronto non calcolabile → resta l'accurata (scelta prudente).
>
> La misura costa ~25s per trascrizione (~50-70s in tutto su un podcast da 17
> minuti, solo nel percorso accurato), quindi viene **messa in cache** e riusata:
> rifarla a ogni run significherebbe due embedding completi per una decisione che
> non cambia. Sui dati reali del 13/09 vinceva la veloce (0.791 contro 0.770) e la
> pipeline usa la veloce: seconda run **40s in tutto**, senza re-embedding.
>
> **Quanto vale questa misura?** Verificata confrontando ~18 timeline candidate
> (corretta, spostata di 4s, invertita, mescolata, casuale) con la verità nota, su
> un deck derivato dall'audio e sul deck reale (probe temporaneo, non nel repo):
>
> - **differenze grandi → separazione netta.** Ordine invertito: 0.05 contro 0.68
>   della timeline corretta (accuratezza 0.00 contro 0.97). Il punteggio riconosce
>   senza ambiguità "slide sbagliata" e "ordine sbagliato".
> - **differenze piccole → non le distingue.** Spostando un confine di 8-20s il
>   punteggio può **salire** (0.530) mentre l'accuratezza **scende** (0.97 → 0.84):
>   il picco di somiglianza sta oltre il confine reale, perché lo speaker anticipa
>   l'argomento prima della transizione. Concordanza con la verità: 79.7%
>   (Spearman +0.743).
> - **la cosine grezza (vecchia misura) resta inutilizzabile:** varia tra 0.821 e
>   0.859 su *tutte* le candidate, giuste o sbagliate.
>
> Conseguenza pratica: uno scarto di ~0.02 fra due trascrizioni è **dentro la
> banda di rumore** del punteggio. La scelta fra le due resta legittima ma non è
> una "vittoria" dimostrata; per non ribaltare su rumore usare
> `AUTO_BEAM_AB_MARGIN=0.05`. Da sapere anche: il controllo frame **non può**
> rispondere a questa domanda — verifica che il video rispetti la timeline
> dichiarata (11/11 anche su una timeline arbitraria), non che la timeline sia
> quella giusta.
>
> Nota hardware: sullo **Snapdragon X Elite** 12 thread erano più lenti di 8
> (saturazione della banda memoria). Per questo il tetto di 12 vale solo per
> **embedding e video**, non per whisper: la trascrizione è l'unica fase che
> scala davvero con i core, quindi segue i core fisici senza tetto rigido.
>
> **L'embedding viene riusato, non ricalcolato.** La matrice slide↔blocchi è
> content-addressed (`.cache/embedding_cache/`, hash dei testi + identità del
> modello): stessi testi e stesso modello → stessi vettori, quindi le run
> ripetute sullo stesso podcast non ripagano l'embedding (misurato: 26s → 3s,
> run da 1m50s a 1m26s, timeline e 11/11 frame identici). Vale anche *dentro*
> la run: le slide embeddate per la verifica ancore e per il confronto fra
> trascrizioni sono le stesse della sincronizzazione, e il raffinamento dei
> confini le riusa. I file sono `.npz` (invisibili alla pulizia delle cache
> orfane, che guarda i `.json`) con un tetto di 40 voci (~1 MB l'una). Senza
> identità del modello la cache **non** viene usata: meglio ricalcolare che
> rischiare i vettori di un modello diverso.
>
> **La tabella tempi ora dice il vero.** Prima la riga `└ Embedding` mostrava il
> *caricamento del modello* (pochi secondi) e i ~25-30s di embedding veri non
> comparivano da nessuna parte — per questo lo spreco non era mai emerso. Ora
> `└ Embedding` è il calcolo dei vettori e `└ Modello` il caricamento, separati.
>
> Anche il tempo dell'LLM ha una riga sua (`└ LLM`), per lo stesso motivo: senza,
> una cascata LLM da ~328s finiva dentro `Sincronizzaz.` senza attribuzione e il
> costo restava invisibile finché non si leggevano i log riga per riga. Il tempo
> è contato anche per le chiamate **fallite**, che sono proprio quelle da vedere.
>
> **Il motore locale gira SEMPRE per primo, l'LLM è un escalation.** Il motore
> embedding costa pochi secondi (embedding in cache) e produce già una timeline
> utilizzabile, quindi viene sempre provato per primo e l'LLM serve solo a
> migliorarla. Prima l'ordine era inverso: oltre `--llm-local-threshold` slide
> senza ancora si saltava dritto all'LLM e, se falliva, si ricalcolava da capo il
> motore locale — nel run del 25/09 (12 slide senza ancora) questo costava ~328s
> di cascata per arrivare esattamente al fallback locale che si aveva a portata di
> mano in 2s. Ora la timeline locale viene **riusata** se l'LLM non dà niente.
>
> **Tre guardie sul costo dell'LLM**, nate dallo stesso run:
>
> 1. **Timeout 45s, non 120s.** Tarato sui dati reali di `comboact-state.json`
>    (p50 3.2s, max 10.9s su 37 modelli): 120s erano ~35x il p50, quindi non
>    scattavano mai per davvero e, quando scattavano, costavano 180s col retry
>    per scoprire che il backup rispondeva in 14s. Alzalo con
>    `LLM_9ROUTER_TIMEOUT` per i modelli "reasoning".
> 2. **Gli endpoint morti restano morti nella run.** Un timeout esaurito marca
>    l'endpoint come irraggiungibile: il retry non ripaga più gli stessi
>    timeout e va dritto al primo backup ancora vivo.
> 3. **I fallimenti vengono ricordati (cache negativa, TTL 30 min).** Prima la
>    cache LLM ricordava solo i successi, quindi un rerun ripagava l'intera
>    cascata per arrivare allo stesso fallback. Ora il rerun entro la finestra va
>    diretto al motore locale. La chiave è un hash del contenuto, quindi un
>    input diverso viene comunque ritentato. TTL con `LLM_FAILURE_TTL_SECONDS`
>    (`0` = disattiva).
>
> **OpenVINO GenAI (solo iGPU Intel).** Su PC Intel con iGPU Iris Xe e senza
> GPU NVIDIA è un'alternativa più veloce di faster-whisper su CPU (~5 min per 28
> min di audio), con word timestamps identici. Non serve su macchine ARM/AMD,
> dove OpenVINO vede solo la CPU. Setup una tantum:
>
> ```bash
> pip install openvino openvino-genai
> python main.py --openvino-download   # scarica il modello OpenVINO IR
> python main.py                       # ora usa OpenVINO in automatico
> ```
>
> **Setup automatico al primo avvio.** Alla prima run `main.py` rileva
> l'hardware del PC (via `machine_setup.py`), sceglie il motore più adatto e
> installa/scarica tutto il necessario, senza intervento manuale:
>
> - GPU NVIDIA → faster-whisper su **CUDA** (float16)
> - iGPU Intel (Iris/UHD/Arc) → **OpenVINO GenAI** (installazione `openvino-genai`
>   + download modello IR inclusi automaticamente)
> - altrimenti → faster-whisper su CPU
>
> La scelta è ricalcolata a ogni run a partire dall'hardware rilevato e
> validata contro il runtime (vedi "Cambio di PC / hardware" qui sotto); i fatti
> hardware restano in `.cache/machine_setup.json` con l'impronta della macchina.
> Controlla con `--force-setup` (rileva di nuovo) o disabilita con
> `--no-auto-setup`.
>
> **Su un PC nuovo, scarica i modelli prima.** Al primo avvio i modelli (~3 GB:
> embedding e5-large 2.2 GB, pesi Whisper, modello OpenVINO IR 930 MB) vengono
> scaricati *dentro* la run reale, mescolati al lavoro: un timeout di rete tronca
> tutto a metà e il log non distingue "modello mancante" da "download fallito".
> Meglio tenerli separati:
>
> ```bash
> python main.py --prefetch-models   # scarica tutto e esce (~3 GB, una tantum)
> python main.py                     # ora la prima run non scarica nulla
> ```
>
> Ogni modello è indipendente: quello non installabile su quella CPU viene
> saltato con una nota, senza far fallire gli altri.
>
> Fallback automatico a faster-whisper se OpenVINO non è installato o il
> modello manca. Seleziona il motore con `--transcriber {auto,openvino,whisper}`
> e il device con `--openvino-device {GPU,CPU}`.
>
> L'avviso "faster-whisper su CPU è LENTO… usa OpenVINO" compare **solo** se
> su quel PC OpenVINO è realmente utilizzabile: iGPU Intel rilevata da
> `machine_setup.json`, oppure runtime installato che espone un device GPU.
> La sola CPU OpenVINO non conta (nessun guadagno di velocità): su macchine
> AMD/ARM (es. Snapdragon X) il suggerimento viene soppresso.
>
> **Controllo aggiornamenti.** All'avvio (`updates.py`) verifica via PyPI se i
> pacchetti usati hanno versioni più recenti (risultato cachato per default 6h
> in `.cache/updates_check.json`). Di default chiede S/N per aggiornare
> automaticamente i pacchetti **non pinnati**; disabilita con `--no-update`
> (solo notifica) o `--no-update-check` (nessun controllo).
>
> Il report distingue tre motivi per cui una voce non è un'azione disponibile:
> è **pinnata** (scelta del progetto), è un salto di **major version** (da
> valutare a mano) oppure è **bloccata** dalle dipendenze già installate. PyPI
> dice cosa è pubblicato, non cosa sta in piedi in questo ambiente: `pillow 12`
> esiste, ma il pin di `fastembed` (`pillow<11`) e `moviepy` (`pillow<12`) la
> escludono, quindi `pip install -U pillow` non la installerebbe. Le voci
> bloccate si mostrano con 🚫 e il motivo, ma non entrano né nell'invito a
> `pip install -U` né nella domanda S/N: non c'è niente da installare.
>
> **Perché un pacchetto non si muove.** Quando il report dice "no" senza dire da
> cosa dipende il no, `--frozen-report` (o `aggiornamenti.bat --frozen-report`)
> spiega: per ogni pacchetto fermo elenca tutti i vincoli dei pacchetti installati
> (con ✅/🚫), il **tetto** raggiungibile in questo ambiente e, per ogni vincolo che
> esclude la candidata, fin dove si arriverebbe sciogliendolo **da solo** — gli
> altri restano, perché sciogliere un vincolo significa aggiornare il pacchetto che
> lo impone. Segnala anche quando il muro è a sua volta un pacchetto pinnato dal
> progetto. È un referto: legge PyPI, non installa nulla e non chiede conferme.
>
> **Pacchetti pinnati e test A/B.** Alcuni pacchetti sono bloccati a una
> versione specifica perché un upgrade cambierebbe il risultato validato.
> L'unico pin attuale è `fastembed==0.5.1` (le versioni successive passano da
> pooling CLS a mean pooling per e5-large, alterando gli embedding).
>
> Prima di aggiornare un pinnato, `check_fastembed_upgrade.py` esegue un
> **test A/B isolato**: raccoglie i testi reali del progetto (slide + blocchi
> trascrizione da `.cache`), calcola gli embedding con la versione installata
> e con la candidata (in una venv temporanea, senza toccare l'ambiente di
> lavoro), e confronta coseno-similarità per vettore e stabilità della
> decisione di sync (argmax z-score per blocco). Verdetto:
>
> - **EQUIVALENTE** → il pinnato viene incluso nell'aggiornamento automatico;
> - **DIVERGENTE** → resta pinnato e si aggiornano solo gli altri pacchetti.
>
> Il report è salvato in `.cache/fastembed_ab.json`. Esegui manualmente con
> `python check_fastembed_upgrade.py`.
>
> **Aggiornamento manuale dedicato.** Per fare il check + aggiornamento dei
> pacchetti senza lanciare l'intera pipeline, usa lo script standalone
> `aggiornamenti.bat` (o `python aggiornamenti.py`): esegue bootstrap +
> controllo aggiornamenti con richiesta S/N, come all'avvio di `main.py`.
> `genera_video.bat` di default salta il controllo (flag `--no-update-check`)
> per non rallentare la generazione; per riattivarlo al volo aggiungi
> `--check-updates`.
>
> Gli interruttori sono gli stessi di `main.py` e di `genera_video.bat`:
> `--no-update` (notifica senza installare), `--no-update-check` (non
> controllare PyPI), `--frozen-report` (referto dei pacchetti fermi),
> `--no-pause` (non fermarsi, per l'uso in automazione).
>
> **La manutenzione di 9Router è un altro script.** Sta in
> `aggiornamenti_9router.bat` e non è dentro `aggiornamenti.bat` perché
> modifica *quali modelli il servizio espone* (con `-AutoReplace` toglie dalla
> combo quelli che falliscono, con `-AddFreeModels` aggiunge quelli gratuiti):
> sono modifiche automatiche e non annullabili, che vanno chiese esplicitamente
> e non nascoste dietro "aggiorna le dipendenze". Serve solo se usi
> `--llm 9router`, perché il percorso di default è `--llm off`.
>
> | `aggiornamenti_9router.bat` | effetto |
> |---|---|
> | (nessun argomento) | chiede conferma, poi applica |
> | `--dry-run` | **mostra cosa cambierebbe e non applica nulla** |
> | `--no-pause` | non si ferma a fine script (automazione) |
>
> Un consiglio: la prima volta lancialo con `--dry-run`. `update-comboact.ps1`
> in anteprima scrive `Would update combo ... No changes applied` con i modelli
> che entrerebbero e quelli che uscirebbero.
>
> **Il codice di uscita di questo script non dice se la combo è sana.**
> `update-comboact.ps1` esce con `0` anche quando ha rimosso modelli falliti
> (esce con `1` solo su eccezione grave): un `0` significa "lo script è andato
> a buon fine". Per lo stato della combo guarda la riga
> `SUCCESS: Kept=.. | Removed=.. | Replaced=..` o il report in
> `9router-maintenance/logs/`.
>
> Serve `pwsh` (PowerShell 7): con il solo PowerShell di Windows lo dice e si
> ferma, invece di saltare il passo in silenzio.

---

