#!/usr/bin/env python3
"""Il blocco delle run concorrenti.

Due run sulla stessa cartella si pestano a vicenda (slide, cache, frame di
verifica) e il risultato e' un video a meta' costruito con i pezzi dell'altro,
senza errori in console. Il blocco e' un lock DEL SISTEMA OPERATIVO: se il
processo muore, il blocco muore con lui (niente file da interpretare a mano).

Esegui con: python -m unittest test_blocco_run -v
"""

from __future__ import annotations

import gc
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import blocco_run
from blocco_run import NON_DISPONIBILE, OCCUPATO, PRESO, BloccoRun


def _esito_figlio(base: Path) -> str:
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


class TestBloccoRun(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _figlio(self, base: Path) -> str:
        """Cosa dice un altro processo quando prova a prendere il blocco."""
        return _esito_figlio(base)

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
            try:
                self.assertTrue(primo.acquired())
                # Prima questo passava senza aprire nulla: il blocco era unico
                # per processo, non per cartella. Ora i due blocchi sono
                # davvero indipendenti, e vanno rilasciati entrambi.
                self.assertTrue(secondo.acquired(), "cartelle diverse: nessun conflitto")
            finally:
                # Su Windows un file aperto non si cancella: i blocchi vanno
                # rilasciati PRIMA che la cartella temporanea sparisca.
                secondo.release()
                primo.release()

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
                if figlio.stdout is not None:
                    figlio.stdout.close()

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

    def test_se_il_file_di_blocco_non_si_crea_la_run_prosegue(self) -> None:
        # Cartella in sola lettura, ACL, disco pieno: il file di blocco non si
        # crea. Non e' una ragione per fermarsi (ne' per aspettare un quarto
        # d'ora): la run parte, con meno garanzie.
        with tempfile.TemporaryDirectory() as d:
            ostacolo = Path(d) / "non-una-cartella"
            ostacolo.write_text("x", encoding="utf-8")
            blocco = BloccoRun(ostacolo / "sub")
            inizio = time.time()
            esito = blocco.tenta(attesa_secondi=30.0)
            durata = time.time() - inizio
            self.assertEqual(esito, NON_DISPONIBILE)
            self.assertTrue(blocco.acquired(attesa_secondi=30.0))
            self.assertLess(durata, 5.0, "non deve aspettare: il blocco non e' di nessuno")

    def test_una_run_che_prova_a_bloccare_dice_occupato(self) -> None:
        primo = BloccoRun(self.base)
        self.addCleanup(primo.release)
        self.assertTrue(primo.acquired())
        # Un processo vero: qui dentro lo stesso processo il blocco e' "preso"
        # per progetto, quindi l'esito OCCUPATO si verifica solo da fuori.
        self.assertIn("BLOCCATO", self._figlio(self.base))
        self.assertEqual(primo.tenta(), PRESO)

    def test_il_file_di_blocco_non_cresce_a_ogni_run(self) -> None:
        # Il file serve solo a dare un byte da bloccare a Windows: nessuno ci
        # scrive niente, quindi non deve crescere a ogni run.
        dimensioni: list[int] = []
        for _ in range(3):
            blocco = BloccoRun(self.base)
            self.assertTrue(blocco.acquired())
            blocco.release()
            dimensioni.append((self.base / "run.lock").stat().st_size)
        self.assertEqual(len(set(dimensioni)), 1)

    def test_il_blocco_si_libera_quando_la_run_finisce(self) -> None:
        # main() non ha un finally attorno alla run: il lock deve morire con
        # l'oggetto, altrimenti un rilancio da un'altra console aspetta 15
        # minuti per un lavoro gia' finito.
        def _una_run() -> None:
            blocco = BloccoRun(self.base)
            self.assertTrue(blocco.acquired())
            # Nessun release(): esce dallo scope e basta.

        _una_run()
        self.assertIn("LIBERO", self._figlio(self.base))


class TestDueIstanzeStessoProcesso(unittest.TestCase):
    """Due `BloccoRun` sulla stessa cartella: vince il primo, non si pestano."""

    def test_la_seconda_istanza_non_libera_il_blocco_della_prima(self) -> None:
        import blocco_run
        from blocco_run import PRESO, BloccoRun

        with tempfile.TemporaryDirectory() as tmp:
            primo = BloccoRun(Path(tmp))
            self.assertEqual(primo.tenta(), PRESO)
            secondo = BloccoRun(Path(tmp))
            self.assertEqual(secondo.tenta(), PRESO, "stesso processo: passa")
            secondo.release()
            # Il blocco vero e' ancora del primo: la chiave deve restare.
            self.assertIn(primo._chiave, blocco_run._proprietari)
            terzo = BloccoRun(Path(tmp))
            self.assertEqual(terzo.tenta(), PRESO)
            primo.release()
            # Il terzo e' ancora dentro il conteggio: va rilasciato anche lui,
            # altrimenti il file resta aperto (ed e' la cosa GIUSTA).
            terzo.release()

    def test_se_il_primo_proprietario_muore_il_blocco_resta(self) -> None:
        # `__del__` del primo non deve sbloccare mentre il secondo crede di
        # tenere ancora il lock: il contatore tiene vivo il file condiviso.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            primo = BloccoRun(base)
            self.assertEqual(primo.tenta(), PRESO)
            secondo = BloccoRun(base)
            self.assertEqual(secondo.tenta(), PRESO)
            del primo
            gc.collect()

            self.assertIn((base / "run.lock").resolve(), blocco_run._proprietari)
            self.assertIn("BLOCCATO", _esito_figlio(base))
            secondo.release()
            self.assertIn("LIBERO", _esito_figlio(base))

    def test_attesa_breve_non_dorme_due_secondi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocco = BloccoRun(Path(tmp))
            # Due tentativi: uno subito (occupato), uno dopo 0.6s (ancora oltre
            # la scadenza di 0.5s). Il sonno va limitato al tempo che manca.
            istanti = iter([0.0, 0.0, 0.0, 0.6])

            def _ora() -> float:
                return next(istanti, 10.0)

            con_tempo = mock.patch.object(blocco_run.time, "time", side_effect=_ora)
            con_sonno = mock.patch.object(blocco_run.time, "sleep")
            with (
                con_tempo,
                con_sonno as dorme,
                mock.patch.object(blocco, "_prova", return_value=OCCUPATO),
                # il logging crea un LogRecord con time.time(): senza questo
                # consumo l'orario falso non sarebbe quello della loop.
                mock.patch.object(blocco_run.log, "info"),
            ):
                esito = blocco.tenta(attesa_secondi=0.5)
            self.assertEqual(esito, OCCUPATO)
            dorme.assert_called_once()
            self.assertAlmostEqual(dorme.call_args.args[0], 0.5)


class TestContesto(unittest.TestCase):
    def test_with_rilascia_anche_se_la_run_solleva(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            blocco = BloccoRun(base)
            with self.assertRaises(RuntimeError), blocco:
                self.assertEqual(blocco.tenta(), PRESO)  # non si conta due volte
                raise RuntimeError("run interrotta")
            # Uscito dal `with`, un nuovo blocco sulla stessa cartella passa.
            altro = BloccoRun(base)
            self.assertEqual(altro.tenta(), PRESO)
            altro.release()


if __name__ == "__main__":
    unittest.main()
