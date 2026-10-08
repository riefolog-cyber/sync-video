# Promemoria prima installazione (nuovo PC)

Per provare Sync Video partendo dalla versione funzionante.

## 1. Prerequisiti (unica cosa da fare a mano)

- **Python 3.10 o superiore** da [python.org](https://python.org) — durante
  l'installazione spunta **"Add Python to PATH"**.
- **~8 GB di spazio libero** (modelli scaricati al primo avvio, una tantum).
- Niente altro: pacchetti, ffmpeg, Tesseract e modelli si installano da soli.

> Senza Python il programma NON parte e NON lo installa da solo:
> è l'unico prerequisito manuale. Tutto il resto è automatico.

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

- La prima volta scarica i modelli (~7 GB): è normale che sembri fermo,
  i download vengono annunciati prima di partire.
- La trascrizione è la fase più lunga (su CPU anche più della durata
  dell'audio): pazienza al primo giro, poi è tutto in cache.
- `aggiornamenti.bat` per la prova **non serve**: serve solo in seguito
  per tenere aggiornati i pacchetti Python.

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
