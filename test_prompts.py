#!/usr/bin/env python3
"""I prompt di NotebookLM e i link del README non possono perdere le regole.

Esegui con: python -m unittest test_prompts -v

I due prompt restanti sono l'unica specifica che il motore di sincronizzazione
non puo' verificare da solo: il programma controlla il RESULTATO (quanti
confini sono ancorati, se le durate sono sbilanciate), ma non puo' sapere se il
prompt continua a dire al conduttore di non ripetere un numero, o di non fare
pagine-lista. Percio' le regole sono verificate qui, sul testo.

Dopo la cancellazione dei due prompt lungi (che avevano regole che le versioni
minime non avevano, e viceversa) questo controllo ne ha trovate subito tre
mancanti, piu' un blocco TONE che era divergente fra i due file. Serve anche a
notare che i due prompt non si allontanano fra loro.

Non e' un test sul comportamento di NotebookLM, che non e' riproducibile qui:
e' un test sul contratto scritto, che e' l'unica cosa che possiamo controllare.
"""

import re
import unittest
from pathlib import Path

RADICE = Path(__file__).resolve().parent
PROMPT = {
    "podcast": RADICE / "PROMPT_PODCAST.md",
    "presentazione": RADICE / "PROMPT_PRESENTAZIONE (PREDEFINITO).md",
}
README = RADICE / "README.md"


def _normale(testo: str) -> str:
    """Minuscole, spazi normalizzati, apostrofi e virgolette unificati."""
    testo = testo.lower()
    # Escape espliciti e non caratteri letterali: il file resta leggibile
    # uguale che il prompt usi l'apostrofo curvo o quello dritto, senza
    # dipendere da come l'editor lo ha salvato.
    for a, b in (("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'), ("\u201d", '"')):
        testo = testo.replace(a, b)
    return " ".join(testo.split())


def _blocchi(path: Path) -> list[str]:
    """I blocchi da incollare in NotebookLM, delimitati dalle cancellette.

    Le cancellette sono il confine: dentro ci finisce solo quello che l'utente
    incolla, e cio' che si puo' controllare (niente indentazione, niente
    grafica markdown, niente simboli che si degradino in console).
    """
    parti = re.findall(r"```\n(.*?)\n```", path.read_text(encoding="utf-8"), re.S)
    if not parti:
        raise AssertionError(f"{path.name}: nessun blocco da incollare")
    return parti


def _testo_da_incollare(path: Path) -> str:
    return _normale(" ".join(_blocchi(path)))


# Ogni voce: (file, espressione attesa, nome della regola). Le espressioni sono
# citate come nel file, accenti compresi: se la formulazione cambia, il test
# fallisce ed è giusto, perché vuol dire che la regola è stata riscritta e la
# verifica va aggiornata guardando il perché.
REGOLE: list[tuple[str, str, str]] = [
    # --- flusso B: struttura del podcast ---
    ("podcast", "una sezione = un solo argomento", "una sezione = un argomento"),
    ("podcast", "non tornare su argomenti già", "non tornare indietro"),
    ("podcast", "non anticipare quelli successivi", "non anticipare temi"),
    ("podcast", "si apre annunciando l'argomento con parole chiare", "apertura dichiarata"),
    ("podcast", "non limitarti a elencare gli elementi", "non solo elenchi"),
    ("podcast", "sezioni di lunghezza simile", "sezioni equilibrate"),
    ("podcast", "introduzione di 30-40 secondi", "intro dentro la sezione"),
    ("podcast", "non usare riferimenti a slide, diapositive, capitoli", "niente riferimenti a slide"),
    # --- flusso B: deck derivato dal podcast ---
    ("podcast", "una slide per sezione", "una slide per sezione"),
    ("podcast", "non fondere due sezioni e non aggiungere pagine", "niente fusioni"),
    ("podcast", "dagli comunque una pagina propria", "sempre una pagina"),
    ("podcast", "numera ogni slide", "slide numerate"),
    ("podcast", "rigorosamente solo in italiano", "deck in italiano"),
    # --- flusso A: deck ---
    ("presentazione", "una slide per argomento", "una slide per argomento"),
    ("presentazione", "mai pagine che sono solo un elenco di voci, nemmeno una", "no pagine-lista"),
    ("presentazione", "numera ogni slide", "slide numerate"),
    ("presentazione", "rigorosamente solo in italiano", "deck in italiano"),
    # --- flusso A: podcast con le ancore ---
    ("presentazione", "non rileggere il testo delle slide", "non leggere la slide"),
    ("presentazione", "non introdurre argomenti di altre pagine", "no contaminazione"),
    ("presentazione", "passiamo alla slide n", "frase di ancoraggio"),
    ("presentazione", "mai un segnaposto", "numero vero"),
    ("presentazione", "senza salti e senza ripetizioni", "annunci in ordine"),
    ("presentazione", "l'ultima pagina si annuncia come le altre", "annuncio anche dell'ultima"),
    ("presentazione", "richiamare una pagina già", "no richiami"),
    # La formulazione del motivo e' cambiata: il prompt diceva "un riferimento
    # fuori ordine che il programma deve scartare", ma il modello non ha un
    # programma e non lo vedra' mai. Ora il motivo e' solo la conseguenza che il
    # modello puo' evitare ("fuori ordine e fa perdere la transizione vera"), che
    # e' anche tutto quello che serve a capire la regola. La regola in se' e'
    # gia' protetta due volte, dalle due voci sopra e sotto.
    ("presentazione", "il richiamo è fuori ordine e fa perdere la transizione vera", "perche' dei richiami"),
    ("presentazione", "guarda sempre slide 3", "esempio di richiamo"),
    ("presentazione", "un numero solo nella frase che apre la sezione", "numeri isolati"),
    ("presentazione", 'mai "slide 1" in apertura', "no slide 1"),
    ("presentazione", "mai il numero totale di pagine", "no totale pagine"),
    ("presentazione", "sezioni di lunghezza simile", "sezioni equilibrate"),
    ("presentazione", "dagli comunque una sezione di almeno 60 secondi", "sempre una sezione"),
    # --- flusso A: regole nate dai run del 05/10, quando il podcast ha coperto
    # 6 e poi 4 sezioni su 14 pagine. Il sintomo era sempre uno solo: gli
    # annunci sparivano dalla metà in giù, non la numerazione. Da qui le tre
    # regole sotto: la corrispondenza è dichiarata senza condizionali, la
    # brevità è qualitativa (il numero di pagine lo sceglie NotebookLM), e
    # quando il tempo stringe si dice cosa sacrificare. ---
    ("presentazione", "una sezione per pagina, tutte le pagine dalla prima all'ultima", "tutte le pagine"),
    ("presentazione", "restando breve: un solo esempio, niente divagazioni", "sezioni brevi"),
    ("presentazione", "accorcia l'esempio e non la sezione", "priorita' all'annuncio"),
    ("presentazione", "ogni pagina deve poter diventare una sezione breve del podcast", "deck leggero"),
    ("presentazione", "al massimo 8 righe e 80 parole", "deck leggero quantificato"),
    ("presentazione", "ogni sezione dura 60-180 secondi", "durate quantificate (A)"),
    ("podcast", "ogni sezione dura 60-180 secondi", "durate quantificate (B)"),
    ("presentazione", "detta sempre dallo stesso conduttore", "ancora stesso conduttore"),
    ("podcast", "titolo univoco di 3-5 parole chiave", "titolo univoco"),
    ("podcast", "riprendi nel titolo di ogni slide le stesse 3-5 parole chiave", "titoli specchio"),
    ("presentazione", "non elencare mai i numeri già annunciati", "no recap numerato"),
    ("presentazione", "non saltarla mai, un salto sposta tutti i numeri dopo", "no salti di pagina"),
    # --- fonte unica: una seconda fonte sposta minutaggio e attenzione fuori
    # dal deck (run con libro + presentazione: 3 slide mai discusse, ancore
    # 2-12 su un deck da 15). Nel flusso A il podcast usa solo la
    # presentazione, nel B il deck usa solo il podcast. ---
    ("presentazione", "nelle fonti tieni solo la presentazione", "fonte unica (A)"),
    ("podcast", "usa solo il podcast come fonte", "fonte unica (B)"),
    # --- numero pagina unico: il tema di Slide Deck ristampa il numero in
    # calce oltre a quello nel contenuto (doppio numero su ogni slide).
    # Non rompe la sync (il controllo legge la posizione) ma sporca il video.
    # La posizione "in basso a sinistra" è un desiderio, non un vincolo che
    # il tema rispetti sempre: l'unica cosa che conta è "una sola volta". ---
    ("presentazione", "il numero compare una sola volta per pagina", "numero unico (A)"),
    ("podcast", "il numero compare una sola volta per pagina", "numero unico (B)"),
    # --- grafica ricca: rimosso il divieto di infografiche/scritte decorative
    # dopo il confronto (regola preventiva mai validata; la prova decisiva e'
    # la fiducia del motore sui run). Resta l'arricchimento visivo. ---
    ("presentazione", "arricchisci ogni pagina con un elemento visivo pertinente", "grafica ricca (A)"),
    ("podcast", "arricchisci ogni pagina con un elemento visivo pertinente", "grafica ricca (B)"),
    # --- il conduttore non annuncia le pause. Il prompt A chiedeva una "pausa
    # prima dell'esempio" e NotebookLM la eseguiva alla lettera: nella trascrizione
    # del run del 09/10 la parola "Pausa." compare 6 volte in 19m47s, come
    # segmento isolato (maiuscola e punto: era un'indicazione di scena scritta,
    # letta ad alta voce dalla TTS) e quasi sempre esattamente su un cambio di
    # sezione, cioe' dove la si era chiesta. Rumore per l'ascoltatore, e rumore
    # per il motore: un blocco fatto di una parola di riempimento non ha
    # contenuto semantico, quindi diluisce il segnale che separa le sezioni.
    # Vale per i due flussi, perche' i due prompt generano entrambi il podcast. ---
    ("presentazione", "niente parole di riempimento e niente indicazioni di scena", "no pause (A)"),
    ("podcast", "niente parole di riempimento e niente indicazioni di scena", "no pause (B)"),
    # --- flusso B: due pagine senza sezione (run del 04/10) hanno rotto il
    # video peggio di qualunque altro difetto: la 13 si e' presa quattro
    # minuti di materiale delle altre. Il deck aggiungeva pagine di sintesi e
    # la numerazione non vietava la copertina (vedi commit 02926a7). ---
    ("podcast", "non aggiungere pagine di sintesi, riepilogo o conclusione", "niente pagine di sintesi"),
    ("podcast", "niente copertina senza numero", "niente copertina"),
    ("presentazione", "niente copertina senza numero", "niente copertina"),
    # --- tono, presente in entrambi e identico ---
    ("podcast", "tono e stile del dibattito", "blocco tono (B)"),
    ("presentazione", "tono e stile del dibattito", "blocco tono (A)"),
]

# Scelte deliberate del 04/10, non dimenticanze. Queste regole sono state tolte
# dai prompt e NON sono piu' richieste; il fatto resta scritto perche' il peso
# delle parole del deck e' diverso nei due flussi.
REGOLE_RIMOSSE = [
    ("titoli non generici", 'mai "Introduzione", "Conclusioni", "Argomento 2"'),
    ("parole distinctive", "evitando termini generici ripetuti sulle altre pagine"),
    ("una idea per slide", "una sola idea centrale"),
]
# Nel flusso A le ancore sono le frasi pronunciate: le parole del deck non
# posizionano nulla e la regola serve solo alla qualita' della slide, quindi
# si può togliere. Nel flusso B invece sono l'UNICO segnale, perche' li' i
# confini si deducono dal contenuto: senza questa regola il deck torna a essere
# "tutte le slide sullo stesso tema", che e' la situazione in cui il motore non
# distingue le sezioni (ed e' cio' che ha reso il primo run non sincronizzabile).


class TestRegolePrompt(unittest.TestCase):
    """Ogni regola che protegge la sincronizzazione deve restare scritta."""

    def test_ogni_regola_e_presente(self):
        mancanti = [
            f"{PROMPT[flusso].name}: {nome}"
            for flusso, atteso, nome in REGOLE
            if _normale(atteso) not in _testo_da_incollare(PROMPT[flusso])
        ]
        self.assertEqual(mancanti, [], "regole sparite dai prompt")

    def test_i_due_prompt_esistono(self):
        for percorso in PROMPT.values():
            self.assertTrue(percorso.exists(), f"manca {percorso.name}")

    def test_nessuna_regola_rimossa_torna_indietro(self):
        """Le regole tolte non devono riapparire per sbaglio.

        Sono state levate di proposito. Se una riappare, il prompt e' stato
        modificato da qualcuno che non sapeva della scelta, e il peso delle
        parole del deck cambia fra i due flussi (vedi REGOLE_RIMOSSE).
        """
        tornate = [
            nome
            for nome, frammento in REGOLE_RIMOSSE
            for testo in (_testo_da_incollare(p) for p in PROMPT.values())
            if _normale(frammento) in testo
        ]
        self.assertEqual(tornate, [], "regole tolte di proposito, riapparse")


class TestCoerenzaTraPrompt(unittest.TestCase):
    """I due prompt non devono divergere: il TONE e' la parte che si copia."""

    def _tono(self, percorso: Path) -> str:
        testo = percorso.read_text(encoding="utf-8")
        m = re.search(r"TONO E STILE DEL DIBATTITO(.*?)```", testo, re.S)
        self.assertIsNotNone(m, f"{percorso.name}: blocco TONE non trovato")
        return _normale(m.group(1))

    def test_blocco_tono_identico(self):
        # Le due versioni lunghe avevano blocchi quasi identici ma non uguali
        # (una chiudeva con un saluto finale, l'altra con una domanda aperta), e
        # la differenza non serviva a nessuno.
        self.assertEqual(self._tono(PROMPT["podcast"]), self._tono(PROMPT["presentazione"]))

    def test_entrambi_i_prompt_hanno_un_blocco_tono(self):
        for percorso in PROMPT.values():
            self.assertIn("TONO E STILE DEL DIBATTITO", percorso.read_text(encoding="utf-8"))


class TestBlocchiIncollabili(unittest.TestCase):
    """Cosa finisce nel campo di testo di NotebookLM deve esserepulito.

    Non sono pedanterie: 4 spazi iniziali diventano un blocco di codice se
    l'input viene interpretato come markdown, e i simboli non-ASCII si degradano
    in console Windows (e in copia-incolla). Le frecce dei titoli sono fuori dai
    blocchi e non sono un problema: non raggiungono mai NotebookLM.
    """

    def test_nessuna_riga_con_quattro_spazi(self):
        offending = [
            f"{percorso.name}: {riga!r}"
            for percorso in PROMPT.values()
            for blocco in _blocchi(percorso)
            for riga in blocco.splitlines()
            if riga.startswith("    ")
        ]
        self.assertEqual(offending, [], "righe che diventerebbero blocco di codice")

    def test_nessuna_grafica_markdown_dentro_ai_blocchi(self):
        # #, **, ═, ▸ non hanno senso in un campo di testo: arrivano al
        # modello come spazzatura.
        grafica = re.compile(r"^#|\*\*|═|▸", re.M)
        offending = [
            f"{percorso.name}: {riga.strip()[:40]!r}"
            for percorso in PROMPT.values()
            for blocco in _blocchi(percorso)
            for riga in blocco.splitlines()
            if grafica.search(riga)
        ]
        self.assertEqual(offending, [], "grafica markdown dentro un blocco")

    def test_nessun_simbolo_fuori_dal_testo_italiano(self):
        # Solo lettere accentate e punteggiatura ASCII: tutto il resto e' un
        # rischio di codifica, e non aggiunge istruzione.
        vietati = {
            c
            for percorso in PROMPT.values()
            for blocco in _blocchi(percorso)
            for c in blocco
            if ord(c) > 127 and not c.isalpha()
        }
        self.assertEqual(
            sorted(vietati), [], "simboli non-italiani dentro i blocchi da incollare"
        )

    def test_nessun_passo_oltre_una_pagina_di_istruzioni(self):
        # NotebookLM non ha un limite dichiarato, ma un prompt oltre qualche
        # migliaio di caratteri compete con i sorgenti. Si sta larghi.
        #
        # Il tetto e' per BLOCCO, non per file. I due blocchi di uno stesso file
        # non vengono mai incollati insieme: Fase 1 va nel generatore di slide,
        # Fase 2 nelle istruzioni dell'audio, e sono due sessioni NotebookLM
        # diverse. Il confronto con i sorgenti avviene mentre ne e' vivo uno solo,
        # quindi sommarli penalizzava un prompt di 700 caratteri per colpa di un
        # altro blocco dello stesso file. Il margine che ne risultava era cosi'
        # stretto da costringere a scegliere fra allentare il tetto e tagliare
        # regole utili.
        for percorso in PROMPT.values():
            for i, blocco in enumerate(_blocchi(percorso), start=1):
                caratteri = len(blocco.strip())
                self.assertLess(
                    caratteri,
                    4000,
                    f"{percorso.name}: blocco {i} da {caratteri} caratteri",
                )


class TestRiferimentiReadme(unittest.TestCase):
    """Ogni link del README a un file locale deve esistere.

    Cancellando i due prompt lungi il README si e' riempito di link rotti in
    sette punti: links rotto in un documento che si dovrebbe leggere per capire
    il funzionamento e' peggio di un link assente.
    """

    def test_i_link_locali_esistono(self):
        testo = README.read_text(encoding="utf-8")
        # [etichetta](<percorso>) e [etichetta](percorso)
        candidati = re.findall(r"\]\((?:<([^>]+)>|([^)\s]+))\)", testo)
        rotti = []
        for con_cani, senza_cani in candidati:
            target = con_cani or senza_cani
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if not (RADICE / target).exists():
                rotti.append(target)
        self.assertEqual(rotti, [], "link del README a file inesistenti")

    def test_il_readme_cita_i_prompt_che_esistono(self):
        testo = README.read_text(encoding="utf-8")
        for percorso in PROMPT.values():
            self.assertIn(percorso.name, testo, f"{percorso.name} non e' citato nel README")


if __name__ == "__main__":
    unittest.main()
