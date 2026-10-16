#!/usr/bin/env python3
"""Un Python troppo recente deve fermare il programma PRIMA di pip.

Il buco che copre: senza guardia, su un PC con solo Python 3.14 il
programma scaricava 2,7 GB di modelli e poi si fermava DENTRO pip, perche'
pillow<11 non ha wheel oltre cp313 e pip provava a compilarlo. Il
messaggio che arrivava era della compilazione, non della causa.

Qui non serve avere un 3.14: si finta `sys.version_info` e si verifica che
la guardia parli e che esca con codice 1.

Esegui con: python -m unittest test_python_troppo_recente -v
"""

from __future__ import annotations

import sys
import unittest
from collections import namedtuple
from unittest import mock

import config
import hardware
import verifica_pc

# Il vero sys.version_info e' una TUPLE con attributi: si confronta con >= (3, 13)
# e si legge come .major/.minor. Una tupla semplice farebbe solo la meta' del
# lavoro (il confronto funziona, l'attributo no) e il test fallirebbe per il
# motivo sbagliato, sembrando un difetto del codice invece che del finto.
_VersionInfo = namedtuple("_VersionInfo", "major minor micro releaselevel serial")
TROPPO_VECCHIA = _VersionInfo(3, 9, 18, "final", 0)
TROPPA_VECCHIA = _VersionInfo(3, 14, 0, "final", 0)
ANCORA_BENE = _VersionInfo(3, 13, 1, "final", 0)


class TestFinestraVersioni(unittest.TestCase):
    """Una sola finestra, dichiarata una volta sola."""

    def test_la_finestra_e_unica(self) -> None:
        """config e verifica_pc devono leggere la stessa, nonCopy ciascuna.

        Se i due moduli avessero limiti diversi, verifica_pc direbbe "ok" e il
        bootstrap si fermerebbe un secondo dopo: il peggiore dei due mondi.
        """
        self.assertEqual(config.PYTHON_MAX, hardware.PYTHON_MAX)
        self.assertEqual(verifica_pc.PYTHON_MAX, hardware.PYTHON_MAX)

    def test_la_finestra_e_quel_della_ci(self) -> None:
        """Il massimo dichiarato deve essere davvero quello provato.

        Alzare PYTHON_MAX senza aggiungere la versione alla matrice della CI
        significa dichiarare supportata una versione mai vista: e' il modo
        piu' economico per ripetere il buco di audioop.
        """
        from pathlib import Path

        import yaml

        y = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
        testo = y.read_text(encoding="utf-8")
        try:
            dati = yaml.safe_load(testo)
        except ImportError:
            self.skipTest("PyYAML non presente: non riesco a leggere la matrice")
        matrici = [
            j["strategy"]["matrix"]["python-version"]
            for j in dati["jobs"].values()
            if isinstance(j.get("strategy"), dict)
            and isinstance(j["strategy"].get("matrix"), dict)
            and "python-version" in j["strategy"]["matrix"]
        ]
        self.assertTrue(matrici, "nessun job CI con matrice Python: il controllo e' cieco")
        for m in matrici:
            with self.subTest(matrice=m):
                maggiore = hardware.PYTHON_MAX[1]
                atteso = f"3.{maggiore}"
                self.assertIn(atteso, m, f"la CI non prova Python {atteso}, che il codice dichiara supportato")


class TestRifiutaTroppoRecente(unittest.TestCase):
    def test_su_314_esce_con_errore(self) -> None:
        with (
            mock.patch.object(sys, "version_info", TROPPA_VECCHIA),
            self.assertRaises(SystemExit) as catturato,
        ):
            config._rifiuta_python_troppo_recente()
        self.assertEqual(catturato.exception.code, 1)

    def test_su_313_non_esce(self) -> None:
        """Il limite deve stare sul 3.13, non piu' in alto: 3.13 e' verde in CI."""
        with mock.patch.object(sys, "version_info", ANCORA_BENE):
            config._rifiuta_python_troppo_recente()  # non deve sollevare

    def test_su_39_non_e_colpa_del_tetto_superiore(self) -> None:
        """Il 3.9 e' troppo VECCHIO: lo gestisce il controllo sotto, non questo."""
        with mock.patch.object(sys, "version_info", TROPPO_VECCHIA):
            config._rifiuta_python_troppo_recente()  # non deve sollevare

    def test_il_messaggio_dice_la_causa(self) -> None:
        """Un "Python troppo recente" da solo non basta: serve il PERCHE'.

        Chi lo legge deve capire che non e' un suo errore e che cosa fare.
        """
        with (
            mock.patch.object(config.log, "error") as errore, mock.patch.object(sys, "version_info", TROPPA_VECCHIA),
            self.assertRaises(SystemExit),
        ):
            config._rifiuta_python_troppo_recente()
        testo = " ".join(str(c.args[0]) for c in errore.call_args_list if c.args)
        for atteso in ("pillow", "3.13", "python.org"):
            with self.subTest(atteso=atteso):
                self.assertIn(atteso.lower(), testo.lower())


class TestVerificaPcLoDicePrima(unittest.TestCase):
    """Il diagnostico deve accorgersene, non aspettare il bootstrap."""

    def _esito(self, versione: _VersionInfo) -> verifica_pc.Esito:
        r = verifica_pc.Relazione()
        with mock.patch.object(sys, "version_info", versione):
            verifica_pc.prova_python(r)
        return next(e for e in r.esiti if e.nome == "Python")

    def test_314_e_ko(self) -> None:
        self.assertEqual(self._esito(TROPPA_VECCHIA).stato, verifica_pc.KO)

    def test_39_e_ko(self) -> None:
        self.assertEqual(self._esito(TROPPO_VECCHIA).stato, verifica_pc.KO)

    def test_313_e_ok(self) -> None:
        self.assertEqual(self._esito(ANCORA_BENE).stato, verifica_pc.OK)

    def test_il_rimedio_dice_da_dove(self) -> None:
        rimedio = self._esito(TROPPA_VECCHIA).rimedio or ""
        self.assertIn("python.org", rimedio)
        self.assertIn("3.12", rimedio)


if __name__ == "__main__":
    unittest.main()
