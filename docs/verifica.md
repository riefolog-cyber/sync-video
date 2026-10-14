<!-- Estratto da README.md (righe 729-818). Torna al [README](../README.md). -->

## 🔍 Verifica post-run (analysis_sync.py)

Dopo una generazione puoi controllare la QUALITÀ della sincronizzazione
(quanto il video è davvero allineato al parlato) con lo strumento standalone:

```bash
python analysis_sync.py
```

Analizza la timeline e il video più recenti in `.cache/` e verifica: durate
per slide e segmenti anomali, similarità embedding parlato↔slide per
segmento, confini che tagliano a metà parola, scostamento delle ancore
"slide N" dichiarate, e confronto frame estratto vs slide renderizzata
(scrive i frame in `.analysis_frames/`). Non modifica nulla.

Ogni run salva inoltre `.cache/sync_report.json`: la tabella dei segmenti
effettivamente mostrati (slide, inizio, fine, durata) con il verdetto di
contenuto dei segmenti anomali (`coerente` / `disallineata` / `incerto`),
le scelte fatte dalla run (es. `engine: llm_escalated_weak_signal` quando il
motore embedding dichiara segnale debole e si passa all'LLM), la **qualità
misurata** dal motore (`quality`: `avg_sim` grezza, `avg_z` normalizzata,
soglia usata) e il verdetto `weak_signal`, le discrepanze della revisione LLM
e — con `--verify-video` — l'esito del confronto frame vs slide. Così la
sincronizzazione resta verificabile a posteriori senza rigenerare il video.

Nel flusso ordinato il report contiene anche **`anchors`**, che è la parte da
leggere per prima quando un confine sembra sbagliato:

- `anchored` / `transitions` — quanti cambi di slide vengono dai "slide N"
  pronunciati e quanti ne servirebbero. Con `anchored == transitions` la misura
  di qualità del motore **non** sta misurando i confini: sono quelli dichiarati
  dagli speaker. Per questo la copertura è stampata anche nel riepilogo finale.
- `discarded_citations` — i numeri che il parlato contiene ma che **non** erano
  un riferimento ("i tre concetti della slide", "la slide spiega il ciclo in
  quattro fasi"), con il loro tempo. È la diagnosi diretta di un confine
  spostato di minuti: un difetto di estrazione, non del motore embeddings.
- `unconfirmed` — ancore che il **parlato** non conferma (subito dopo
  l'annuncio il testo somiglia di più a un'altra slide). Non è un errore: il
  bridge verso la slide successiva è normale. Diventa un dubbio nel riepilogo
  solo se il mapping non è stato corretto (`mapping_corrected`), altrimenti è
  solo un dato di ispezione.
- `mapping_suspicious` / `mapping_corrected` — la numerazione detta ad alta voce
  era disallineata rispetto al PDF: o è stata corretta in automatico, o è
  rimasta sospetta e va controllata a mano.
- `starved_slides` — le slide che **non hanno tempo proprio nel podcast**: il
  pavimento anti-flicker le ha portate al minimo partendo da quasi nulla, quindi
  la loro durata è *costruita* per leggibilità, non misurata dal parlato. In
  video scorrono veloce. È il sintomo del deck con più pagine delle sezioni
  realmente sviluppate, e il rimedio dipende dal flusso: in podcast → slide si
  rigenera la **presentazione** (l'audio si riusa), in slide → podcast si
  rigenera l'audio. Non viene segnalata una slide che il podcast ha trattato per
  pochi secondi ma che il pavimento ha solo arrotondato: la soglia è metà del
  minimo, così l'avviso resta raro e quindi leggibile.

Quando la scelta automatica del beam entra in gioco, il report contiene anche
`beam`: la trascrizione **usata** (`chosen`: `greedy` o `accurate`), il motivo
(`reason`) e `beam.ab`, cioè la **misura di allineamento delle due trascrizioni**
sulle stesse slide e senza ancore (`greedy`, `accurate`, `delta_avg_z`,
`would_have_won`, `seconds`, `from_cache`). Sui dati reali del 13/09: veloce
0.791, accurata 0.770 → *avrebbe vinto la veloce*, con `concordance` 0.62 contro
0.53, e la pipeline ha usato la veloce. `concordance` e `confusability` restano
nel report perché su un deck confondibile il confronto è rumore e va riconosciuto
come tale.

### Come il raffinamento decide adesso (e perché il margine da solo non basta)

Il raffinamento a livello di parola sposta un confine quando un candidato batte
quello attuale, ma non su un margine assoluto: `margin` (0.01) è solo il
pavimento, e la decisione vera è `REFINE_STABILITY_SIGMA` (2.0) volte la
dispersione **misurata nella stessa finestra** in cui si sta decidendo — un MAD
(mediana degli scarti assoluti, robusta ai candidati degeneri che gonfiano una
deviazione standard). In più il candidato deve vincere anche con la finestra di
contesto allargata di `REFINE_PERTURBATION_SCALE` (1.5): un picco che sparisce
guardando più parlato non è un confine.

Perché il margine da solo non basta, misurato: sul materiale del 14/10, nella
finestra in cui il confine della slide 10 viene deciso la curva del punteggio ha
deviazione standard 0.013 (±30s) e MAD 0.015, mentre le decisioni prese in due
run consecutive valevano 0.021-0.023 (circa 1.8sigma) e 0.054. Un numero assoluto non
distingue un confine da quanto il punteggio si muove da solo. La banda 0.05-0.10
documentata in `config.py` non viene riusata qui: è misurata su un'altra scala
(`avg_z`), mentre questa soglia si autocalibra sulla scala della decisione.

Il raffinamento **guidato dall'evidenza** fa eccezione, ed è dichiarato: quando
la finestra di ricerca arriva dalla verifica del video (`search_window`), il
confine è sbagliato per prova e il punteggio serve solo a scegliere dove
metterlo. Le due prove di robustezza valgono per il raffinamento libero, dove è
il punteggio a decidere se muovere.

### Ogni decisione sul confine resta scritta

`refine_diagnostics()` (`semantic_sync.py`) tiene una voce per confine:
`current`, `best`, `margin`, `scatter` (il rumore locale), `sigma` e la
`decision` (`spostato`, `tenuto: margine dentro il rumore`, `tenuto: vince solo a
finestra stretta`, `tenuto: violerebbe un'ancora`, `non valutabile: ...`). Finisce
in `sync_report.json` (chiave `refine`) e risponde a una domanda che prima non
aveva risposta: **quale** confine è debole, invece di un solo numero globale che
non dice dove guardare. Replay della run del 14/10 (parole e OCR in cache,
modello embedding vero, 11s):

```text
slide 10: 1058.18s -> 1058.18s (+0.00s)
diagnostica: margin 0.0069, scatter 0.015, sigma 0.46,
             decision "tenuto: margine dentro il rumore"
```

Cioè: il confine della slide 10 non è sostenuto dal contenuto nemmeno per mezza
volta il rumore locale. Il riepilogo finale adesso lo dice, e per le slide il cui
inizio non è un annuncio `slide N` aggiunge la durata quando sta sotto
`_WEAK_ESTIMATE_DURATION_RATIO` (0.6) della mediana delle altre — con l'esclusione
di quello che il pavimento anti-flicker ha garantito, accorciato o non salvato,
che ha già la sua riga:

```text
- la slide 10: l'inizio non è un annuncio 'slide N' e il contenuto lo sostiene
  solo per 0.5 volte il rumore locale, quindi è una stima e non una misura, la
  slide dura 1m03s contro una mediana di 1m50s — se nel video compare tardi o
  presto, il confine da guardare è qui
```

Sulla run reale questa riga non c'era: la slide 10 compariva come tutte le altre,
quindi una stima non sostenuta era indistinguibile da una misurata.

### Misura del 14/10: il raffinamento può anche peggiorare

La run del 14/10 (10 slide, 18m41s, 8 transizioni su 9 ancorate) permette la
prima misura con verità di riferimento, perché la slide 10 annuncia se stessa:
in `podcast.m4a` "Passiamo alla slide 10" è alle 16:56 (parole 1015.5-1016.5,
col beam accurato). La regola delle ancore (`timeline._reference_boundary`) mette
il confine all'inizio della parola successiva all'annuncio: **1017.8s**. La stima
semantica era 1036.2s (+18.4s), il raffinamento l'ha portata a 1058.2s
(**+40.4s**): 22s nella direzione sbagliata, con margine 0.023, cioè dentro la
banda di rumore. Causa: la narrazione della slide 10 comincia con ~35s di
esempio (la lettera di scuse scritta dall'IA) che non compare nel testo OCR della
slide, quindi la somiglianza col pannello trascina il confine dove il pannello
viene letto, e il raffinamento massimizza la stessa somiglianza. Su questa classe
di errore il contenuto non può essere la fonte di verità: il confine cade dove
il testo della slide ancora non descrive nulla, e solo un'ancora lo fissa.

L'annuncio era recuperabile e non è stato recuperato: il beam greedy ha
sostituito 15.8s di parlato (1002.9-1018.6s) con la stessa frase di 11 parole
ripetuta 6 volte, e il rilevatore di testo inaffidabile guardava solo cicli di
1-3 parole. Da qui `DEFAULT_MAX_LOOP_CYCLE_WORDS` (config, 12 parole): sul
transcript di quella run la zona viene ora riconosciuta, e la ritrascrizione
mirata riporta l'ancora della slide 10 a 1017.8s (verificato ricucendo le parole
e rieseguendo `extract_slide_anchors`, senza rigenerare il video).

### Coda senza ancora a destra: la transizione finale si localizza da sola

Quel recupero aveva un difetto di fondo: funzionava **perché** il decoder aveva
sbagliato due volte. `anchor_gaps` localizza i buchi interni (serve un'ancora a
destra), quindi la transizione finale, che a destra ha solo la fine dell'audio,
veniva ritrascritta solo se nella stessa zona c'era **anche** un ciclo ripetuto.
Un annuncio garbato dell'ultima slide senza quel difetto non veniva recuperato:
la recuperabilità dipendeva dal caso, non da una regola. Ora la coda la localizza
`tail_recovery_zone` (`timeline.py`): la zona parte dall'ultima ancora e si
allunga di `DEFAULT_TAIL_RECOVERY_RATIO` (config, 1.5) volte la **cadenza** degli
annunci già trovati — la mediana degli intervalli fra ancore — perché l'annuncio
successivo cade dopo una sezione lunga circa come le altre.

Misura sul materiale del 14/10 (parole greedy in cache, che sono l'ingresso
vero della fase di recovery, durata 1121.25s): le ancore sono 2-6 e 8-9, la 7 è
un buco interno e la 10 è la coda. La zona esce **900.24-1067.57s**, copre le
16:56 e non prende tutta la coda; con un fattore 1.0 si fermerebbe a 1011.8s,
mancando l'annuncio di 6s — è la misura che giustifica il default 1.5 invece di
1.0 (una sezione finale lunga 157.9s contro una cadenza mediana di 111.6s).
Ritrascrivendo SOLO quella zona col beam accurato (123.9s di calcolo, 462
parole) l'ancora della slide 10 torna a **1017.84s**, lo stesso valore che
`_reference_boundary` prescrive: il recupero non dipende più dal ciclo ripetuto.
La prova è fatta sul materiale vero, ricucendo le parole in cache ed eseguendo
`extract_slide_anchors`, senza rigenerare il video.

Il cablaggio è verificato anche attraverso la pipeline, non solo chiamando la
funzione: `python src/main.py --dry-run --no-confirm` sugli stessi cache della
run del 14/10 registra `[Gap] Coda senza ancora a destra (slide 10): ritrascrivo
900.2-1067.6s`, e le zone si fondono come previsto (il loop delle 1002.9-1018.6s
finisce dentro la zona di coda: una ritrascrizione sola, non due). Il log chiude
con `Ancore recuperate dalla ritrascrizione mirata: slide 7 a 688.8s, slide 10 a
1017.8s` e la timeline finale dà la slide 10 a 16m57s (`start 1017.84s`, durata
1m43s) con 9 confini ancorati su 9. Su QUESTO materiale il risultato non cambia
rispetto a prima — la run reale aveva già il ciclo ripetuto e quindi recuperava
lo stesso annuncio: la differenza è che adesso non dipende da quel difetto del
decoder, e la zona viene dichiarata nel log invece di essere un effetto
collaterale.

Due limiti dichiarati invece che aggirati. Se la cadenza non è misurabile (una
sola ancora, o più ancore nello stesso istante) la zona non si apre: non si sa
dove cercare, e aprire tutta la coda costerebbe una ritrascrizione intera per
niente. E la zona è un'**ipotesi** su dove può stare l'annuncio: se dentro non
c'è nessuna menzione non si inventa niente — si è speso tempo, non credibilità.
Con più slide scoperte la zona copre la prima (che chiude anche la durata della
precedente), le altre restano stimate e il log le dichiara.

Da quella run nasce anche il controllo incrociato `announcements_off_timeline`
(`timeline.py`): ogni menzione "slide N" con un verbo di transizione davanti
viene confrontata col confine che la timeline **finale** dichiara per quella
slide, e uno scarto oltre i 5s finisce fra i "Da controllare a mano" del
riepilogo ("nel parlato la slide 10 viene annunciata a 16m57s ma il video la
mostra da 17m38s"). I richiami come "come dicevo nella slide 4" non hanno il
verbo davanti e restano fuori: la soglia serve a dire un fatto, non a fabbricare
allarmi. Sul materiale del 14/10 il controllo tace con la trascrizione greedy
(l'annuncio lì non c'è più, quindi non c'è niente da confrontare) e scatta sul
transcript ricucito: è complementare alla ritrascrizione mirata, non un
sostituto.

Ogni dubbio sui confini porta con sé un'anteprima da guardare in due secondi
(`video.boundary_review_sheet`): il foglio affianca il fotogramma **all'annuncio**
e quello **al confine dichiarato**, ognuno con la sua didascalia, e finisce in
`.cache/review/confine_slideNN_II.png` (nome deterministico: due run si
sovrascrivono invece di accumulare). Sul materiale del 14/10 il foglio mostra la
slide 9 all'annuncio (1017.8s, similarità 1.00 contro 0.50 della slide 10) e la
slide 10 al confine dichiarato (1058.2s): la contraddizione si vede invece di
leggersi, e non serve cercare il punto in un video da 13 minuti. Se il video non
esiste (dry-run) il dubbio resta scritto senza anteprima.

I dubbi si rileggono senza rifare niente: `python src/main.py --review` legge
`sync_report.json`, la trascrizione in `.cache/` e il video che esiste, e
ristampa i confini da controllare con le loro anteprime. È un comando di sola
lettura come `--cache-du` (nessun download, nessun modello caricato) e funziona
anche sui report scritti prima del controllo incrociato: lì `mentions_off` non
esiste e viene ricalcolato dalle parole in cache. Sul report del 14/10 dice
"confini ancorati 8 su 9, confini stimati dal contenuto: la slide 10, nessuna
contraddizione fra annunci e confini", che è la verità di quella cache: nella
trascrizione greedy l'annuncio delle 16:56 non c'è più, quindi non c'è niente da
confrontare finché non lo recupera la ritrascrizione mirata.

### Riparazione automatica (verifica del video → nuovo confine)

Il controllo del video non si limita a segnalare: se un segmento mostra una
slide diversa da quella dichiarata e la slide mostrata è quella di un segmento
**adiacente**, il confine tra i due è fuori posto e la direzione dell'errore è
nota (il video mostra la slide successiva → il confine è in ritardo). La
pipeline sposta quel confine con il motore embedding, cercandolo solo nella
direzione indicata dall'evidenza (mai oltre l'istante in cui il frame è stato
estratto) e solo se il parlato offre una posizione migliore di quella attuale;
poi **rigenera il video** e ripete il controllo una volta. La sequenza delle
slide non cambia mai, quindi l'audio resta allineato.

Due casi non vengono "riparati" di proposito: la slide mostrata non è adiacente
(probabile problema di rendering, non di allineamento: l'avviso lo dice) e il
parlato non offre nessuna posizione migliore. In entrambi i casi la timeline
resta intatta, gli spostamenti applicati finiscono in `sync_report.json`
(`repairs`) e restano visibili nel riepilogo finale. Con `--no-auto-repair` la
riparazione è disattivata e l'esito resta un semplice avviso.

> Richiede il file `llm_timeline_finale.json` nella cache (salvato a ogni
> run) e il video generato. Il percorso base è auto-rilevato dalla cartella
> dello script: funziona da qualsiasi copia del progetto, senza percorsi
> hardcoded.

---

