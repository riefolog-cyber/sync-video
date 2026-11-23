#!/usr/bin/env python3
"""Verifica che i numeri delle doc (default delle tabelle, conteggi dei test)
corrispondano al codice.

Esegui con: python -m unittest test_docs -v

Perche' esiste: tre volte in un giorno un numero scritto in una tabella delle
doc e' rimasto indietro rispetto al codice.

  - docs/comandi.md: "i tre parametri di thread hanno lo stesso default,
    min(8, cpu_count())" quando il codice distingueva gia' whisper (senza
    tetto) da embed/video (min(12, core fisici)) e OCR (min(6, ...));
  - la stessa tabella: un flag documentato con un default diverso da quello
    del codice;
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

import contextlib
import re
import unittest
from collections.abc import Generator
from pathlib import Path
from unittest import mock

import config
from hardware import project_root

# I .md che questo test verifica stanno in radice, i moduli in src/:
# `project_root()` trova la radice, `__file__.parent` darebbe src/ e i
# documenti risulterebbero inesistenti (il test passerebbe saltandoli).
RADICE = project_root()

# Documenti in cui un default può essere scritto a mano.
DOCUMENTI = [
    *sorted((RADICE / "docs").glob("*.md")),
    RADICE / "README.md",
    RADICE / "promemoria prima installazione.md",
]

# Un valore "semplice": numero puro, o parola senza parentesi né virgole.
# Tutto il resto e' un'espressione o un valore condizionale: si salta.
_SEMPLICE = re.compile(r"^-?\d+(\.\d+)?$|^[A-Za-z][A-Za-z0-9_./+-]*$")

# Un comando di test in una doc: `python -m unittest ...`. Da qui si ricava
# quali test girano davvero, per non fidarsi del numero scritto a mano.
_COMANDO_TEST = re.compile(r"python(?:\.exe)?\s+-m\s+unittest\s+(?P<argomenti>[^\n#]+)")

# Un conteggio dichiarato: "424 test" o "147 unit test".
_NUMERO_TEST = re.compile(r"(?P<numero>\d+)\s+(?:unit\s+)?test\b")

# Un flag di riga di comando citato in una tabella. Le env var non cominciano
# con `--`, quindi non vengono estratte: non sono flag di argparse.
_FLAG_TABELLA = re.compile(r"--[A-Za-z][A-Za-z0-9-]*")


def _flags_argparse() -> set[str]:
    """Tutte le opzioni accettate dal parser, sinonimi `--no-*` compresi.

    `vars(parse_args([]))` non basta: `--no-auto-beam` ha dest `auto_beam` e
    `--no-snap-pauses` ha dest `snap_pauses`, quindi dai dest non si risalgono
    ai flag scritti. Qui si leggono le `option_strings` delle azioni del parser
    vero, che e' la fonte di verita' sia per i default sia per l'esistenza.
    """
    return {
        opzione
        for azione in config.build_parser()._actions
        for opzione in azione.option_strings
    }


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
        # Usa la cartella che il COMANDO dichiara (`-s src`), non la radice:
        # altrimenti questo controllo conterebbe i test che si vedono da un
        # posto diverso da quello documentato, e la doc passerebbe senza
        # essere vera.
        partenza = RADICE
        m_s = re.search(r"-s\s+(\S+)", argomenti)
        if m_s:
            partenza = Path(m_s.group(1).strip('"').strip("'"))
            if not partenza.is_absolute():
                partenza = RADICE / partenza
        try:
            suite.addTests(loader.discover(str(partenza), pattern="test_*.py", top_level_dir=str(partenza)))
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
        # Il .env della macchina e' gitignored e puo' cambiare i default presi
        # da os.environ: le tabelle documentano i default DEL CODICE, non le
        # preferenze locali di chi esegue i test. Confrontiamo quindi i
        # valori letterali dichiarati in config.py.
        default_da_env = {
            "DEFAULT_TRANSCRIBER": "auto",
            "DEFAULT_OPENVINO_DEVICE": "GPU",
            "DEFAULT_WHISPER_MODEL": "small",
            "DEFAULT_WHISPER_DEVICE": "cpu",
            "DEFAULT_WHISPER_COMPUTE_TYPE": "int8",
            "DEFAULT_OCR_LANG": "ita",
        }
        with contextlib.ExitStack() as stack:
            for nome, valore in default_da_env.items():
                stack.enter_context(mock.patch.object(config, nome, valore))
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
    """Qui si verifica la parte controllabile: le opzioni chiave nelle doc.

    Le frasi in prosa restano a controllo umano, ma l'elenco delle opzioni
    essenziali no: se `--pdf` o `--dry-run` spariscono dalle doc, chi legge
    non sa piu' usare il programma e nessun confronto di default se ne accorge
    (una riga di tabella mancante viene semplicemente saltata).
    """

    # Opzioni senza le quali il comando d'uso non e' completo. La fonte di
    # verita' resta argparse: prima di pretendere che siano documentate si
    # controlla che esistano davvero.
    FLAGS_CHIAVE = ("--pdf", "--audio", "--output", "--dry-run", "--no-cache", "--debug")

    def test_le_opzioni_chiave_sono_documentate(self) -> None:
        reali = {"--" + nome.replace("_", "-") for nome in vars(config.parse_args([]))}
        sparite = [flag for flag in self.FLAGS_CHIAVE if flag not in reali]
        self.assertEqual(sparite, [], "opzioni chiave sparite da argparse")

        testi = [
            doc.read_text(encoding="utf-8", errors="replace")
            for doc in DOCUMENTI
            if doc.exists()
        ]
        non_documentate = [
            flag for flag in self.FLAGS_CHIAVE if not any(flag in testo for testo in testi)
        ]
        self.assertEqual(non_documentate, [], "opzioni chiave non citate in nessuna doc")


class TestFlagDocumentatiEsistono(unittest.TestCase):
    """Il controllo sui default saltava i flag inesistenti: qui si fa il contrario.

    `TestDefaultDocumentati` scarta una riga di tabella se il flag non e' in
    argparse, quindi una opzione inventata (o rimossa e rimasta nelle doc)
    passava inosservata. Questo test inverte il controllo: ogni `--flag` che
    compare in una tabella deve esistere davvero nel parser.

    Le eccezioni sono esplicite e numerate: env var (non iniziano con `--`),
    flag dei `.bat`, opzioni di script terzi citate nelle doc e un flag rimosso
    di proposito, ancora documentato come storico.
    """

    NON_ARGPARSE = frozenset(
        {
            "--help",  # aggiunto da argparse
            "--semantic-min-sim",  # rimosso: la tabella lo cita come storico
            "--check-updates",  # genera_video.bat
            "--no-pause",  # genera_video.bat
            "--ricrea",  # crea_venv.bat
            "--id",  # winget install (troubleshooting)
            "--upgrade",  # pip install (troubleshooting)
            "--fix",  # ruff check --fix (struttura)
            "--quality",  # archify (diagrammi)
            "--json",  # archify (diagrammi)
            "--platform",  # mypy (PIANO_OTTIMIZZAZIONI)
            "--aggiorna",  # prova_materiale_reale.py (verifica)
            "--scrivi",  # riga_baseline.py (verifica)
            "--confronta",  # riga_baseline.py (verifica)
        }
    )

    def test_ogni_flag_in_tabella_esiste_nel_parser(self) -> None:
        reali = _flags_argparse()
        problemi: list[str] = []
        controllati = 0

        for doc in DOCUMENTI:
            if not doc.exists():
                continue
            testo = doc.read_text(encoding="utf-8", errors="replace")
            for riga, celle in _righe_tabella(testo):
                for cella in celle:
                    for flag in _FLAG_TABELLA.findall(cella):
                        controllati += 1
                        if flag in reali or flag in self.NON_ARGPARSE:
                            continue
                        problemi.append(f"{doc.name}:{riga}  flag documentato ma inesistente: {flag}")

        if controllati == 0:
            self.skipTest("nessun flag di riga di comando trovato nelle tabelle")
        self.assertEqual(problemi, [], "\n".join(problemi))


class TestEtichetteRiepilogoPromemoria(unittest.TestCase):
    """Le etichette che il promemoria dice di cercare devono essere stampate.

    Una checklist con etichette inventate e' peggio di nessuna checklist: chi
    legge cerca una riga che il programma non scrive e conclude che la run e'
    andata storto. Qui si leggono le voci della checklist del paragrafo 5 e si
    cerca ciascuna etichetta nel sorgente del riepilogo.
    """

    def test_etichette_citate_sono_quelle_del_riepilogo(self) -> None:
        promemoria = RADICE / "promemoria prima installazione.md"
        testo = promemoria.read_text(encoding="utf-8")
        pezzi = testo.split("## 5. Come capire se è andato bene", 1)
        if len(pezzi) < 2:
            self.fail("il promemoria non ha piu' il paragrafo 5")
        corpo = pezzi[1].split("\n## ", 1)[0]

        # Solo le voci della checklist: nel resto del paragrafo i backtick
        # citano messaggi di errore di altre fasi, non etichette del riepilogo.
        etichette = [
            etichetta
            for riga in corpo.splitlines()
            if riga.lstrip().startswith("- ")
            for etichetta in re.findall(r"`([^`]+)`", riga)
        ]
        self.assertTrue(etichette, "nessuna etichetta citata nella checklist del promemoria")

        sorgente_main = (RADICE / "src" / "main.py").read_text(encoding="utf-8")
        inizio = sorgente_main.index("def _log_plain_summary(")
        fine = sorgente_main.index("def _frame_check_video(", inizio)
        sorgente_riepilogo = sorgente_main[inizio:fine]

        mancanti = [etichetta for etichetta in etichette if etichetta not in sorgente_riepilogo]
        self.assertEqual(
            mancanti,
            [],
            "etichette citate nel promemoria ma non scritte dal riepilogo: " + ", ".join(mancanti),
        )


if __name__ == "__main__":
    unittest.main()
