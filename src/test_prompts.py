#!/usr/bin/env python3
"""Il prompt di NotebookLM e i link del README non possono perdere le regole.

Esegui con: python -m unittest test_prompts -v

Il prompt è l'unica specifica che il motore di sincronizzazione non puo'
verificare da solo: il programma controlla il RESULTATO (quanti confini sono
ancorati, se le durate sono sbilanciate), ma non puo' sapere se il prompt
continua a dire al conduttore di non ripetere un numero, o di non fare
pagine-lista. Percio' le regole sono verificate qui, sul testo.

Non e' un test sul comportamento di NotebookLM, che non e' riproducibile qui:
e' un test sul contratto scritto, che e' l'unica cosa che possiamo controllare.
"""

import re
import unittest
from pathlib import Path

from hardware import project_root

# I .md che questo test verifica stanno in radice, i moduli in src/:
# `project_root()` trova la radice, `__file__.parent` darebbe src/.
RADICE = project_root()
PROMPT = RADICE / "PROMPT_PRESENTAZIONE (PREDEFINITO).md"
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


# Ogni voce: (espressione attesa, nome della regola). Le espressioni sono
# citate come nel file, accenti compresi: se la formulazione cambia, il test
# fallisce ed è giusto, perché vuol dire che la regola è stata riscritta e la
# verifica va aggiornata guardando il perché.
REGOLE: list[tuple[str, str]] = [
    # --- deck ---
    ("una slide per argomento", "una slide per argomento"),
    ("numera ogni slide", "slide numerate"),
    ("rigorosamente solo in italiano", "deck in italiano"),
    # --- podcast con le ancore ---
    ("non rileggere il testo delle slide", "non leggere la slide"),
    ("non introdurre argomenti di altre pagine", "no contaminazione"),
    ("passiamo alla slide n", "frase di ancoraggio"),
    ("mai un segnaposto", "numero vero"),
    ("senza salti e senza ripetizioni", "annunci in ordine"),
    ("l'ultima pagina si annuncia come le altre", "annuncio anche dell'ultima"),
    ("richiamare una pagina già", "no richiami"),
    # La formulazione del motivo e' cambiata: il prompt diceva "un riferimento
    # fuori ordine che il programma deve scartare", ma il modello non ha un
    # programma e non lo vedra' mai. Ora il motivo e' solo la conseguenza che il
    # modello puo' evitare ("fuori ordine e fa perdere la transizione vera"), che
    # e' anche tutto quello che serve a capire la regola. La regola in se' e'
    # gia' protetta due volte, dalle due voci sopra e sotto.
    ("il richiamo è fuori ordine e fa perdere la transizione vera", "perche' dei richiami"),
    ("guarda sempre slide 3", "esempio di richiamo"),
    ("un numero solo nella frase che apre la sezione", "numeri isolati"),
    ('mai "slide 1" in apertura', "no slide 1"),
    ("mai il numero totale di pagine", "no totale pagine"),
    ("sezioni di lunghezza simile", "sezioni equilibrate"),
    ("dagli comunque una sezione di almeno 60 secondi", "sempre una sezione"),
    # --- regole nate dai run del 05/10, quando il podcast ha coperto 6 e poi 4
    # sezioni su 14 pagine. Il sintomo era sempre uno solo: gli annunci
    # sparivano dalla metà in giù, non la numerazione. Da qui le tre regole
    # sotto: la corrispondenza è dichiarata senza condizionali, la brevità è
    # qualitativa (il numero di pagine lo sceglie NotebookLM), e quando il tempo
    # stringe si dice cosa sacrificare. ---
    ("una sezione per pagina, tutte le pagine dalla prima all'ultima", "tutte le pagine"),
    ("restando breve: un solo esempio, niente divagazioni", "sezioni brevi"),
    ("accorcia l'esempio e non la sezione", "priorita' all'annuncio"),
    ("ogni pagina deve poter diventare una sezione breve del podcast", "deck leggero"),
    ("ogni sezione dura 60-180 secondi", "durate quantificate"),
    ("detta sempre dallo stesso conduttore", "ancora stesso conduttore"),
    ("non elencare mai i numeri già annunciati", "no recap numerato"),
    ("non saltarla mai, un salto sposta tutti i numeri dopo", "no salti di pagina"),
    # --- fonte unica: una seconda fonte sposta minutaggio e attenzione fuori
    # dal deck (run con libro + presentazione: 3 slide mai discusse, ancore
    # 2-12 su un deck da 15). Il podcast usa solo la presentazione. ---
    ("nelle fonti tieni solo la presentazione", "fonte unica"),
    # --- numero pagina unico: il tema di Slide Deck ristampa il numero in
    # calce oltre a quello nel contenuto (doppio numero su ogni slide).
    # Non rompe la sync (il controllo legge la posizione) ma sporca il video.
    # La posizione "in basso a sinistra" è un desiderio, non un vincolo che
    # il tema rispetti sempre: l'unica cosa che conta è "una sola volta". ---
    ("il numero compare una sola volta per pagina", "numero unico"),
    # --- grafica ricca: il divieto di infografiche/scritte decorative era gia'
    # caduto il 04/10 (regola preventiva mai validata; la prova decisiva e' la
    # fiducia del motore sui run); il 15/10 e' caduta anche la richiesta
    # dell'elemento visivo. La voce sta ora in REGOLE_RIMOSSE. ---
    # --- il conduttore non annuncia le pause. Il prompt chiedeva una "pausa
    # prima dell'esempio" e NotebookLM la eseguiva alla lettera: nella trascrizione
    # del run del 09/10 la parola "Pausa." compare 6 volte in 19m47s, come
    # segmento isolato (maiuscola e punto: era un'indicazione di scena scritta,
    # letta ad alta voce dalla TTS) e quasi sempre esattamente su un cambio di
    # sezione, cioe' dove la si era chiesta. Rumore per l'ascoltatore, e rumore
    # per il motore: un blocco fatto di una parola di riempimento non ha
    # contenuto semantico, quindi diluisce il segnale che separa le sezioni. ---
    ("niente parole di riempimento e niente indicazioni di scena", "no pause"),
    # --- due pagine senza sezione (run del 04/10) hanno rotto il video peggio
    # di qualunque altro difetto: la 13 si e' presa quattro minuti di materiale
    # delle altre. La numerazione non vietava la copertina (vedi commit 02926a7). ---
    ("niente copertina senza numero", "niente copertina"),
    # --- tono ---
    ("tono e stile del dibattito", "blocco tono"),
]

# Scelte deliberate del 04/10 e del 15/10, non dimenticanze. Queste regole sono
# state tolte dal prompt e NON sono piu' richieste; il fatto resta scritto
# perche' `test_nessuna_regola_rimossa_torna_indietro` verifica che non
# rientrino di soppiatto in una riscrittura del prompt.
REGOLE_RIMOSSE = [
    ("titoli non generici", 'mai "Introduzione", "Conclusioni", "Argomento 2"'),
    ("parole distinctive", "evitando termini generici ripetuti sulle altre pagine"),
    ("una idea per slide", "una sola idea centrale"),
    # --- 15/10: prompt accorciato di proposito. Il deck e' guidato solo dalle
    # regole qualitative rimaste ("una slide per argomento", "ogni pagina deve
    # poter diventare una sezione breve del podcast", gli annunci ordinati):
    # nessun limite numerico di righe o parole e nessun divieto di pagina-lista.
    # Se una pagina torna gonfia, la causa e' questa scelta, non un errore di
    # caricamento del prompt: il numero di righe non e' piu' sorvegliato da
    # nessun test.
    ("no pagine-lista", "mai pagine che sono solo un elenco di voci, nemmeno una"),
    ("deck leggero quantificato", "al massimo 8 righe e 80 parole"),
    # La richiesta di arricchimento visivo e' uscita dal prompt: la grafica non
    # aiuta la sincronizzazione (le ancore sono le frasi pronunciate, non le
    # figure) e la scelta visiva resta a NotebookLM.
    ("grafica ricca", "arricchisci ogni pagina con un elemento visivo pertinente"),
]


class TestRegolePrompt(unittest.TestCase):
    """Ogni regola che protegge la sincronizzazione deve restare scritta."""

    def test_ogni_regola_e_presente(self) -> None:
        mancanti = [
            f"{PROMPT.name}: {nome}"
            for atteso, nome in REGOLE
            if _normale(atteso) not in _testo_da_incollare(PROMPT)
        ]
        self.assertEqual(mancanti, [], "regole sparite dal prompt")

    def test_il_prompt_esiste(self) -> None:
        self.assertTrue(PROMPT.exists(), f"manca {PROMPT.name}")

    def test_nessuna_regola_rimossa_torna_indietro(self) -> None:
        """Le regole tolte non devono riapparire per sbaglio.

        Sono state levate di proposito. Se una riappare, il prompt e' stato
        modificato da qualcuno che non sapeva della scelta (vedi
        REGOLE_RIMOSSE).
        """
        testo = _testo_da_incollare(PROMPT)
        tornate = [
            nome for nome, frammento in REGOLE_RIMOSSE if _normale(frammento) in testo
        ]
        self.assertEqual(tornate, [], "regole tolte di proposito, riapparse")


class TestBloccoTono(unittest.TestCase):
    """Il blocco TONE e' quello che l'utente incolla per il tono del dibattito."""

    def test_il_prompt_ha_un_blocco_tono(self) -> None:
        self.assertIn("TONO E STILE DEL DIBATTITO", PROMPT.read_text(encoding="utf-8"))


class TestBlocchiIncollabili(unittest.TestCase):
    """Cosa finisce nel campo di testo di NotebookLM deve essere pulito.

    Non sono pedanterie: 4 spazi iniziali diventano un blocco di codice se
    l'input viene interpretato come markdown, e i simboli non-ASCII si degradano
    in console Windows (e in copia-incolla). Le frecce dei titoli sono fuori dai
    blocchi e non sono un problema: non raggiungono mai NotebookLM.
    """

    def test_nessuna_riga_con_quattro_spazi(self) -> None:
        offending = [
            f"{PROMPT.name}: {riga!r}"
            for blocco in _blocchi(PROMPT)
            for riga in blocco.splitlines()
            if riga.startswith("    ")
        ]
        self.assertEqual(offending, [], "righe che diventerebbero blocco di codice")

    def test_nessuna_grafica_markdown_dentro_ai_blocchi(self) -> None:
        # #, **, ═, ▸ non hanno senso in un campo di testo: arrivano al
        # modello come spazzatura.
        grafica = re.compile(r"^#|\*\*|═|▸", re.M)
        offending = [
            f"{PROMPT.name}: {riga.strip()[:40]!r}"
            for blocco in _blocchi(PROMPT)
            for riga in blocco.splitlines()
            if grafica.search(riga)
        ]
        self.assertEqual(offending, [], "grafica markdown dentro un blocco")

    def test_nessun_simbolo_fuori_dal_testo_italiano(self) -> None:
        # Solo lettere accentate e punteggiatura ASCII: tutto il resto e' un
        # rischio di codifica, e non aggiunge istruzione.
        vietati = {
            c
            for blocco in _blocchi(PROMPT)
            for c in blocco
            if ord(c) > 127 and not c.isalpha()
        }
        self.assertEqual(
            sorted(vietati), [], "simboli non-italiani dentro i blocchi da incollare"
        )

    def test_nessun_passo_oltre_una_pagina_di_istruzioni(self) -> None:
        # NotebookLM non ha un limite dichiarato, ma un prompt oltre qualche
        # migliaio di caratteri compete con i sorgenti. Si sta larghi.
        #
        # Il tetto e' per BLOCCO, non per file. I due blocchi del file non
        # vengono mai incollati insieme: Fase 1 va nel generatore di slide,
        # Fase 2 nelle istruzioni dell'audio, e sono due sessioni NotebookLM
        # diverse. Il confronto con i sorgenti avviene mentre ne e' vivo uno solo,
        # quindi sommarli penalizzava un prompt di 700 caratteri per colpa di un
        # altro blocco dello stesso file. Il margine che ne risultava era cosi'
        # stretto da costringere a scegliere fra allentare il tetto e tagliare
        # regole utili.
        for i, blocco in enumerate(_blocchi(PROMPT), start=1):
            caratteri = len(blocco.strip())
            self.assertLess(
                caratteri,
                4000,
                f"{PROMPT.name}: blocco {i} da {caratteri} caratteri",
            )


class TestRiferimentiReadme(unittest.TestCase):
    """Ogni link del README a un file locale deve esistere.

    Cancellando un prompt il README si e' riempito di link rotti in sette punti:
    un link rotto in un documento che si dovrebbe leggere per capire il
    funzionamento e' peggio di un link assente.
    """

    def test_i_link_locali_esistono(self) -> None:
        testo = README.read_text(encoding="utf-8")
        # [etichetta](<percorso>) e [etichetta](percorso)
        candidati = re.findall(r"\]\((?:<([^>]+)>|([^)\s]+))\)", testo)
        rotti = []
        for con_cani, senza_cani in candidati:
            target = con_cani or senza_cani
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            # Via#sezione e' un link valido: il file e' 'via', il resto e'
            # l'ancora dentro il file. Controllare l'intera stringa come
            # percorso segnalerebbe come rotto un link che funziona, e
            # renderebbe vietato linkare una sezione.
            percorso = target.split("#", 1)[0]
            if not (RADICE / percorso).exists():
                rotti.append(target)
        self.assertEqual(rotti, [], "link del README a file inesistenti")

    def test_il_readme_cita_il_prompt_che_esiste(self) -> None:
        testo = README.read_text(encoding="utf-8")
        self.assertIn(PROMPT.name, testo, f"{PROMPT.name} non e' citato nel README")


if __name__ == "__main__":
    unittest.main()
