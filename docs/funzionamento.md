<!-- Estratto da README.md (righe 956-1075). Torna al [README](../README.md). -->

## 🔧 Come funziona

1. **OCR** — Ogni pagina PDF → immagine (DPI 300) → Tesseract.
2. **Trascrizione** — Audio → Whisper (faster-whisper) con timestamp al decimo di secondo, **decodifica a batch** e beam 1 (~2.3× più veloce a parità di ancore: vedi sopra). Stopword rimosse, parole di transizione ("passiamo", "slide", "blocco"...) preservate.
3. **Auto-detection** — Scansione trascrizione per decidere il flusso (`slide-audio` o `audio-slide`).
4. **Ancore "slide N"** — Riferimenti espliciti → ancore deterministiche ad alta precisione. Riconosce numeri in cifre (*"slide 3"*), cardinali (*"slide tre"*, *"numero due"*), **ordinali** (*"la terza diapositiva"*, *"la quinta slide"*) in entrambi i generi, con articolo o "numero" in mezzo, e varianti fonetiche di trascrizione (*"nonna slide"* → slide 9, *"sla e due"* → slide 2, *"asl cinque"* → slide 5, *"sallay 2"* / *"slaib6"* → slide 2/6 con numero incorporato).
5. **Verifica mapping ancore** — Se la numerazione parlata è sfasata rispetto al PDF (es. copertina esclusa: lo speaker dice "slide 1" mostrando la slide 2), corregge il numero di slide delle ancore mantenendone i tempi esatti. Lo fa con l'**euristica deterministica** (embeddings locali, offline): rileva un offset sistematico coerente su tutte le ancore e lo applica. Tempi sempre rispettati, mai spostati.
   **Guardia sulle derive** (anche sulle cache): si applicano solo le derive *coerenti* della numerazione, cioè run contigue di **almeno 2 ancore sfasate dello stesso delta** (copertina esclusa, slide del PDF saltata a metà narrazione). I **rimappi isolati** vengono rifiutati: subito dopo un annuncio il parlato è già quello della slide *successiva*, quindi ogni test di contenuto la preferisce alla slide annunciata (tipico delle copertine/diapositive di transizione, il cui OCR non può confermarle). La **"slide 1" parlata non è mai rimappabile**: la slide 1 reale è sempre a 0.0s e il suo riferimento non è un confine di transizione.
   **Soglia sul rumore**: un'ancora è dichiarata *non confermata* solo se la pagina migliore batte quella annunciata di più della dispersione delle similarità della stessa finestra (MAD × 1.4826, minimo 0.01 di coseno). Su un deck monotematico le pagine stanno tutte in una banda stretta (run del 17/10: 12 slide fra 0.840 e 0.888) e l'argmax sceglie di millesimi: senza soglia un margine di 0.004 diventava un dubbio da verificare a mano su un video già confermato 12/12. I near-miss restano nel report (`anchors.weak_margin`) e non guidano nessuna correzione, così un offset nato dal rumore non sposta la numerazione di tutte le ancore.
6. **Sincronizzazione semantica** — Embedding (e5-large via fastembed, offline ONNX) + programmazione dinamica monotona. Assegna ogni blocco audio alla slide semanticamente più vicina, con competizione softmax (temperatura 0.15).
7. **Anti-flicker** — Garanzia di durata minima (default `max(8s, 2×--semantic-min-duration)`) su ogni slide della timeline ordinata: le slide troppo corte (tipiche di quelle senza ancora, posizionate a ridosso dell'ancora successiva) vengono allungate spostando **solo i confini non ancorati**. Le ancore "slide N" pronunciate restano esatte al decimo di secondo; se nessun confine è spostabile la slide resta corta e viene segnalata, senza inventare posizioni.
8. **Riconciliazione** — Validazione: tempi crescenti, durate positive, ultima slide entro fine audio. Se invalida → interruzione.
9. **Video** — Slide ridimensionate a 1080p, assemblate con MoviePy (fps=5, buffer 3.0s anti-troncamento).
10. **Pulizia** — File temporanei e cache orfana rimossi automaticamente.

### Come lavora il codice per scenario

Le fasi 1-3 (OCR, trascrizione, auto-detection) sono comuni: da lì la
pipeline prende una strada diversa a seconda del segnale presente nell'audio.

| Scenario | Segnale | Cosa fa |
|---|---|---|
| **`slide-audio`** | "Passiamo alla slide 3" | Estratte ancore deterministiche "slide N" (cifre, cardinali o **ordinali**: "la terza diapositiva") → vincoli ad alta precisione. Verifica mapping ancore: se la numerazione parlata è sfasata rispetto al PDF, l'**euristica deterministica** (embeddings locali, offline) corregge gli offset sistematici. Sincronizzazione semantica (embedding e5-large offline): ogni blocco audio → slide più vicina, DP monotona. Le slide SENZA ancora vengono posizionate per contenuto dal motore embedding (nessun LLM); le ancore restano esatte. Riconciliazione (tempi crescenti, durate positive); se impossibile → **interruzione**. Slide in ordine 1→N. Un log diagnostico distingue riferimenti trovati/usati/scartati e segnala le slide senza ancora esplicita. |
| **`audio-slide`** | "Passiamo al blocco successivo" | Stesse ancore (numeriche o ordinali), stessa pipeline ordinata; la slide cambia sulle transizioni di blocco non numerate. |

Un audio **senza** questi riferimenti riconoscibili non è sincronizzabile: la
pipeline si **interrompe** con un errore chiaro che chiede di rigenerare il
podcast annunciando le slide.

Il raggruppamento in finestre temporali e la costruzione dei blocchi per la
sincronizzazione vivono in `chunks.py` (`build_windows`).

**In tutte le strade**: timeline → durate → MoviePy (slide 1080p, fps 5,
buffer 3s) → `video_finale.mp4`, con pulizia finale della cache orfana.
Se il segnale è insufficiente il programma **si interrompe con avviso**,
mai inventando distribuzioni uniformi.

### 🧠 Che cos'è un embedding (in parole semplici)

Un **embedding** è la "traduzione" di un testo in una lista di numeri che
ne cattura il **significato**. L'idea chiave: **testi che vogliono dire cose
simili hanno liste di numeri simili**.

- "il ciclo dell'acqua" e "come l'acqua evapora e poi piove" → numeri **vicini**
- "il ciclo dell'acqua" e "la guerra dei cent'anni" → numeri **lontani**

La pipeline usa questa tecnica per sincronizzare: trasforma ogni slide e ogni
pezzo di parlato in numeri, poi mostra ogni slide nel momento in cui il parlato
ha i numeri più simili. È un modello **offline** (gira sul tuo PC, nessuna
connessione) e **senza IA online**: il "cervello" che decide quando cambiare
slide è tutto locale.

Esempio: se nella trascrizione a 3 minuti si parla di "riciclaggio della
plastica" e una slide parla di "riciclo dei rifiuti", il modello riconosce che
sono simili e mostra quella slide in quel momento.

> **Nota sulla fiducia**: il riepilogo finale è diviso in tre parti, in
> quest'ordine: **COSA È CERTO** (verificato sul video o annunciato a voce dal
> relatore), **COSA NON È CERTO** (stime e punti da guardare) e **COSA HA
> DECISO IL PROGRAMMA** (durate minime, correzioni automatiche). Chiude con la
> fiducia misurata del motore, es. `Quanto è sicuro il programma: ALTO — il
> parlato somiglia chiaramente alla sua slide (punteggio 0.742 contro una
> soglia di 0.450; somiglianza media 0.836)`. Se il parlato non segue
> chiaramente l'ordine delle slide (es. le slide si somigliano molto tra loro)
> il verdetto diventa `BASSO — le slide si somigliano troppo...` e la
> sincronizzazione è dichiarata **stimata dal contenuto** — ma solo dei confini
> che lo sono davvero: con i confini pronunciati come ancore "slide N" il
> valore basso resta un fatto della run (il motore non ha potuto confermare la
> numerazione da solo) e **non** diventa un dubbio sui tempi, perché non c'è
> nessuna durata stimata da controllare. Per un allineamento certo, fai
> pronunciare le ancore esplicite "slide N" (vedi i workflow qui sotto).
>
> **Il frame check ha la precedenza sulla fiducia del motore.** Se
> `--verify-video` ha controllato *tutti* i segmenti e non ha trovato
> mismatch, l'artefatto è verificato e finisce in **COSA È CERTO**, non fra i
> punti da guardare: sarebbe chiedere di controllare un video già guardato.
> Le due misure rispondono a domande diverse — il frame check guarda
> l'artefatto (ogni segmento mostra la slide dichiarata), la fiducia guarda la
> timeline (i confini sono quelli giusti) — e per questo l'avviso sulla fiducia
> resta anche quando i frame tornano, a meno che i confini non siano tutti
> pronunciati (lì non c'è nessuna stima da mettere in dubbio).
>
> Attenzione: `avg_z` è un proxy, e diventa pessimista quando le slide sono
> **poche e lunghe** (misurato: 8 slide su 30 minuti, ~226 s per slide →
> `avg_z` 0.38 con frame check 8/8). Lo z-score di una slide è normalizzato su
> tutta la linea temporale: più la slide è "a casa sua" a lungo, più il
> parlato delle sezioni vicine le somiglia e il segnale di picchio si diluisce.
> Per questo la soglia 0.45 non va ricalibrata sulla sola base di questi
> casi: è il frame check a decidere, non il motore.

### Modello embedding

- **Default (definitivo)**: `intfloat/multilingual-e5-large` (1024 dim, ~2.2 GB) — scelto tramite test A/B su podcast reale (10 slide): similarità media 0.791 vs 0.380 (MiniLM) e 0.421 (mpnet), con durate tutte bilanciate (104-188s) e zero slide anomale (gli altri producevano slide da 8-20s e da 332s).
- **Fallback**: `paraphrase-multilingual-mpnet-base-v2` (768 dim, ~1.0 GB) — usato automaticamente se e5-large non si carica. Tenere in cache: è la rete di sicurezza della pipeline.
- **Cache**: `.cache/embedding_model/` è condivisa e i modelli si scaricano al primo utilizzo (serve internet una tantum). MiniLM (240 MB) e mpnet (1.0 GB) restano in cache anche se non più default: non disturbano, e per tornare al vecchio comportamento basta un override temporaneo:
  ```powershell
  $env:EMBEDDING_MODEL="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
  python src/main.py
  ```
- **Ambiente**: `EMBEDDING_MODEL` e `EMBEDDING_MODEL_ALTERNATE` sovrascrivibili permanentemente.

### Quale prompt usare

Un solo processo di input: **presentazione → podcast**. Il podcast deve
pronunciare le ancore "slide N" **in cifre** a ogni sezione: senza ancore il
pipeline non può sapere dove cambia la slide e **si interrompe** con un errore
chiaro.

Il prompt [`PROMPT_PRESENTAZIONE (PREDEFINITO).md`](../<PROMPT_PRESENTAZIONE (PREDEFINITO).md>)
guida sia la generazione della presentazione (Studio → Slide Deck, dalle tue
fonti) sia il podcast che la segue nell'ordine, con la presentazione come
UNICA fonte. Ancore strette: cifre, "slide" chiara, mai "la slide successiva",
recupero salti.

**Procedura (Presentazione → Podcast):**

1. Genera la **presentazione** con NotebookLM (Studio → Slide Deck) usando il prompt dedicato nel file, e mettila nelle fonti come **UNICA fonte (rimuovi le altre)**.
2. Genera il **podcast** con il prompt del file: segue l'ordine della presentazione, con le ancore.
3. Verifica le ancore prima di lanciare il pipeline:
   ```bash
   grep -c "slide" transcript_raw.txt   # deve essere ≥ N-1 (una per transizione)
   ```
   Se è 0 il prompt non è stato seguito: rigenera il podcast.
4. `python src/main.py` → auto-detection `slide-audio`, ancore deterministiche, allineamento embedding locale.

**Se il podcast non pronuncia le ancore** (dibattito libero): il pipeline si
**interrompe** con "Nessun flusso rilevato". Rigenera il podcast
annunciando le slide (`PROMPT_PRESENTAZIONE (PREDEFINITO).md`), oppure forza
`--flow slide-audio` se sai cosa fai.

Risultato su test reali (NotebookLM, presentazione come UNICA fonte): 11/11, 13/14 e 12/13 ancore, durate 37s–2m05s senza pavimenti anti-flicker, fiducia del motore alta (picco 0.53–0.74 su soglia 0.45) e frame-check pieno. Sincronizzazione misurata, non stimata.

---

