# Prompt per NotebookLM — WORKFLOW 2: PRESENTAZIONE → PODCAST

Variante "generale": podcast multi-fonte che segue l'ordine di una presentazione
già pronta, arricchendo ogni sezione con le altre fonti.
Per il workflow complementare (podcast → presentazione, sincronizzazione 1:1)
usa [`PROMPT_NOTEBOOKLM PRIMA PODCAST.md`](PROMPT_NOTEBOOKLM%20PRIMA%20PODCAST.md).

---

## 0. Preparazione (obbligatoria)

Prima crea la presentazione e mettila nelle fonti nominata **PRESENTAZIONE**.
TESTO RIGOROSAMENTE E SOLO IN ITALIANO.

## 1. Indicazione da incollare in "Personalizza" → "Istruzioni" per generare audio (con TUTTE le fonti selezionate)

Struttura il podcast come un percorso che segue l'ordine del file
presentazione.pdf che trovi nelle fonti: procedi per slide/sezioni, e per
ognuna non limitarti a rileggere o parafrasare la presentazione: per ogni
punto cita riferimenti puntuali alle altre fonti, collega le sezioni tra loro
e aggiungi esempi concreti. Mantieni la spina dorsale della presentazione ma
arricchisci ogni sezione con il contenuto delle altre fonti.

ANCORE E STRUTTURA (FONDAMENTALE, OBBLIGATORIO):
- Segui le slide della presentazione in ORDINE RIGOROSO e CONSECUTIVO:
  slide 1, 2, 3, … fino all'ultima, senza mai saltarne una.
- All'inizio di OGNI sezione pronuncia esplicitamente il numero come
  transizione, SEMPRE IN CIFRE: es. "passiamo alla slide 2" oppure
  "siamo alla slide 2". Mai "la slide successiva" e mai "slide numero due".
- Pronuncia la parola "slide" (o "diapositiva") in modo CHIARO e separato
  dal numero: il sistema di trascrizione automatica la riconosce meglio.
- Se ti accorgi di aver saltato un numero, torna indietro e recuperalo subito.
- Per ogni sezione: 1) annuncia "slide N"; 2) cita il titolo della sezione;
  3) spiega i punti chiave (anche citando le altre fonti); 4) chiudi con una
  frase riassuntiva.
- Non anticipare contenuti di sezioni successive e non tornare indietro.

ESEMPIO DI TRANSAZIONE (schema da adattare, contenuti generici come segnaposto):
  "Okay, passiamo alla slide 3. [Titolo della sezione]. Le altre fonti
   aggiungono che [sintesi del punto centrale]... [punti chiave]...
   In sintesi, [frase riassuntiva della sezione]."
