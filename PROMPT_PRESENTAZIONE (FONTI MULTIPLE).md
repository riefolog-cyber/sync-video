# Prompt NotebookLM — presentazione + materiali → podcast (variante)

> **Variante sperimentale, non validata.** Il percorso consigliato resta
> [`PROMPT_PRESENTAZIONE (PREDEFINITO).md`](<PROMPT_PRESENTAZIONE (PREDEFINITO).md>),
> con la presentazione come **UNICA fonte**: cinque run verdi consecutive, la
> ricetta congelata nel README. Questa variante serve a una prova, non all'uso
> normale. Non e' coperta da `test_prompts.py`, che verifica solo il file
> predefinito: se una regola sparisce da qui, nessun test se ne accorge.

Il problema che questa variante prova a risolvere non e' la qualita' del
contenuto — il deck di Fase 1 nasce gia' da tutti i materiali — ma il permesso di
usarli: il predefinito chiede di **rimuovere** le altre fonti perche' con una
seconda fonte il podcast spende minutaggio e attenzione fuori dal deck (run con
libro + presentazione: 3 slide mai discusse, ancore 2-12 su un deck da 15). Qui
si prova a tenerle, con due limiti espliciti: i materiali **non** toccano la
struttura (ordine, una sezione per pagina, annunci) e **non** portano numeri.

## Cosa cambia rispetto al predefinito

Tre cose, e solo tre. Il resto e' identico parola per parola, perche' cosi' una
prova fallita dice che la causa e' la seconda fonte e non una regola riscritta
per errore.

1. **La riga delle fonti.** "Nelle fonti tieni SOLO la presentazione, rimuovi le
   altre" diventa "tieni la presentazione e anche i materiali di studio".
2. **Una sezione nuova, "COME USARE GLI ALTRI MATERIALI".** L'esempio di ogni
   sezione (l'unico che c'e' sempre stato) puo' venire dai materiali, resta
   dentro la sezione della pagina in corso, e i materiali non aggiungono sezioni
   ne' giustificano un salto di pagina. Se un materiale contraddice la slide,
   vale la slide.
3. **Una voce nuova fra le ancore: "DAI MATERIALI NON PORTARE NUMERI".** Non e'
   prudenza generica: l'estrattore riconosce un annuncio quando trova la parola
   "slide" e un numero di pagina plausibile entro poche parole (`timeline.py`,
   finestra di 8 parole, in entrambe le forme "slide N" e "N ... slide"). Un
   "capitolo 3" o un "paragrafo 12" detto accanto alla parola "slide" diventa un
   annuncio falso, e il video mostra la pagina sbagliata. I numeri grandi (un
   anno) non sono numeri di pagina e si escludono da soli; i piccoli no.

**Fase 1: identica al predefinito**, copiata qui perche' il file resti
autosufficiente. Se cambia nel predefinito, va riportata anche qui: sono due
copie da tenere allineate, ed e' un costo dichiarato di questa variante.

## Fase 1 — La presentazione (incolla in "Studio → Slide Deck")

```
Crea una presentazione: una slide per argomento, senza dividere un argomento in due slide né accorparne due in una. Ogni pagina deve poter diventare una sezione breve del podcast. Testo rigorosamente solo in italiano. Numera ogni slide con il numero della sua posizione, dalla 1: niente copertina senza numero. Il numero compare una sola volta per pagina, in basso a sinistra. Il video sincronizza sul numero stampato sulla pagina: se quel numero non è quello annunciato, il video mostra un numero che il podcast non sta dicendo.
```

Scarica la presentazione e ricaricala nelle fonti **insieme agli altri
materiali**: il predefinito qui chiede di lasciare la presentazione come unica
fonte, la variante no. La presentazione comanda, gli altri materiali
arricchiscono.

## Fase 2 — Il podcast (incolla in "Personalizza" → "Istruzioni")

```
La presentazione è la spina dorsale: segui le sue pagine in ordine, una sezione per pagina, TUTTE le pagine dalla prima all'ultima. Nelle fonti tieni la presentazione e anche i materiali di studio: la presentazione decide ordine, numero e confini delle sezioni, gli altri materiali riempiono il loro contenuto. Non rileggere il testo delle slide: sintetizzalo e arricchiscilo.

COME USARE GLI ALTRI MATERIALI
- L'esempio di ogni sezione può venire dai materiali di studio: un caso, un dato, una citazione. Resta l'unico esempio di sempre.
- L'esempio sta DENTRO la sezione della pagina che stai trattando: parla della pagina in corso, non dell'argomento del materiale. Non introdurre argomenti di altre pagine.
- I materiali non aggiungono sezioni, non ne sostituiscono una e non giustificano saltare una pagina. Se per una pagina i materiali non hanno niente, quella sezione si sviluppa con la slide e basta.
- Se un materiale contraddice la slide, vale la slide: è quella che si vede.

Ogni sezione:
1. si apre con la frase isolata "Passiamo alla slide N.", con punto fermo, detta sempre dallo stesso conduttore e seguita subito dall'esempio, dove N è il numero vero della pagina (mai un segnaposto), dalla seconda in poi;
2. sviluppa il contenuto con esempi concreti, senza limitarti a elencare i punti della slide, ma restando breve: un solo esempio, niente divagazioni e niente elenchi di punti.

Niente parole di riempimento e niente indicazioni di scena lette ad alta voce: non dire mai "pausa", "silenzio" o "un attimo". Tra una sezione e l'altra si passa diretti, senza segnali di attesa.

ANCORE: valgono più della fluidità del testo. Il video si sincronizza sulle frasi in cui dichiari il numero di pagina.

- In ordine, una volta sola: se le pagine sono N, gli annunci sono N-1 dal 2 al N, senza salti e senza ripetizioni. L'ultima pagina si annuncia come le altre: non è una conclusione, è una sezione.

- MAI RICHIAMARE una pagina già trattata: il richiamo è fuori ordine e fa perdere la transizione vera. Se torni su un concetto di una pagina precedente, non ne ripetere il numero, dillo con le parole. Non "guarda sempre slide 3" ma "come dicevamo, lo stesso schema".

- Un numero solo nella frase che apre la sezione, mai altrove, almeno una frase di distanza da "slide". Mai "slide 1" in apertura (la prima pagina parte già all'inizio), mai il numero totale di pagine. Nel finale non elencare mai i numeri già annunciati: per verificare il conteggio, conta senza scriverli.

- DAI MATERIALI NON PORTARE NUMERI: niente numeri di capitolo, paragrafo, pagina o conteggi. Un numero detto vicino alla parola "slide" viene letto come l'annuncio di quella pagina, e il video mostra la pagina sbagliata. Per citare una fonte basta dire "nel testo che abbiamo letto".

Sezioni di lunghezza simile: ogni sezione dura 60-180 secondi, nessuna sotto 45 secondi e nessuna oltre il doppio della media. Se per eccezione una pagina è un semplice elenco, dagli comunque una sezione di almeno 60 secondi con un esempio: non saltarla mai, un salto sposta tutti i numeri dopo.

Se il tempo stringe, accorcia l'esempio e non la sezione: un annuncio perso fa leggere sul video la pagina sbagliata, e non si vede.

TONO E STILE DEL DIBATTITO
TARGET: classe di scuola secondaria di secondo grado (14-19 anni), lezione di IRC.
TONO: frasi corte, linguaggio fresco e immediato, esempi dalla quotidianità dei ragazzi (scuola, amicizia, famiglia, social); niente tecnicismi, niente termini stranieri non spiegati.
DINAMICA: due conduttori in scambio rapido, senza monologhi; uno solleva dubbi da studente, l'altro chiarisce senza giudicare. Rivolgiti sempre direttamente agli studenti.
FOCUS: concetti con ricaduta etica o esistenziale; niente tono moralistico: proponi i concetti come domande, non come verità.
CHIUSA: chiudi ogni sezione con una domanda aperta e una frase riassuntiva (senza "in sintesi" a ogni giro); l'ultima sezione si conclude con un saluto finale breve.
```

Scarica l'audio.

## Come si giudica la prova

I criteri vanno fissati **prima** di ascoltare il podcast, altrimenti la
discussione si sposta sulla qualita' del contenuto (che con piu' materiali
sembra sempre migliore) invece che sulla sincronizzazione.

| Deve restare | Dove si legge |
|---|---|
| Copertura ancore N-1 su N-1, sopra il gate `--min-anchor-coverage` (50%) | riepilogo, `Confini ancorati` |
| Annunci fuori timeline: nessuno | riepilogo, `announcements_off_timeline` |
| Frame check pieno | riepilogo, `Controllo del video finito` |
| Nessuna pagina povera | riepilogo, `starved_slides` |
| Durate 60-180s, nessuna sotto 45s | riepilogo, tabella durate |
| Uscita 0, nessuna interruzione | un'interruzione e' il risultato, non un intoppo da aggirare |

| Puo' peggiorare, e NON e' un veto | Perche' |
|---|---|
| Fiducia del motore (`avg_z`, `weak_signal`) | il parlato racconta anche materiale che non sta sulla pagina: la somiglianza con la pagina cala **per costruzione**. E' il costo atteso dell'arricchimento, non una prova che la numerazione e' sbagliata |
| `discarded_citations` | i materiali portano numeri propri. Se aumentano, la regola "niente numeri" non e' stata seguita: e' il segnale piu' informativo della prova |

La prova va fatta **a scala**, con la stessa presentazione e lo stesso prompt:
solo presentazione (baseline) / +1 materiale / +tutti. Un salto di copertura fra
il primo e il secondo gradino dice che e' quel materiale a spostare il
discorso; se crolla solo con tutti, il problema e' la quantita' di fonti, e la
risposta non e' una regola in piu' nel prompt.

Se la prova riesce e la variante diventa il percorso consigliato, i posti da
allineare sono: questo file, `PROMPT_PRESENTAZIONE (PREDEFINITO).md`, i test in
`test_prompts.py` (le due voci che oggi vietano l'innesto: "no contaminazione" e
"fonte unica") e le frasi "UNICA fonte" in `README.md`,
`docs/funzionamento.md` e `promemoria prima installazione.md`. Nessuna riga
della pipeline: la sincronizzazione non guarda le fonti, guarda gli annunci.
