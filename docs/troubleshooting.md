<!-- Estratto da README.md (righe 1076-1091). Torna al [README](../README.md). -->

## 🐛 Troubleshooting

| Problema | Soluzione |
|---|---|
| `TESSERACT OCR NON TROVATO` | Auto-install fallita: installa manualmente da [UB-Mannheim](https://github.com/UB-Mannheim/tesseract/wiki), oppure `winget install --id UB-Mannheim.TesseractOCR -e`, poi rilancia `genera_video.bat` |
| `open() got an unexpected keyword argument 'metadata_errors'` | PyAV fuori finestra (vecchio senza l'opzione, o v19 che l'ha tolta): `py -m pip install -U "av>=12,<19"`, poi rilancia (lo ZIP nuovo lo sistema da solo) |
| `Failed building wheel for pyyaml` / `py-rust-stemmers` su ARM | Stai usando il Python ARM64 nativo: serve quello x86-64 a 64 bit (il `.bat` lo sceglie da solo se c'è, altrimenti installa "Windows installer (64-bit)") |
| Il log si ferma dopo `2. Trascrizione con OpenVINO` senza errori | Il processo è morto dentro la GPU Intel (driver o RAM condivisa esaurita): chiudi tutto e rilancia; alla seconda volta parte su CPU da solo. Scorciatoia: `genera_video.bat --transcriber whisper` |
| `Library cublas64_12.dll is not found or cannot be loaded` (o `cudnn64_9.dll`) | GPU NVIDIA senza le librerie CUDA, che il driver non installa. Doppio clic su `installa_cuda.bat` (installa cuBLAS 12 e cuDNN 9, aggiunge le cartelle al PATH e verifica), poi chiudi e riapri `genera_video.bat`. A mano: `py -m pip install --upgrade nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"`, aggiungi al PATH le due cartelle `bin` di `nvidia/cublas` e `nvidia/cudnn` e riapri la finestra. Se le trova mancanti il programma ripiega da solo su CPU: il video esce lo stesso, più lento |
| `[USCITA ANOMALA] codice 1` | Errore gestito: leggi il messaggio qui sopra |
| `[USCITA ANOMALA] codice molto negativo` | Chiusura forzata dal sistema (crash nativo/OOM), tipicamente in trascrizione: vedi riga sopra |
| `Manca la presentazione` / `Manca l'audio` | Dice quale manca ed elenca la cartella: nomi esatti (`presentazione.pdf`, `podcast.m4a`) oppure qualsiasi altro nome (ricerca libera), mai nelle sottocartelle |
| `Python ... troppo recente` o `... solo su Windows` | Serve Python 3.10–3.13 x64 (su ARM quello emulato): riscarica lo ZIP fresco, il `.bat` installa quello giusto da solo |
| `TesseractError: language 'ita' not found` | `tessdata/ita.traineddata` è incluso — verifica che la cartella `tessdata/` esista |
| `ModuleNotFoundError` | `pip install -r requirements.txt` |
| `ImportError: C extension: None not built` o `Please upgrade numpy to >= 1.26.0` | numpy troppo vecchio per pandas (dipendenza di pytesseract): `pip install -U "numpy>=1.26.0"`. Il bootstrap lo rileva e lo ripara da solo prima di usare l'OCR |
| `Pacchetti installati ma non importabili` | Un'altra installazione ha cambiato una versione nel Python globale (condiviso con altri progetti): `python -m pip check` elenca i conflitti, poi `pip install -U <pacchetto>` |
| Il programma smette di partire senza aver toccato il codice | L'ambiente condiviso è cambiato: `python -m pip check`. Per non rivederlo più: `crea_venv.bat` (ambiente dedicato, immune alle installazioni altrui) |
| Sincronizzazione fallita | Similarità troppo bassa. Verifica che l'audio parli dei contenuti delle slide |
| Timeline sbagliata | Prova `--flow audio-slide` o aggiungi ancore "slide N" nell'audio |
| `Nessun flusso rilevato` | L'audio non annuncia le slide: rigeneralo con `PROMPT_PRESENTAZIONE (PREDEFINITO).md` ("passiamo alla slide N") |
| Audio troncato | Già protetto da fps=5 + buffer 3.0s |
| Video senza audio | `winget install ffmpeg` |
| Slide vecchie nel video | Usa `--no-cache` |

