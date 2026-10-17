<!-- Estratto da README.md (righe 541-614). Torna al [README](../README.md). -->

## 🔀 Due flussi di lavoro (e i loro avvisi)

Il progetto supporta due modi di lavorare, riconosciuti automaticamente dalla
trascrizione (override con `--flow`):

### 1. Podcast → Slide (`PROMPT_PODCAST.md`)

Il podcast viene generato per primo, in modo libero, e la presentazione nasce
DA esso (una slide per sezione). Questo flusso **non è più supportato**: la
pipeline si basa sulle ancore "slide N" pronunciate.

- Messaggio "Flusso libero non supportato" → **non è un difetto**: la
  trascrizione non contiene riferimenti riconoscibili ("slide N" per
  `slide-audio`, "blocco successivo" per `audio-slide`). Il programma si
  ferma **prima** di generare un video.
- Rimedio: rigenera l'audio annunciando le slide ("Passiamo alla slide N", vedi
  `PROMPT_PRESENTAZIONE (PREDEFINITO).md`) oppure forza `--flow slide-audio`
  se sai cosa fai.

> **`--require-full-anchors` e `--min-anchor-coverage`**: servono al flusso con
> ancore, dove ogni pagina deve essere annunciata.

> **Modello embedding**: il default è `intfloat/multilingual-e5-large`
> (più preciso, ~2.2 GB, validato con test A/B). Su macchine meno potenti puoi
> provare un modello più leggero e veloce con `--semantic-model
> sentence-transformers/paraphrase-multilingual-mpnet-base-v2` (già usato
> come fallback automatico): più rapido ma leggermente meno preciso —
> controlla con `python src/main.py --dry-run` che nel riepilogo la fiducia del
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
- Prima di dire che il podcast annuncia poche slide, la pipeline prova a
  riparare il TESTO: l'avviso "solo N su M annunciate" e la soglia
  `--min-anchor-coverage` (che interrompe la run) arrivano dopo la ritrascrizione
  mirata delle zone cieche e, sotto soglia, dopo la decodifica accurata
  dell'audio intero con il confronto fra le due trascrizioni. L'audio viene
  giudicato per quello che dice, non per quello che il beam veloce è riuscito a
  scrivere; quando un gate scatta, dice anche cosa è stato già tentato. La
  decodifica intera si paga solo se le zone cieche non sono bastate: se il
  recupero riporta le ancore che mancavano, resta la decodifica veloce col testo
  ricucito.
- La ritrascrizione mirata non ricarica il modello per ogni zona (una sola
  istanza per run) e usa il budget di thread della decodifica principale. Senza
  `cpu_threads` esplicito faster-whisper sceglie da solo, e su questa macchina il
  default si comporta come 4 thread: su una clip da 60s dentro una zona reale
  (beam 5, small int8) sono 32.6s contro 24.7s col budget pieno, con il testo
  **identico** (somiglianza 1.000, le stesse 164 parole). Le zone sono la fase
  che decodifica più volte, quindi era anche il costo nascosto più alto.
- Non esiste un rimedio intermedio fra "zone mirate" e "audio intero": quando
  la copertura è così bassa da far scattare la decodifica accurata, le zone
  coprono l'86-92% dell'audio (misurato rifacendo il conto su sottoinsiemi
  sparse delle ancore reali del run del 15/10), quindi un passaggio mirato
  costerebbe quasi quanto quello intero e in più perderebbe il confronto fra le
  due trascrizioni, che è l'unica cosa che permette di tornare al testo veloce
  quando l'accurato peggiora le ancore. La quota `RECOVERY_MAX_ZONE_SHARE`
  (default 0.5) è il confine fra i due casi.
- L'ultima slide non ha un'ancora a destra (a destra c'è la fine dell'audio),
  quindi la sua transizione veniva ritrascritta col beam accurato solo se il
  decoder sbagliava **due** volte nella stessa zona (annuncio distrutto **e**
  ciclo ripetuto). Ora la coda si localizza da sola: la zona di ricerca va
  dall'ultima ancora a `TAIL_RECOVERY_RATIO` (default 1.5) volte la cadenza
  degli annunci già trovati, e se dentro c'è la transizione l'ancora torna
  esatta. Se la cadenza non è misurabile la zona non si apre (niente
  ritrascrizioni a caso su tutta la coda); se l'annuncio non c'è, si è speso
  tempo e nient'altro.

---

