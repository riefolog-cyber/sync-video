# Prompt NotebookLM — versione MINIMA (podcast → presentazione)

> **Cos'è e cosa non è.** Qui c'è il minimo indispensabile perché il video
> risulti sincronizzato. **Non è un prompt di stile**: non c'è tono, non
> c'è formato delle slide, non c'è istruzione sulla lingua. Se ti serve la
> lezione di IRC con il tono giusto, incolla questo e **aggiungi il blocco
> TONO E STILE** della versione lunga
> ([`PROMPT_NOTEBOOKLM_ PRIMA PODCAST.md`](PROMPT_NOTEBOOKLM_ PRIMA PODCAST.md)).
>
> **Perché la versione lunga esiste.** Ha più regole perché copre i casi in
> cui il podcast non segue il deck (richiami, numeri pronunciati per
> sbaglio, sezioni che si elencano). Se parti da qui e funziona, non ti
> servono.

## Fase 1 — Il podcast (incolla in "Personalizza" → "Istruzioni")

<!-- INIZIO BLOCCO DA INCOLLARE (Fase 1: audio) -->

   Dibattito a due conduttori che copre tutti gli argomenti delle fonti
   per SEZIONI: una sezione = un solo argomento, sviluppato con esempi
   concreti e concluso prima di passare al successivo.

   Non limitarti a ELENCARE gli elementi di un argomento (le sette
   emozioni, le quattro fasi): sviluppa almeno uno di quelli elementi con
   una spiegazione. Una sezione che si limita a pronunciare una lista dura
   pochi secondi, e la pagina corrispondente nel video non potrà avere una
   durata reale.

   Non usare riferimenti a slide, diapositive o numeri di sezione.

<!-- FINE BLOCCO DA INCOLLARE -->

Scarica l'audio.

## Fase 2 — La presentazione (incolla in "Studio → Slide Deck")

Carica **l'audio come fonte** insieme alle altre: senza la trascrizione il
deck non può seguire le sezioni.

<!-- INIZIO BLOCCO DA INCOLLARE (Fase 2: presentazione) -->

   Crea una presentazione che segua le sezioni della trascrizione
   nell'ordine in cui compaiono: UNA slide per sezione, con lo stesso numero
   di sezioni (niente fusioni, niente slide extra).

   Non creare una pagina per una semplice elencazione: se una sezione
   nomina degli elementi senza svilupparli, accorpa quel contenuto alla
   pagina del tema che la introduce.

<!-- FINE BLOCCO DA INCOLLARE -->

Scarica il deck e mettilo nella cartella come `presentazione.pdf`.

---

## Controllo prima di generare (2 minuti, fallo davvero)

Questa è l'unica cosa che il programma **non** può fare al posto tuo.

1. **Conta le pagine** del PDF.
2. **Conta le sezioni** del podcast (i cambi di argomento, non le frasi).

Se non coincidono, il video non può essere 1:1. Rigenera il deck: l'audio
si riusa, quindi è veloce.

---

## Note per te (non incollare)

**Cosa fa il programma con un podcast senza "slide N".** L'assenza è
**attesa**: il programma la riconosce, avvisa e prosegue. Non è un errore e
non va corretto rigenerando l'audio. L'allineamento successivo è ordinato
per contenuto, che è la modalità giusta qui: il deck nasce dalle sezioni
del podcast, quindi l'ordine coincide per costruzione.

**Due avvertenze.**

- **Non usare `--require-full-anchors`**: qui le ancore sono escluse dal
  prompt per scelta e il programma le ignora automaticamente.
- **L'LLM è spento**: si lavora con i soli embeddings. Le durate sono
  *stimate*, non misurate.

**Se nel report compare `starved_slides`**, c'è una pagina senza tempo
proprio nel podcast: rigenera il **deck** chiedendo una sezione sviluppata
per quella pagina.