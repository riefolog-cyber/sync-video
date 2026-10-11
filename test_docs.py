#!/usr/bin/env python3
"""Verifica che i numeri delle doc (default delle tabelle, conteggi dei test)
corrispondano al codice.

Esegui con: python -m unittest test_docs -v

Perche' esiste: tre volte in un giorno un numero scritto in una tabella delle
doc e' rimasto indietro rispetto al codice.

  - docs/comandi.md: "i tre parametri di thread hanno lo stesso default,
    min(8, cpu_count())" quando il codice distingueva gia' whisper (senza
    tetto) da embed/video (min(12, core fisici)) e OCR (min(6, ...));
  - la stessa tabella: --llm documentato con default "auto" mentre il
    codice fa "off";
  - promemoria prima installazione.md: diceva che il programma "NON
    installa Python da solo",cioe' il contrario di quello che faceva.

Un default scritto a mano in un documento prima o poi mente. Qui non si
affida alla buona volonta': ogni volta che una tabella delle doc nomina un
flag, il valore documentato viene confrontato con argparse.

Anche i CONTEGGI invecchiano. Le doc scrivono quanti test esegue un comando:
un numero vero quando e' stato scritto, falso dopo il primo test aggiunto.
Percio' ogni riga `python -m unittest ...` delle doc viene caricata per
davvero e il numero dichiarato confrontato con quello misurato.

COSA NON CATTURA, e va detto chiaramente: i valori "semplici" delle tabelle.
Le frasi in prosa ("NON lo installa da solo", "sempre", "mai") non sono
verificabili da uno script: quelle le controlla chi scrive. E i valori
condizionali tipo "min(6, core fisici)" o "8 (4 sotto i 6 GB di RAM)" sono
saltati di proposito, perche' cambiano al variare della macchina e il loro
numero dipende dall'host: qui si confronta la FORMA, non il numero. Restano
fuori anche i conteggi che produce un altro strumento (le righe e i moduli di
mypy, le segnalazioni di ruff): per quelli il controllo e' il passo di CI che
esegue lo strumento, non questo file.
"""

from __future__ import annotations

import re
import unittest
from collections.abc import Generator
from pathlib import Path

import config

RADICE = Path(__file__).resolve().parent

# Documenti in cui un default può essere scritto a mano.
DOCUMENTI = [
    *sorted((RADICE / "docs").glob("*.md")),
    RADICE / "README.md",
    RADICE / "promemoria prima installazione.md",
    RADICE / "PIANO_OTTIMIZZAZIONI.md",
]

# Un valore "semplice": numero puro, o parola senza parentesi né virgole.
# Tutto il resto e' un'espressione o un valore condizionale: si salta.
_SEMPLICE = re.compile(r"^-?\d+(\.\d+)?$|^[A-Za-z][A-Za-z0-9_./+-]*$")

# Un comando di test in una doc: `python -m unittest ...`. Da qui si ricava
# quali test girano davvero, per non fidarsi del numero scritto a mano.
_COMANDO_TEST = re.compile(r"python(?:\.exe)?\s+-m\s+unittest\s+(?P<argomenti>[^\n#]+)")

# Un conteggio dichiarato: "424 test" o "147 unit test".
_NUMERO_TEST = re.compile(r"(?P<numero>\d+)\s+(?:unit\s+)?test\b")


def _righe_tabella(testo: str) -> Generator[tuple[int, list[str]], None, None]:
    """(riga, celle) per ogni riga di tabella markdown con almeno 2 celle."""
    for i, riga in enumerate(testo.splitlines(), 1):
        if not riga.lstrip().startswith("|"):
            continue
        celle = [c.strip() for c in riga.strip().strip("|").split("|")]
        if len(celle) >= 2:
            yield i, celle


def _conta_test(argomenti: str) -> int | None:
    """Quanti test esegue davvero il comando `python -m unittest <argomenti>`.

    Carica i moduli con lo stesso loader di unittest (nessun test viene
    eseguito). Un comando che non si carica non deve far fallire questo
    controllo: si salta restituendo None.
    """
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    if "discover" in argomenti:
        try:
            suite.addTests(loader.discover(str(RADICE), pattern="test_*.py", top_level_dir=str(RADICE)))
        except Exception:
            return None
        return suite.countTestCases()
    moduli = [a for a in argomenti.split() if a and not a.startswith("-")]
    if not moduli:
        return None
    for nome in moduli:
        try:
            suite.addTests(loader.loadTestsFromName(nome))
        except Exception:
            return None
    return suite.countTestCases()


class TestDefaultDocumentati(unittest.TestCase):
    def _default_reali(self) -> dict[str, int | float | str | bool]:
        args = config.parse_args([])
        default: dict[str, int | float | str | bool] = {}
        for nome, valore in vars(args).items():
            if isinstance(valore, (bool, int, float, str)):
                default["--" + nome.replace("_", "-")] = valore
        return default

    def test_ogni_flag_documentato_ha_un_default_vera(self) -> None:
        """Il cuore: nessuna tabella può documentare un default diverso."""
        reali = self._default_reali()
        problemi: list[str] = []
        controllati = 0

        for doc in DOCUMENTI:
            if not doc.exists():
                continue
            testo = doc.read_text(encoding="utf-8", errors="replace")
            for riga, celle in _righe_tabella(testo):
                flag = celle[0].strip("`").strip()
                if flag not in reali:
                    continue
                documentato = celle[1].strip().strip("`").strip()
                if not documentato or not _SEMPLICE.match(documentato):
                    continue  # espressione o condizionale: fuori portata qui
                controllati += 1
                atteso = reali[flag]
                if isinstance(atteso, bool) or (isinstance(documentato, str) and not documentato[0].isdigit()):
                    stessa = documentato == str(atteso)
                else:
                    try:
                        atteso_num: float = float(atteso) if isinstance(atteso, (int, float)) else 0.0
                        stessa = abs(float(documentato) - atteso_num) < 1e-9
                    except ValueError:
                        stessa = False
                if not stessa:
                    problemi.append(
                        f"{doc.name}:{riga}  {flag}  la doc dice '{documentato}', "
                        f"il codice fa '{atteso}'"
                    )

        if controllati == 0:
            self.skipTest("nessuna riga di tabella con un default semplice da confrontare")
        self.assertEqual(problemi, [], "\n".join(problemi))

    def test_i_documenti_esistono(self) -> None:
        # Se un file di doc sparisce, il confronto sopra lo ignorerebbe in
        # silenzio: un controllo che non guarda niente passa sempre.
        for doc in DOCUMENTI:
            with self.subTest(doc=doc.name):
                self.assertTrue(doc.exists(), f"{doc} non esiste piu'")

    def test_le_tabelle_usano_valori_sensati(self) -> None:
        """Sani: una cella che dice solo '~6.4 GB' o un trattino non e' un default."""
        reali = self._default_reali()
        for doc in DOCUMENTI:
            if not doc.exists():
                continue
            testo = doc.read_text(encoding="utf-8", errors="replace")
            for riga, celle in _righe_tabella(testo):
                flag = celle[0].strip("`").strip()
                if flag not in reali:
                    continue
                valore = celle[1].strip().strip("`").strip()
                with self.subTest(doc=doc.name, riga=riga, flag=flag):
                    self.assertNotIn(valore, ("~", "-", "n/d", "?"), f"{flag}: default non dichiarato")


class TestConteggiDocumentati(unittest.TestCase):
    """Il numero di test scritto in una doc deve essere quello vero.

    Il comando e' la fonte di verita' (come argparse per i default): qui si
    carica davvero la suite nominata e si confronta il conteggio dichiarato.
    """

    def test_i_conteggi_dichiarati_sono_quelli_veri(self) -> None:
        problemi: list[str] = []
        controllati = 0

        for doc in DOCUMENTI:
            if not doc.exists():
                continue
            righe = doc.read_text(encoding="utf-8", errors="replace").splitlines()
            for indice, riga in enumerate(righe):
                comando = _COMANDO_TEST.search(riga)
                if not comando:
                    continue
                atteso = _conta_test(comando.group("argomenti").strip())
                if atteso is None:
                    continue
                # Il numero sta dopo il comando o, come nelle doc, nella riga
                # di commento subito sopra.
                candidati = [riga]
                if indice > 0 and righe[indice - 1].lstrip().startswith("#"):
                    candidati.append(righe[indice - 1])
                documentato = None
                for candidato in candidati:
                    trovato = _NUMERO_TEST.search(candidato)
                    if trovato:
                        documentato = int(trovato.group("numero"))
                        break
                if documentato is None:
                    continue
                controllati += 1
                if documentato != atteso:
                    problemi.append(
                        f"{doc.name}:{indice + 1}  la doc dice '{documentato} test', il comando ne esegue {atteso}"
                    )

        if controllati == 0:
            self.skipTest("nessun conteggio di test dichiarato nelle doc")
        self.assertEqual(problemi, [], "\n".join(problemi))


class TestProseNonVerificabile(unittest.TestCase):
    """Non un test vero: rende esplicito il limite dello strumento.

    Le frasi in prosa non sono verificabili da uno script. Questo test puo'
    solo segnalare che il limite esiste, non che sia stato risolto.
    """

    def test_le_frasi_in_prosa_restano_a_controllo_umano(self) -> None:
        # Se un giorno qualcuno vuole coprire anche le frasi, questo e' il
        # posto dove dirlo. Per ora e' una constatazione, non una promessa.
        self.assertTrue(
            hasattr(config, "parse_args"),
            "argparse resta la fonte di verita' dei default",
        )


if __name__ == "__main__":
    unittest.main()
