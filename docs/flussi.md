<!-- Estratto da README.md (righe 541-614). Torna al [README](../README.md). -->

## 🔀 Due flussi di lavoro (e i loro avvisi)

Il progetto supporta due modi di lavorare, riconosciuti automaticamente dalla
trascrizione (override con `--flow`):

### 1. Podcast → Slide (`PROMPT_PODCAST.md`)

Il podcast viene generato PER PRIMO, in modo libero, e la presentazione nasce
DA esso (una slide per sezione). Il prompt **vieta esplicitamente** i
riferimenti "slide N": l'assenza di ancore è il comportamento atteso.

- Avviso "Nessun riferimento 'slide N'… flusso libero" → **atteso, nessuna
  azione necessaria** (il messaggio lo dice esplicitamente).
- Il pipeline ripiega sull'allineamento ordinato con soli embeddings (veloce,
  senza LLM) e posiziona le slide per contenuto.

> **`--require-full-anchors` e `--min-anchor-coverage` in questo flusso sono ignorati**: servono al flusso B →
> A (dove ogni pagina deve essere annunciata), qui le ancore sono escluse dal
> prompt per scelta. L'opzione viene applicata solo se il flusso *rilevato* è
> quello con ancore: il fallback interno riscrive il flusso effettivo da `free`
  a `slide-audio`, ma la decisione guarda il flusso originale.
>
> **Prima di generare, conta le pagine del PDF e confrontale con le sezioni
  del podcast**: è l'unico controllo che il programma non può fare al posto
  tuo, ed è la condizione dell'allineamento 1:1 che questo flusso promette.

> **Modello embedding**: il default è `intfloat/multilingual-e5-large`
> (più preciso, ~2.2 GB, validato con test A/B). Nel solo flusso libero puoi
> provare un modello più leggero e veloce con `--semantic-model
> sentence-transformers/paraphrase-multilingual-mpnet-base-v2` (già usato
> come fallback automatico): più rapido ma leggermente meno preciso —
> controlla con `python main.py --dry-run` che nel riepilogo la fiducia del
> motore resti **alta** (picco medio normalizzato sopra la soglia) e non
> compaia l'avviso "segnale debole".

### 2. Slide → Podcast (`PROMPT_PRESENTAZIONE (PREDEFINITO).md`)

La presentazione esiste prima e il podcast deve **annunciare ogni slide**
("passiamo alla slide N"): queste ancore vincolano la sincronizzazione.

- Avviso "Solo N slide su M annunciate esplicitamente" → nel flusso
  slide → podcast il podcast doveva annunciarle tutte: se le manca, conviene
  rigenerare l'audio PRIMA di procedere. In batch/CI (`--no-confirm`, es.
  `genera_video.bat`) la soglia `--min-anchor-coverage` (default 50%)
  interrompe da sola sotto copertura; aggiungi `--require-full-anchors` per
  **interrompere** anche sopra soglia (qualsiasi ancora mancante) invece di
  generare un video con durate stimate e micro-segmenti.
- Avviso "Durate slide molto squilibrate" → ora viene **validato sul
  contenuto**: se il parlato del segmento è coerente con la slide mostrata
  (F1 lessicale), la durata lunga/corta è reale e l'avviso si riduce a una
  nota informativa; se il parlato corrisponde a un'altra slide, l'avviso
  resta (probabile allineamento errato).
- Riferimenti fuori ordine (es. "come dicevamo nella slide 3" dopo la
  slide 4) non fanno più perdere l'ancora: viene recuperata la prima
  menzione in ordine cronologico.
- Un numero vicino alla parola "slide" che **non** è un riferimento ("i tre
  concetti della slide", "la slide spiega il ciclo in quattro fasi") non crea
  più un'ancora fantasma: una menzione vale come transizione solo se, al suo
  tempo, nessuna slide di numero maggiore era già stata annunciata. Le
  citazioni scartate sono elencate con il loro tempo nel riepilogo e in
  `sync_report.json` (`anchors.discarded_citations`): un confine spostato di
  minuti ha lì la sua spiegazione.
- La numerazione detta ad alta voce è verificata **a ogni run**, anche quando
  tutte le slide sono annunciate: uno sfasamento uniforme (deck con una pagina
  in meno, copertina esclusa dalla numerazione) sposta l'intero video di una
  slide senza che manchi alcuna ancora. È un controllo offline (~2s con cache).
- Fra due ancore consecutive le slide non ancorate restano dentro i blocchi
  compresi fra le due: senza questo vincolo la somiglianza del contenuto
  potrebbe assegnare a cinque slide lo stesso blocco, producendo segmenti di
  mezzo secondo che il pavimento anti-flicker non può spostare (i due vicini
  sono ancorati).

---

