# Promemoria prima installazione (nuovo PC)

Per provare Sync Video partendo dalla versione funzionante.

Questo è il percorso minimo. Per tutto il resto — cosa viene adattato al
tuo PC, come cambiare un default, cosa fare quando qualcosa va storto —
vedi [docs/setup.md](docs/setup.md).

## 1. Prerequisiti

**Su Windows non devi installare niente a mano.** Scarichi lo ZIP, metti
`presentazione.pdf` e `podcast.m4a` nella cartella e fai doppio clic su
`genera_video.bat`: il programma installa da sé Python 3.12 (3.11 su
Windows ARM), Tesseract, ffmpeg, i pacchetti e i modelli, poi genera il
video.

Restano due cose che il programma non può fare al posto tuo:

- **~9 GB di spazio libero** (modelli al primo avvio, una tantum). Se lo
  spazio non basta te lo dice prima di scaricare, con la cifra.
- **I tuoi due file**: la presentazione e l'audio.

Su **Linux e macOS** Python va installato a mano (`python.org`), poi si
lancia `crea_venv.sh`; tutto il resto è automatico anche lì.

> Se preferisci installare Python tu, o se winget non è disponibile, puoi
> disattivare l'installazione automatica:
> `set SYNC_VIDEO_NO_PYTHON_INSTALL=1` prima di lanciare il `.bat`.
> Il programma avvisa e si ferma con le istruzioni.

## 2. Procurati i file

- **Prova veloce (consigliata):** scarica lo ZIP del progetto da GitHub,
  estrailo dove vuoi. Per aggiornarlo in futuro dovrai riscaricarlo.
- **Uso continuativo:** `git clone https://github.com/riefolog-cyber/sync-video.git`
  (serve Git installato). Poi gli aggiornamenti arrivano con `git pull`.

## 3. Aggiungi i tuoi materiali

Metti nella cartella del progetto:

- `presentazione.pdf` — le slide (numerate dalla 1, niente copertina)
- `podcast.m4a` — l'audio (vanno bene anche `.mp3` o `.wav`)

Il podcast deve seguire il prompt giusto (vedi `PROMPT_PRESENTAZIONE
(PREDEFINITO).md` per il flusso consigliato): la presentazione come
**UNICA fonte** nelle fonti di NotebookLM.

## 4. Avvia

Doppio clic su **`genera_video.bat`** e aspetta il riepilogo finale.

- La prima volta scarica i modelli (~2,6 GB misurati; il programma ne
  chiede ~9 GB perché controlla sul caso peggiore): è normale che sembri
  fermo,
  i download vengono annunciati prima di partire.
- Al primo avvio vedi anche cosa è stato deciso per il tuo PC (motore di
  trascrizione, encoder video, thread in base ai core, batch in base alla
  RAM): è il rilevamento automatico, non devi configurarlo.
- La trascrizione è la fase più lunga (su CPU anche più della durata
  dell'audio): pazienza al primo giro, poi è tutto in cache.
- `aggiornamenti.bat` per la prova **non serve**: serve solo in seguito
  per tenere aggiornati i pacchetti Python.

> Se il tuo PC ha poca RAM (< 12 GB) vedrai un avviso che i modelli di
> default sono pesanti. Il batch si riduce da solo; per alleggerire anche
> i pesi metti `WHISPER_MODEL=tiny` in un file `.env` accanto al `.bat`.

## 5. Come capire se è andato bene

Nel riepilogo finale cerca:

- `Confini ancorati: N su N` (o quasi) — i cambi slide sono quelli
  dichiarati nel podcast;
- `Fiducia del motore: alta` — il contenuto corrisponde alle slide;
- `Controllo del video finito: OK` — ogni segmento mostra la slide prevista;
- `Dubbi da controllare a mano: nessuno` — non serve guardare il video.

Se invece la run si interrompe per `Copertura ancore insufficiente`,
l'audio non annunciava abbastanza slide: rigenera il podcast seguendo il
prompt (una sezione sviluppata per pagina, annunci `slide N` dalla 2
all'ultima) e rilancia.
