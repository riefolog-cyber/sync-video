"""Test per `aggiornamenti.py` (interruttori e propagazione delle opzioni).

Il modulo non ha logica sua: decide solo *se* e *come* chiamare
`run_update_check`. E' pero' il posto dove si annidano interruttori che il
README promette, quindi va verificato che non siano decorativi: un flag
accettato e poi ignorato e' peggio di un flag assente, perche' l'utente crede
di aver scelto qualcosa.

Non tocca PyPI ne' installs: il bootstrap e il controllo sono mockati.
"""

import unittest
from unittest import mock

import aggiornamenti


class TestInterruttori(unittest.TestCase):
    def _esegui(self, argv):
        """Esegue main() con argv finto e restituisce le chiamate registrate."""
        with mock.patch.object(aggiornamenti, "bootstrap") as boot, mock.patch.object(
            aggiornamenti, "run_update_check"
        ) as check, mock.patch("sys.argv", ["aggiornamenti.py", *argv]):
            aggiornamenti.main()
        return boot, check

    def test_di_default_chiede_il_consenso(self):
        # Il comportamento storico: senza opzioni deve chiedere S/N, altrimenti
        # l'aggiornamento automatico dei pacchetti diventerebbe silenzioso.
        boot, check = self._esegui([])
        boot.assert_called_once()
        check.assert_called_once_with(ask_to_update=True)

    def test_no_update_notifica_senza_installare(self):
        _, check = self._esegui(["--no-update"])
        check.assert_called_once_with(ask_to_update=False)

    def test_no_update_check_non_controlla_pyPI(self):
        # `--no-update-check` non e' un parametro di run_update_check: e'
        # l'assenza della chiamata. Se passasse ask_to_update qualunque it'd be
        # un interruttore che non fa niente.
        _, check = self._esegui(["--no-update-check"])
        check.assert_not_called()

    def test_i_due_interruttori_insieme(self):
        _, check = self._esegui(["--no-update", "--no-update-check"])
        check.assert_not_called()

    def test_opzione_sconosciuta_esce_con_errore(self):
        # argparse deve rifiutare: uno switch typo' che passa silenziosamente
        # lascerebbe l'utente convinto di aver escluso qualcosa.
        with mock.patch.object(aggiornamenti, "bootstrap") as boot, mock.patch.object(
            aggiornamenti, "run_update_check"
        ) as check, mock.patch(
            "sys.argv", ["aggiornamenti.py", "--no-aggiornamenti"]
        ), self.assertRaises(SystemExit) as ctx:
            aggiornamenti.main()
        self.assertEqual(ctx.exception.code, 2)
        boot.assert_not_called()
        check.assert_not_called()

    def test_help_non_rivela_nulla(self):
        with mock.patch.object(
            aggiornamenti, "bootstrap"
        ) as boot, mock.patch("sys.argv", ["aggiornamenti.py", "--help"]), self.assertRaises(
            SystemExit
        ) as ctx:
            aggiornamenti.main()
        self.assertEqual(ctx.exception.code, 0)
        boot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
