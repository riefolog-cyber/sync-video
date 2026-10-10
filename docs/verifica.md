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

