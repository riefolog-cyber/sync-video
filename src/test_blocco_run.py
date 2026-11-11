#!/usr/bin/env python3
"""Il blocco delle run concorrenti.

Due run sulla stessa cartella si pestano a vicenda (slide, cache, frame di
verifica) e il risultato e' un video a meta' costruito con i pezzi dell'altro,
senza errori in console. Il blocco e' un lock DEL SISTEMA OPERATIVO: se il
processo muore, il blocco muore con lui (niente file da interpretare a mano).

Esegui con: python -m unittest test_blocco_run -v
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from blocco_run import BloccoRun


class TestBloccoRun(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _figlio(self, base: Path) -> str:
        """Cosa dice un altro processo quando prova a prendere il blocco."""
        src = str(Path(__file__).resolve().parent)
        codice = (
            f"import sys; sys.path.insert(0, {src!r})\n"
            "from pathlib import Path\n"
            "from blocco_run import BloccoRun\n"
            f"print('LIBERO' if BloccoRun(Path({str(base)!r})).acquired() else 'BLOCCATO')\n"
        )
        esito = subprocess.run(
            [sys.executable, "-c", codice], capture_output=True, text=True, timeout=120
        )
        return esito.stdout

    def test_una_run_in_un_altro_processo_non_puo_partire(self) -> None:
        # Il conflitto che conta e' fra PROCESSI diversi: due run sulla stessa
        # cartella. Si verifica davvero, con un processo figlio.
        primo = BloccoRun(self.base)
        self.addCleanup(primo.release)
        self.assertTrue(primo.acquired(), "il primo run deve prendere il blocco")

        self.assertIn("BLOCCATO", self._figlio(self.base))

        primo.release()
        self.assertIn("LIBERO", self._figlio(self.base))

    def test_nello_stesso_processo_il_blocco_non_blocca(self) -> None:
        # Due run NELLO STESSO processo non si pestano (le slide sono gia'
        # state pulite, la cache e' la stessa): il secondo avvio passa.
        primo = BloccoRun(self.base)
        self.addCleanup(primo.release)
        self.assertTrue(primo.acquired())
        secondo = BloccoRun(self.base)
        self.assertTrue(secondo.acquired(), "stesso processo: nessun conflitto")

    def test_il_blocco_su_cartelle_diverse_indipende(self) -> None:
        with tempfile.TemporaryDirectory() as altra:
            primo = BloccoRun(self.base)
            secondo = BloccoRun(Path(altra))
            self.addCleanup(primo.release)
            self.addCleanup(secondo.release)
            self.assertTrue(primo.acquired())
            self.assertTrue(secondo.acquired(), "cartelle diverse: nessun conflitto")

    def test_la_run_che_arriva_dopo_aspetta_che_l_altra_finisca(self) -> None:
        # Il caso reale: un rilancio mentre la prima run e' ancora al lavoro.
        # La seconda ASPETTA (il blocco e' del sistema operativo: quando la
        # prima finisce o muore, il blocco si libera da solo).
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            src = str(Path(__file__).resolve().parent)
            codice = (
                f"import sys, time; sys.path.insert(0, {src!r})\n"
                "from pathlib import Path\n"
                "from blocco_run import BloccoRun\n"
                f"b = BloccoRun(Path({str(base)!r}))\n"
                "print('PRESO' if b.acquired() else 'BLOCCATO', flush=True)\n"
                "time.sleep(3)\n"
            )
            figlio = subprocess.Popen(
                [sys.executable, "-c", codice], stdout=subprocess.PIPE, text=True
            )
            uscita = figlio.stdout
            assert uscita is not None
            blocco = BloccoRun(base)
            try:
                self.assertEqual(uscita.readline().strip(), "PRESO")
                self.assertTrue(
                    blocco.acquired(attesa_secondi=30.0),
                    "la seconda run deve aspettare che la prima finisca",
                )
            finally:
                # Il blocco va rilasciato PRIMA che la cartella temporanea
                # venga cancellata: su Windows il file aperto non si cancella.
                blocco.release()
                figlio.wait(timeout=30)

    def test_il_file_di_blocco_puo_restare_sul_disco(self) -> None:
        # Il file resta, ma conta il blocco del SO: dopo la run il file non
        # blocca nessuno (nessuna "pulizia manuale" da fare).
        primo = BloccoRun(self.base)
        self.addCleanup(primo.release)
        self.assertTrue(primo.acquired())
        primo.release()
        self.assertTrue(self.base.joinpath("run.lock").exists())

        secondo = BloccoRun(self.base)
        self.addCleanup(secondo.release)
        self.assertTrue(secondo.acquired(), "un file di blocco vecchio non blocca la run")
        secondo.release()


if __name__ == "__main__":
    unittest.main()
