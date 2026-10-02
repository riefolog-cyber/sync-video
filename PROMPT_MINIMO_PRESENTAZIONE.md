# Prompt NotebookLM — versione MINIMA (presentazione → podcast)

> **Cos'è e cosa non è.** Qui c'è il minimo indispensabile perché il video
> risulti sincronizzato. **Non è un prompt di stile**: non c'è tono, non
> c'è formato delle slide, non c'è istruzione sulla lingua. Se ti serve la
> lezione di IRC con il tono giusto, incolla questo e **aggiungi il blocco
> TONO E STILE** della versione lunga
> ([`PROMPT_NOTEBOOKLM_ PRIMA PRESENTAZIONE.md`](PROMPT_NOTEBOOKLM_ PRIMA PRESENTAZIONE.md)).
>
> **Perché questo flusso è diverso dall'altro.** Le regole sotto non
> servono solo per la qualità del video: servono perché il programma possa
> **misurare** i confini invece di dedurli. È l'unico flusso in cui il tempo
> di ogni slide è una frase pronunciata dallo speaker, e non una stima.

## Fase 1 — La presentazione (incolla in "Studio → Slide Deck")

<!-- INIZIO BLOCCO DA INCOLLARE (Fase 1: presentazione) -->

   Crea una presentazione in italiano: una slide per argomento, senza
   dividere un argomento in due slide né accorparne due in una. Numera
   ogni slide nell'ordine delle pagine. Testo rigorosamente solo in
   italiano.

<!-- FINE BLOCCO DA INCOLLARE -->

Scarica la presentazione e **ricaricala nelle fonti come PRESENTAZIONE**.

## Fase 2 — Il podcast (incolla in "Personalizza" → "Istruzioni")

<!-- INIZIO BLOCCO DA INCOLLARE (Fase 2: audio) -->

   La presentazione è la spina dorsale: segui le sue pagine in ordine,
   una sezione per pagina.

   Ogni sezione:
   1. si apre con la frase "Passiamo alla slide N." dove N è il numero della
      pagina, dalla seconda in poi. Una volta sola per numero: se torni su
      una pagina già trattata, dillo con le parole senza ripetere "slide N".
   2. sviluppa il contenuto della pagina con esempi concreti, usando anche
      le altre fonti. Non limitarti a leggere la slide né a elencare i suoi
      punti.

<!-- FINE BLOCCO DA INCOLLARE -->

Scarica l'audio.

---

## Controllo prima di generare (2 minuti, fallo davvero)

Rileggi le frasi che contengono "slide" e conta ogni numero: **ogni numero
dalla 2 all'ultima pagina deve comparire una volta sola**, all'inizio
della sua sezione. Se un numero manca o si ripete, correggi il testo e
ricontrolla — è più economico che rigenerare tutto.

Il totale delle pagine **non** è un'ancora: "le 14 slide di oggi" è un
conteggio, dillo senza numero.

---

## Note per te (non incollare)

**Cosa fa il programma con le tue frasi.** La sincronizzazione fissa
l'inizio di ogni pagina sull'**ultima** volta che ne sente il numero. Se lo
dici una volta sola all'inizio, il confine è esatto. Ma è fragile: **un
richiamo sposta la pagina in avanti**, anche di un minuto. Da qui la
regola sul "una volta sola".

**Se sbagli comunque, non è un disastro.**

- *Pochi riferimenti trovati* → i log mostrano quanti ne sono stati usati e
  quali slide sono senza ancora. La sincronizzazione passa per contenuto.
- *Richiami* → il log segnala "Slide richiamate più volte (richiamo
  ignorato)". È un'informazione, non un allarme: il confine resta giusto.
- *Slide troppo lunghe o troppo corte* → compaiono sotto "da controllare a
  mano", col tempo prima e dopo.

**Se invece ti serve la via senza regole**, il flusso
[`PROMPT_MINIMO_PODCAST.md`](PROMPT_MINIMO_PODCAST.md) fa la stessa cosa
senza ancore: il video viene comunque, ma i confini sono stimati invece che
misurati.