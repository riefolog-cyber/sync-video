# Prompt per NotebookLM — PODCAST → PRESENTAZIONE

> **Quando usare questo flusso**: quando vuoi un podcast più naturale e meno
> rigido, oppure quando il flusso A (presentazione → podcast) ha prodotto un
> avviso "segnale debole" (slide troppo simili tra loro). Qui la presentazione
> nasce DAL podcast: ogni slide corrisponde a una sezione realmente parlata,
> nello stesso ordine → allineamento 1:1 anche senza ancore "slide N".

## Fase 1 — Genera il podcast libero (senza vincoli di slide)

1. Seleziona TUTTE le fonti che vuoi usare.
2. In "Personalizza" → "Istruzioni" incolla il blocco qui sotto.
3. Scarica l'audio del podcast.

<!-- INIZIO BLOCCO DA INCOLLARE (Fase 1: audio) -->

   Dibattito a due conduttori che copre TUTTI gli argomenti delle fonti in
   ordine logico, procedendo per SEZIONI tematiche ben distinte: una sezione =
   un solo argomento, esaurito prima di passare al successivo. Non tornare su
   argomenti già trattati né anticipare quelli successivi.
   
   Per ogni sezione:
   1. apri annunciando l'argomento con parole chiare (usa i termini chiave delle fonti);
   2. sviluppa con esempi concreti, citando le fonti;
   3. chiudi con una domanda aperta e poi con una frase riassuntiva
      (senza ripetere ogni volta "in sintesi").
   
   NON usare riferimenti a slide, diapositive, capitoli o numeri di sezione:
   il podcast deve funzionare da solo, come conversazione libera.

   TONO E STILE DEL DIBATTITO
   ═══════════════════════════════════════════════════════════
   ▸ TARGET: Classe di scuola secondaria di secondo grado (14-19 anni), lezione di IRC.
   ▸ TONO: Frasi corte, linguaggio fresco e immediato, esempi dalla quotidianità dei giovani (scuola, amicizia, famiglia, social); zero tecnicismi e termini stranieri non spiegati.
   ▸ DINAMICA: Due conduttori in scambio rapido, senza monologhi; uno solleva dubbi da studente, l'altro chiarisce senza giudicare. Rivolgiti sempre direttamente agli studenti.
   ▸ FOCUS: Nodi con valenza educativa, etica, esistenziale o culturale; niente tono moralistico: proponi i concetti come domande, non come verità.
   ▸ INTRO: Breve (30-40 s) e già parte della prima sezione, senza annunciare una scaletta.
   ▸ CHIUSA: Concludi l'ultima sezione con un saluto finale breve.

<!-- FINE BLOCCO DA INCOLLARE -->


## Fase 2 — Genera la presentazione DERIVATA dal podcast

1. In NotebookLM, **aggiungi il podcast (l'audio scaricato) come fonte** insieme
   alle altre: senza la trascrizione la presentazione non può seguire le sezioni.
2. Genera la presentazione (**Studio → Slide Deck**) incollando il blocco qui sotto.
3. Scarica la presentazione e mettila nella cartella del progetto come
   `presentazione.pdf`.

<!-- INIZIO BLOCCO DA INCOLLARE (Fase 2: presentazione) -->

Crea una presentazione che segua ESATTAMENTE le sezioni della
   trascrizione del podcast nell'ordine in cui compaiono: UNA slide per
   sezione, con lo stesso numero di sezioni (niente fusioni, niente slide
   extra).

   OGNI SLIDE COMUNICA UNA SOLA IDEA CENTRALE, non elenca gli argomenti
   della sezione: il testo della slide è la 'spalla' del parlato, non il
   copione. Titolo breve e incisivo (max 8 parole) che contenga il termine
   chiave della sezione, pronunciato come nel podcast; sotto, al massimo
   3-4 punti molto brevi (max 6 parole ciascuno) con le parole chiave
   specifiche del parlato, evitando termini generici ripetuti sulle altre
   slide.

   VARA IL FORMATO tra le slide, alternando questi tipi (mai due uguali di
   seguito):
   - DICHIARAZIONE: titolo-affermazione forte + una frase chiave;
   - DOMANDA: titolo sotto forma di domanda aperta (quella che chiude la
     sezione nel podcast);
   - DATO / CONFRONTO: un numero o un confronto 'prima vs dopo' in evidenza
     (SOLO se il dato è presente nelle fonti, MAI inventarne);
   - ESEMPIO: un caso concreto dalla quotidianità degli studenti;
   - CITAZIONE: una frase breve e memorabile da ricordare;
   - SCHEMA: 3-4 parole chiave collegate tra loro (mappa concettuale).

   Regole visive: niente frasi lunghe né paragrafi; una slide non deve mai
   sembrare la fotocopia della precedente; titoli distintivi e specifici
   (mai 'Introduzione', 'Conclusioni', 'Argomento 2'); stesso tono fresco e
   diretto del podcast, adatto a studenti 14-19 anni. NUMERA OGNI SLIDE
   (1, 2, 3...) in un piccolo angolo in basso a sinistra, nell'ordine delle
   pagine. TESTO RIGOROSAMENTE SOLO IN ITALIANO.

<!-- FINE BLOCCO DA INCOLLARE -->

<!-- =====================================================================
     Da qui in giù NON va incollato in NotebookLM: sono note per te.
     ===================================================================== -->

## Controllo prima di generare (fallo davvero)

Questo passaggio è quello che il programma **non** può fare al posto tuo.

Apri il PDF e **conta le pagine**. Poi conta le sezioni del podcast (i cambi
di argomento, non le frasi). Le due numerazioni devono coincidere: è da lì
che nasce l'allineamento 1:1.

- **Pagine in meno delle sezioni** → il deck non può essere 1:1; il
  generatore ha fuso delle sezioni. Rigenera il deck chiedendo il conteggio.
- **Pagine in più** → ci sono pagine senza contenuto: verranno mostrate con
  durate brevi o spezzate. Rigenera.
- **Il numero stampato in basso a sinistra è solo per te**: non è un punto
  d'appoggio per il programma, serve a controllare a occhio l'ordine.

## Note per te (non incollare questa parte)

**L'assenza di "slide N" qui è attesa.** Il programma lo riconosce, avvisa
"Nessun riferimento 'slide N'…" e prosegue: non è un errore e non va corretto
rigenerando l'audio. L'allineamento che segue è **ordinato per contenuto**
(embeddings), ed è la modalità giusta per questo flusso: il deck nasce dalle
sezioni del podcast, quindi l'ordine coincide per costruzione.

**Due avvertenze operative.**

- **Non usare `--require-full-anchors`.** Serve al flusso A, dove ogni pagina
  deve essere annunciata. Qui le ancore sono escluse dal prompt per scelta, e il
  programma lo sa: l'opzione viene ignorata automaticamente in questo flusso.
- **L'LLM è spento.** Si lavora con i soli embeddings: più in fretta e senza
  9Router. Le durate sono quindi *stimate*, non misurate — approssimazione buona
  nel 1:1 di un deck derivato dal podcast, ma non una garanzia.

**Come leggere il referto.** In `.cache/sync_report.json` trovi
`quality.avg_z` e `weak_signal`. Con `weak_signal: true` le slide sono troppo
simili fra loro perché il solo contenuto le distingua: è il caso in cui questo
flusso **non** è la scelta giusta e conviene tornare al flusso A."

