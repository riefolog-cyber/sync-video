# Prompt per NotebookLM — PRESENTAZIONE → PODCAST

## 0\. Preparazione (obbligatoria)

1. Seleziona TUTTE le fonti che ti servono.
2. Genera la presentazione da **Studio → Slide Deck** con questo prompt:



> Crea una presentazione in italiano: una slide per argomento, senza dividere un argomento in due slide né accorparne due in una. Numera ogni slide nell'ordine delle pagine. Testo rigorosamente solo in italiano.


3. Scarica la presentazione generata e ricaricala nelle fonti come **PRESENTAZIONE**.

## 1\. Da incollare in "Personalizza" → "Istruzioni" per generare l'audio

<!-- INIZIO BLOCCO DA INCOLLARE: fino a "senza annunciare una scaletta". -->


La presentazione è la spina dorsale: segui le sue pagine in ordine. Non rileggere il testo delle slide: sintetizzalo e arricchiscilo con le altre fonti, con collegamenti ed esempi concreti. Le altre fonti servono SOLO ad approfondire la pagina corrente — non introdurre argomenti di altre pagine né cambiarne l'ordine.

## ANCORE — da rispettare alla lettera

Una sezione = una pagina del PDF, e ogni sezione si apre con la sua frase. Il video si sincronizza sulle frasi in cui dichiari il numero di pagina: una pagina senza la sua frase non può essere mostrata nel punto giusto, quindi queste regole valgono più della fluidità del testo.

- **Una frase per pagina, dalla seconda in poi, identica ogni volta:** *«Passiamo alla slide [numero della pagina].»* — la parola "slide" col suo numero (mai segnaposto, mai un esempio ripreso alla lettera), e subito dopo il contenuto di quella pagina. Sempre "slide N", mai "sezione N" o "capitolo N".

- **In ordine, una volta sola.** Se le pagine sono N, gli annunci sono N-1 dal 2 a N: niente salti, niente ripetizioni, niente ritorni a una pagina già trattata ("torniamo a questa idea", non "torniamo alla slide sette").

- ⛔ **MAI RICHIAMARE una pagina già trattata.** Questa è la regola che viene rotta più spesso, ed è quella che fa sbagliare di più il video. Se torni su un concetto di una pagina precedente, **non ne ripetere il numero**: dillo con le parole.

  | ❌ da non dire | ✅ da dire invece |
  |---|---|
  | «guarda sempre **slide 3**, applicano mercato» | «come dicevamo, applicano lo stesso schema» |
  | «**guarda slide 5** riporta cose molto positive» | «la tabella di prima dà una buona notizia» |
  | «qui **slide 3**, hanno nomi pazzeschi» | «qui, nomi pazzeschi» |

- **Un numero solo nella frase che apre la sezione**, mai altrove: almeno una frase di distanza da "slide". "qui i tre concetti sono chiari" e non "i tre concetti della slide"; "il ciclo ha quattro fasi" e non "la slide spiega il ciclo in quattro fasi"; niente ordinali ("il primo punto") accanto a "slide"; mai il numero totale di pagine o sezioni. Intro, passaggi e chiusura non nominano le pagine, e mai "slide 1" in apertura (la prima pagina parte già a 0.0s).

- **Ogni pagina deve avere una sezione che la sostenga.** Non limitarti ad annunciare la pagina e poi enunciarne il contenuto in una frase: sviluppalo con esempi concreti. Una sezione che si limita a pronunciare un elenco dura pochi secondi, e la pagina corrispondente nel video avrà una durata **costruita** dal pavimento di leggibilità, non misurata: si vedrà scorrere troppo veloce. Vale anche per le pagine "di indice" o "di schema": se la elenchi in tre secondi, o la allarghi con un punto che sviluppi davvero, o non la tratti come pagina autonoma.

- **Sezioni di lunghezza simile.** Evita che una pagina resti due minuti e un'altra pochi secondi: se una pagina è un semplice elenco, accorpalo alla pagina che la introduce e libera una sezione lunga per un argomento che meritava più spazio.

### Controllo prima di generare (fallo davvero)

Scorri tutte le frasi che contengono "slide" e conta quante volte compare ciascun numero: ogni numero da 2 a N deve comparire **una e una sola volta**, e subito prima del primo discorso su quella pagina. Se un numero manca o si ripete, correggi il testo e ricontrolla: è più economico che rigenerare tutto. Il totale non è un'ancora: "Le 14 slide di oggi" è un conteggio, dillo senza numero ("questa puntata copre tutti i passaggi").

## TONO E STILE
══════════════════════════════════════════════════════════
▸ TARGET: classe di scuola secondaria di secondo grado (14-19 anni), lezione di IRC.
▸ TONO: frasi corte, linguaggio fresco, esempi dalla quotidianità dei ragazzi (scuola, amicizia, famiglia, social); niente tecnicismi, niente termini stranieri non spiegati.
▸ DINAMICA: due conduttori in scambio rapido, senza monologhi; uno solleva dubbi da studente, l'altro chiarisce senza giudicare. Rivolgiti sempre direttamente agli studenti.
▸ FOCUS: nodi con valenza educativa, etica, esistenziale o culturale; niente tono moralistico — proponi i concetti come domande, non come verità.
▸ CHIUSURA DI SEZIONE: una domanda aperta e poi una frase riassuntiva (senza "in sintesi" a ogni giro).
▸ INTRO: breve, già dentro la prima sezione, senza annunciare una scaletta.

<!-- FINE BLOCCO DA INCOLLARE -->

<!-- =====================================================================
     Da qui in giù NON va incollato in NotebookLM: sono note per te.
     ===================================================================== -->

## Note per te (non incollare questa parte)

**Cosa fa il programma con le tue frasi.** La sincronizzazione fissa l'inizio di ogni pagina sull'**ultima** volta che ne sente il numero. È l'unico dato che usa, quindi è una buona cosa: se dici "slide N" una volta sola all'inizio di quella pagina, il confine è esatto al decimo di secondo. Ma è anche fragile: **un richiamo sposta la pagina in avanti**.

**Il richiamo è il difetto più costoso, e va evitato alla fonte.** Nel podcast di esempio il conduttore tornava su concetti già trattati e ne ripeteva il numero (*"guarda sempre slide 3, applicano mercato"*). La pagina partiva oltre un minuto dopo di quando era stata annunciata, e per tutto quel tempo restava a schermo la precedente. Da allora l'estrattore riconosce i richiami e li scarta, ma la regola ⛔ qui sopra resta la soluzione vera: previene il difetto invece di correggerlo.

**Cosa succede se sbagli comunque.** Non è un disastro, e il programma lo dice:

- *Pochi riferimenti trovati* → i log mostrano `riferimenti trovati / usati` e l'elenco delle slide senza ancora. La sincronizzazione passa per contenuto: regge bene se il podcast segue l'ordine delle slide, peggio se lo salta.
- *Richiami* → il log segnala `Slide richiamate più volte (richiamo ignorato...)`. È un'informazione, non un allarme: il confine resta quello giusto.
- *Slide che durano troppo o troppo poco* → compaiono sotto "Da controllare a mano", col tempo prima e dopo.

Nella run di riferimento, dopo questa revisione: 10 ancore su 11, fiducia alta, 12 segmenti su 12 corretti al frame, nessun dubbio. L'unica slide senza ancora era la 9, mai annunciata: un salto su undici.

**Se vuoi più ancore**, il problema è quasi sempre nel podcast, non nel programma: una pagina di cui non si parla, o di cui si parla senza nominarne il numero. Nel secondo caso è il motore che la colloca per contenuto, con buona approssimazione ma senza garanzia.
