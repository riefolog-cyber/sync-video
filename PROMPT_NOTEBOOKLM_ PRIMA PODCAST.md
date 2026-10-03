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

   OGNI SEZIONE DEVE ESSERE SVILUPPATA, NON SOLO ELENCATA: non limitarti a
   nominare gli elementi di una lista (le sette emozioni, le quattro fasi, i
   tre concetti). Sviluppa almeno uno di quegli elementi con un esempio
   concreto e una spiegazione, e cita gli altri. Una sezione che si limita a
   pronunciare un elenco dura pochi secondi: la pagina corrispondente avrà
   nel video una durata COSTRUITA invece che misurata, e scorrerà veloce.

   Sezioni di lunghezza simile: evita che una sezione duri due minuti e un'altra
   pochi secondi. Ogni pagina deve avere una sezione che la sostenga per un
   tempo sufficiente a essere letta.

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

   NON DEDICARE UNA PAGINA A UNA SEMPLICE ELENCAZIONE. Se una sezione del
   podcast si limita a nominare degli elementi senza svilupparli (per esempio
   enumera sette emozioni in pochi secondi), NON creare una pagina per quella
   lista: accorpa quel contenuto alla pagina del tema che la introduce, o
   scegli un aspetto da sviluppare davvero. Una pagina senza una sezione
   sostanziale nel podcast non può avere una durata reale nel video e
   scorrerà troppo veloce.

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

