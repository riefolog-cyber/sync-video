# Prompt NotebookLM — podcast → presentazione

> **Flusso B.** La presentazione nasce dal podcast: una slide per sezione, e
> l'audio si riusa se un errore ti fa rifare il deck. In compenso i confini sono
> **stimati** dal contenuto e possono cadere minuti fuori posto.
>
> Nel riepilogo del programma, se leggi **«segnale debole»**, i confini sono
> stime e non misure: guarda il video prima di darlo per buono. Ma non è un
> certificato: con una pagina senza sezione i confini sbagliano di minuti
> senza avvisi. L'unico controllo che vale è contare le sezioni del podcast e
> confrontarle con le pagine del deck. Se invece ti serve un confine esatto,
> usa
> [`PROMPT_PRESENTAZIONE (PREDEFINITO).md`](<PROMPT_PRESENTAZIONE (PREDEFINITO).md)>).

## Fase 1 — Il podcast (incolla in "Personalizza" → "Istruzioni")

```
Dibattito a due conduttori che copre tutti gli argomenti delle fonti per SEZIONI: una sezione = un solo argomento, sviluppato con esempi concreti e concluso prima di passare al successivo. Non tornare su argomenti già trattati e non anticipare quelli successivi.

Ogni sezione si apre annunciando l'argomento con parole chiare, usando i termini chiave delle fonti, con un titolo univoco di 3-5 parole chiave mai riusato in altre sezioni.

Non limitarti a elencare gli elementi di un argomento: sviluppa almeno uno di quelli elementi con una spiegazione. Una sezione fatta solo di elenco dura pochi secondi, e la pagina che vi corrisponde nel video non potrà avere una durata reale.

Sezioni di lunghezza simile: ogni sezione dura 60-180 secondi, nessuna sotto 45 secondi e nessuna oltre il doppio della media.

Introduzione di 30-40 secondi, già dentro la prima sezione e senza annunciare una scaletta: un preambolo non appartiene a nessuna sezione, quindi non può essere attribuito a nessuna pagina.

Non usare riferimenti a slide, diapositive, capitoli o numeri di sezione.

TONO E STILE DEL DIBATTITO
TARGET: classe di scuola secondaria di secondo grado (14-19 anni), lezione di IRC.
TONO: frasi corte, linguaggio fresco e immediato, esempi dalla quotidianità dei ragazzi (scuola, amicizia, famiglia, social); niente tecnicismi, niente termini stranieri non spiegati.
DINAMICA: due conduttori in scambio rapido, senza monologhi; uno solleva dubbi da studente, l'altro chiarisce senza giudicare. Rivolgiti sempre direttamente agli studenti.
FOCUS: concetti con ricaduta etica o esistenziale; niente tono moralistico: proponi i concetti come domande, non come verità.
CHIUSA: chiudi ogni sezione con una domanda aperta e una frase riassuntiva (senza "in sintesi" a ogni giro); l'ultima sezione si conclude con un saluto finale breve.
```

Scarica l'audio.

## Fase 2 — La presentazione (incolla in "Studio → Slide Deck")

Carica **l'audio come UNICA fonte (rimuovi tutte le altre)**: senza la
trascrizione il deck non può seguire le sezioni, e ogni altra fonte invita
pagine extra che il video non riesce a sincronizzare.

```
Crea una presentazione che segua le sezioni del podcast nell'ordine in cui compaiono: UNA slide per sezione, con lo stesso numero di sezioni. Usa SOLO il podcast come fonte: non aggiungere contenuti o pagine da altri materiali. Non fondere due sezioni e non aggiungere pagine. Se una sezione è un puro elenco, dagli comunque una pagina propria: mai accorpare, mai saltare. Riprendi nel titolo di ogni slide le stesse 3-5 parole chiave con cui la sezione si apre nel podcast. Non aggiungere pagine di sintesi, riepilogo o conclusione: ogni pagina corrisponde a una sezione pronunciata, e una pagina senza sezione è una pagina che il video non riesce a sincronizzare.

Numera ogni slide con il numero della sua posizione, dalla 1: niente copertina senza numero, perché una copertina sposta le etichette di una pagina e il video mostrerebbe un numero diverso da quello annunciato. Il numero compare una sola volta per pagina, in basso a sinistra. Testo rigorosamente solo in italiano.
```

Scarica il deck e mettilo nella cartella come `presentazione.pdf`.