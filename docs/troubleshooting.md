<!-- Estratto da README.md (righe 1076-1091). Torna al [README](../README.md). -->

## 🐛 Troubleshooting

| Problema | Soluzione |
|---|---|
| `TESSERACT OCR NON TROVATO` | Auto-install fallita: installa manualmente da [UB-Mannheim](https://github.com/UB-Mannheim/tesseract/wiki) |
| `TesseractError: language 'ita' not found` | `tessdata/ita.traineddata` è incluso — verifica che la cartella `tessdata/` esista |
| `ModuleNotFoundError` | `pip install -r requirements.txt` |
| `ImportError: C extension: None not built` o `Please upgrade numpy to >= 1.26.0` | numpy troppo vecchio per pandas (dipendenza di pytesseract): `pip install -U "numpy>=1.26.0"`. Il bootstrap lo rileva e lo ripara da solo prima di usare l'OCR |
| `Pacchetti installati ma non importabili` | Un'altra installazione ha cambiato una versione nel Python globale (condiviso con altri progetti): `python -m pip check` elenca i conflitti, poi `pip install -U <pacchetto>` |
| Il programma smette di partire senza aver toccato il codice | L'ambiente condiviso è cambiato: `python -m pip check`. Per non rivederlo più: `crea_venv.bat` (ambiente dedicato, immune alle installazioni altrui) |
| Sincronizzazione fallita | Similarità troppo bassa. Verifica che l'audio parli dei contenuti delle slide |
| Timeline sbagliata | Prova `--flow audio-slide` o aggiungi ancore "slide N" nell'audio |
| Audio troncato | Già protetto da fps=5 + buffer 3.0s |
| Video senza audio | `winget install ffmpeg` |
| Slide vecchie nel video | Usa `--no-cache` |

