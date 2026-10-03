# Prompt NotebookLM — presentazione → podcast (PREDEFINITO)

> **Flusso A, consigliato.** La presentazione viene prima e il podcast la
> segue dichiarando il numero di ogni pagina: i cambi di slide sono **misurati**,
> non dedotti. Costa un ciclo in più e un podcast più dichiarato.
> Se invece ti serve l'audio prima e il deck derivato dal parlato, c'è
> [`PROMPT_MINIMO_PODCAST.md`](<PROMPT_MINIMO_PODCAST.md>) — ma i confini li
> stima e possono cadere minuti fuori posto.

## Fase 1 — La presentazione (incolla in "Studio → Slide Deck")

```
Crea una presentazione: una slide per argomento, senza dividere un argomento in due slide né accorparne due in una. Non creare pagine che sono solo un elenco di voci. Numera ogni slide nell'ordine delle pagine. Testo rigorosamente solo in italiano.

Titolo distintivo e breve (max 8 parole) con il termine specifico dell'argomento, mai "Introduzione", "Conclusioni", "Argomento 2". Sotto, 3-4 punti molto brevi con le parole chiave, evitando termini generici ripetuti sulle altre pagine.
```

Scarica la presentazione e **ricaricala nelle fonti come PRESENTAZIONE**.

## Fase 2 — Il podcast (incolla in "Personalizza" → "Istruzioni")

```
La presentazione è la spina dorsale: segui le sue pagine in ordine, una sezione per pagina. Non rileggere il testo delle slide: sintetizzalo e arricchiscilo con esempi concreti. Le altre fonti servono SOLO ad approfondire la pagina corrente: non introdurre argomenti di altre pagine.

Ogni sezione:
1. si apre con la frase "Passiamo alla slide N.", dove N è il numero vero della pagina (mai un segnaposto), dalla seconda in poi;
2. sviluppa il contenuto con esempi concreti, senza limitarti a elencare i punti della slide.

ANCORE: valgono più della fluidità del testo. Il video si sincronizza sulle frasi in cui dichiari il numero di pagina.

- In ordine, una volta sola: se le pagine sono N, gli annunci sono N-1 dal 2 al N, senza salti e senza ripetizioni.

- MAI RICHIAMARE una pagina già trattata. Il video colloca ogni pagina nell'ultimo momento in cui ne sente il numero, quindi un richiamo la sposta in avanti anche di un minuto. Se torni su un concetto di una pagina precedente, non ne ripetere il numero, dillo con le parole. Non "guarda sempre slide 3" ma "come dicevamo, lo stesso schema".

- Un numero solo nella frase che apre la sezione, mai altrove, almeno una frase di distanza da "slide". Mai "slide 1" in apertura (la prima pagina parte già all'inizio), mai il numero totale di pagine.

Sezioni di lunghezza simile: evita che una sezione duri due minuti e un'altra pochi secondi. Se una pagina è un semplice elenco, nonarle una sezione autonoma: la sua durata non potrà essere misurata.

TONO E STILE DEL DIBATTITO
TARGET: classe di scuola secondaria di secondo grado (14-19 anni), lezione di IRC.
TONO: frasi corte, linguaggio fresco e immediato, esempi dalla quotidianità dei ragazzi (scuola, amicizia, famiglia, social); niente tecnicismi, niente termini stranieri non spiegati.
DINAMICA: due conduttori in scambio rapido, senza monologhi; uno solleva dubbi da studente, l'altro chiarisce senza giudicare. Rivolgiti sempre direttamente agli studenti.
FOCUS: concetti con ricaduta etica o esistenziale; niente tono moralistico: proponi i concetti come domande, non come verità.
CHIUSA: chiudi ogni sezione con una domanda aperta e una frase riassuntiva (senza "in sintesi" a ogni giro); l'ultima sezione si conclude con un saluto finale breve.
```

Scarica l'audio.

## Controllo prima di generare (fallo davvero)

Fuori dal blocco: questo passaggio è per te, non per NotebookLM.

Scorri le frasi che contengono "slide": ogni numero dalla 2 all'ultima pagina
deve comparire **una e una sola volta**. Se un numero manca o si ripete,
correggi il testo e ricontrolla — è più economico che rigenerare tutto.

Poi guarda nel riepilogo del programma: `Confini ancorati: 6 su 6` per un deck
da 7 pagine. Se è meno, qualche pagina non ha annunciato la propria frase:
quel confine è una stima e va ascoltato.

Non fidarti dei numeri stampati sulle pagine: se il generatore non li mette,
diciamolo. Il dato che conta è quello nel riepilogo.