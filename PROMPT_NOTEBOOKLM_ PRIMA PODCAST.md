# Prompt per NotebookLM — PODCAST → PRESENTAZIONE

> **Quando usare questo flusso**: quando vuoi un podcast più naturale e meno
> rigido, oppure quando il flusso A (presentazione → podcast) ha prodotto un
> avviso "segnale debole" (slide troppo simili tra loro). Qui la presentazione
> nasce DAL podcast: ogni slide corrisponde a una sezione realmente parlata,
> nello stesso ordine → allineamento 1:1 anche senza ancore "slide N".

## Fase 1 — Genera il podcast libero (senza vincoli di slide)

1. Seleziona TUTTE le fonti che vuoi usare.
2. In "Personalizza" → "Istruzioni" incolla:

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

3. Scarica l'audio del podcast.

## Fase 2 — Recupera la trascrizione dal programma

La pipeline trascrive già il podcast a ogni run: la trascrizione più recente è
in `.cache/transcript_*.json` (campo `words_raw`, oppure usa il testo dei
blocchi). Apri il file, copia il testo parlato in ordine e salvalo come PDF o
documento (una sezione per paragrafo). In alternativa, chiedi a NotebookLM di
trascrivere/riassumere l'audio appena generato.

> Suggerimento: `python main.py --dry-run` genera la trascrizione SENZA
> creare il video: serve proprio per preparare questa fase.

## Fase 3 — Genera la presentazione DERIVATA dal podcast

1. Aggiungi alle fonti la **trascrizione del podcast** (Fase 2) e tienila
   selezionata SOLO insieme a eventuali fonti utili per arricchire il testo
   delle slide.
2. Genera la presentazione (**Studio → Slide Deck**) incollando:

   "Crea una presentazione che segua ESATTAMENTE le sezioni della
   trascrizione del podcast nell'ordine in cui compaiono: UNA slide per
   sezione, con lo stesso numero di sezioni (niente fusioni, niente slide
   extra). Ogni slide ha: titolo chiaro (= argomento della sezione) ed elenco
   puntato con LE PAROLE CHIAVE specifiche usate nel parlato di quella
   sezione, evitando termini generici ripetuti sulle altre slide. NUMERA OGNI
   SLIDE (1, 2, 3...) in un piccolo angolo in basso a sinistra, nell'ordine
   delle pagine. TESTO RIGOROSAMENTE SOLO IN ITALIANO."

3. Scarica la presentazione e mettila nella cartella del progetto come
   `presentazione.pdf` (dopo aver rinominato quella vecchia).

## Cosa cambia nella pipeline (nota tecnica)

- Il podcast NON contiene ancore "slide N" → il programma usa il **flusso
  semantico puro** (`--flow free`): i confini sono stimati con gli embedding.
  Con una presentazione derivata 1:1 dal parlato il segnale è forte e
  l'allineamento affidabile, ma i confini sono meno precisi al secondo rispetto
  al flusso A.
- Se vuoi verificare prima del render: `python main.py --dry-run` e controlla
  similarità media e avvisi ("segnale debole" non dovrebbe apparire).
