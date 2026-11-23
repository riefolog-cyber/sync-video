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


def _testo_da_incollare(path: Path) -> str:
    """Tutto il file, normalizzato.

    Il prompt e' testo continuo: non ci sono piu' blocchi delimitati da
    cancellette. Le due fasi sono intestazioni dentro lo stesso file, quindi
    cio' che va controllato e' l'intero contenuto, non un estratto.
    """
    return _normale(path.read_text(encoding="utf-8"))


def _link_locali_rotti(path: Path, base: Path) -> list[str]:
    """Link locali rotti in un file markdown, risolti relativamente a `base`.

    Il README usa percorsi relativi alla radice, i file in docs/ usano
    percorsi relativi alla propria cartella (es. `../README.md`), quindi la
    base di risoluzione va passata dal chiamante.
    """
    testo = path.read_text(encoding="utf-8")
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
        if not (base / percorso).exists():
            rotti.append(target)
    return rotti


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
    ("un numero solo nella frase che apre la sezione", "numeri isolati"),
    ('mai "slide 1" in apertura', "no slide 1"),
    ("mai il numero totale di pagine", "no totale pagine"),
    ("sezioni di lunghezza simile", "sezioni equilibrate"),
    # --- corrispondenza pagina/sezione: dopo la riscrittura del 2026-11 resta
    # la forma qualitativa, senza i dettagli quantificati che sono in
    # REGOLE_RIMOSSE. ---
    ("una sezione per pagina, tutte le pagine dalla prima all'ultima", "tutte le pagine"),
    ("ogni pagina deve poter diventare una sezione breve del podcast", "deck leggero"),
    ("non elencare mai i numeri già annunciati", "no recap numerato"),
    # --- numero pagina unico: il tema di Slide Deck ristampa il numero in
    # calce oltre a quello nel contenuto (doppio numero su ogni slide).
    # Non rompe la sync (il controllo legge la posizione) ma sporca il video.
    # La posizione "in basso a sinistra" è un desiderio, non un vincolo che
    # il tema rispetti sempre: l'unica cosa che conta è "una sola volta". ---
    ("il numero compare una sola volta per pagina", "numero unico"),
    # --- due pagine senza sezione (run del 04/10) hanno rotto il video peggio
    # di qualunque altro difetto: la 13 si e' presa quattro minuti di materiale
    # delle altre. La numerazione non vietava la copertina (vedi commit 02926a7). ---
    ("niente copertina senza numero", "niente copertina"),
    # --- tono ---
    ("tono e stile del dibattito", "blocco tono"),
]

# Scelte deliberate del 04/10, del 15/10 e della semplificazione del 2026-11,
# non dimenticanze. Queste regole sono state tolte dal prompt e NON sono piu'
# richieste; il fatto resta scritto perche'
# `test_nessuna_regola_rimossa_torna_indietro` verifica che non rientrino di
# soppiatto in una riscrittura del prompt.
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
    # --- 2026-11: riscrittura a mano del prompt, convalidata dall'utente nel
    # suo test reale. Le dieci frasi sotto sono sparite di proposito insieme ai
    # blocchi ```: non devono rientrare di soppiatto in una futura "pulizia".
    # Il prompt piu' corto funziona, quindi la regola e' che resti cosi'. ---
    ("perche' dei richiami", "il richiamo è fuori ordine e fa perdere la transizione vera"),
    ("esempio di richiamo", "guarda sempre slide 3"),
    ("sempre una sezione", "dagli comunque una sezione di almeno 60 secondi"),
    ("sezioni brevi", "restando breve: un solo esempio, niente divagazioni"),
    ("priorita' all'annuncio", "accorcia l'esempio e non la sezione"),
    ("durate quantificate", "ogni sezione dura 60-180 secondi"),
    ("ancora stesso conduttore", "detta sempre dallo stesso conduttore"),
    ("no salti di pagina", "non saltarla mai, un salto sposta tutti i numeri dopo"),
    ("fonte unica", "nelle fonti tieni solo la presentazione"),
    ("no pause", "niente parole di riempimento e niente indicazioni di scena"),
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
    """Cio' che finisce nel campo di testo di NotebookLM deve essere pulito.

    Non sono pedanterie: 4 spazi iniziali diventano un blocco di codice se
    l'input viene interpretato come markdown, e i simboli non-ASCII si degradano
    in console Windows (e in copia-incolla). Ora che il prompt e' un unico testo
    continuo, controlliamo l'intero file: non ci sono piu' blocchi "protetti"
    dentro le cancellette. Le due frecce (— e →) e le lettere accentate sono
    ammesse: fanno parte del testo che l'utente ha convalidato.
    """

    def setUp(self) -> None:
        self.testo = PROMPT.read_text(encoding="utf-8")

    def test_nessuna_riga_con_quattro_spazi(self) -> None:
        offending = [
            f"{PROMPT.name}: {riga!r}"
            for riga in self.testo.splitlines()
            if riga.startswith("    ")
        ]
        self.assertEqual(offending, [], "righe che diventerebbero blocco di codice")

    def test_nessuna_grafica_markdown(self) -> None:
        # #, **, ═, ▸ non hanno senso in un campo di testo: arrivano al
        # modello come spazzatura.
        grafica = re.compile(r"^#|\*\*|═|▸", re.M)
        offending = [
            f"{PROMPT.name}: {riga.strip()[:40]!r}"
            for riga in self.testo.splitlines()
            if grafica.search(riga)
        ]
        self.assertEqual(offending, [], "grafica markdown nel prompt")

    def test_nessun_simbolo_fuori_dal_testo_italiano(self) -> None:
        # Lettere accentate, piu' le sole frecce usate nelle intestazioni.
        # Tutto il resto e' un rischio di codifica e non aggiunge istruzione.
        ammessi = {"—", "→"}
        vietati = {
            c
            for c in self.testo
            if ord(c) > 127 and not c.isalpha() and c not in ammessi
        }
        self.assertEqual(
            sorted(vietati), [], "simboli non-italiani nel testo da incollare"
        )

    def test_nessun_passo_oltre_una_pagina_di_istruzioni(self) -> None:
        # NotebookLM non ha un limite dichiarato, ma un prompt oltre qualche
        # migliaio di caratteri compete con i sorgenti. Si sta larghi: il file
        # riscritto e' poco sopra i 2.700 caratteri.
        caratteri = len(self.testo)
        self.assertLess(
            caratteri,
            4000,
            f"{PROMPT.name}: {caratteri} caratteri in tutto il file",
        )


class TestRiferimentiReadme(unittest.TestCase):
    """Ogni link del README a un file locale deve esistere.

    Cancellando un prompt il README si e' riempito di link rotti in sette punti:
    un link rotto in un documento che si dovrebbe leggere per capire il
    funzionamento e' peggio di un link assente.
    """

    def test_i_link_locali_esistono(self) -> None:
        self.assertEqual(
            _link_locali_rotti(README, RADICE), [], "link del README a file inesistenti"
        )

    def test_i_link_locali_nei_docs_esistono(self) -> None:
        # I docs non sono come il README: i loro percorsi partono dalla
        # cartella del documento, non dalla radice del progetto.
        docs = sorted((RADICE / "docs").glob("*.md"))
        self.assertTrue(docs, "nessun file .md trovato in docs/")
        for doc in docs:
            with self.subTest(doc=doc.name):
                self.assertEqual(
                    _link_locali_rotti(doc, doc.parent),
                    [],
                    f"link rotti in docs/{doc.name}",
                )

    def test_il_readme_cita_il_prompt_che_esiste(self) -> None:
        testo = README.read_text(encoding="utf-8")
        self.assertIn(PROMPT.name, testo, f"{PROMPT.name} non e' citato nel README")


class TestStrutturaDelFile(unittest.TestCase):
    """La FORMA del file, non le frasi: e' cio' che si rompe riformattando.

    Ora le due fasi sono intestazioni di testo continuo, senza cancellette.
    Se una intestazione sparisce o un'istruzione "Scarica ..." viene a mancare,
    il prompt non e' piu' quello convalidato dall'utente. Gli spazi finali
    invece sono voluti: servono da interruzioni di riga markdown, quindi non
    vengono vietati.
    """

    def setUp(self) -> None:
        self.testo = PROMPT.read_text(encoding="utf-8")

    def test_le_due_fasi_ci_sono(self) -> None:
        for intestazione in ("Fase 1", "Fase 2"):
            with self.subTest(intestazione=intestazione):
                self.assertIn(intestazione, self.testo, f"manca {intestazione}")

    def test_le_istruzioni_di_download_ci_sono(self) -> None:
        for istruzione in ("Scarica la presentazione", "Scarica l'audio"):
            with self.subTest(istruzione=istruzione):
                self.assertIn(istruzione, self.testo, f"manca {istruzione!r}")

    def test_nessun_nbsp(self) -> None:
        # L'NBSP non e' uno spazio normale: copiato in NotebookLM non si
        # comporta come separatore e non si vede a occhio nudo.
        self.assertNotIn("\u00a0", self.testo, "NBSP (U+00A0) nel prompt")


if __name__ == "__main__":
    unittest.main()
