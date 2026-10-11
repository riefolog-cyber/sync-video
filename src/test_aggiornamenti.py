"""Test per `aggiornamenti.py` (interruttori e propagazione delle opzioni).

Il modulo non ha logica sua: decide solo *se* e *come* chiamare
`run_update_check`. E' pero' il posto dove si annidano interruttori che il
README promette, quindi va verificato che non siano decorativi: un flag
accettato e poi ignorato e' peggio di un flag assente, perche' l'utente crede
di aver scelto qualcosa.

Non tocca PyPI ne' installs: il bootstrap e il controllo sono mockati.
"""

import contextlib
import io
import unittest
from unittest import mock

import aggiornamenti
import config


class TestInterruttori(unittest.TestCase):
    def _esegui(self, argv: list[str]) -> tuple[mock.MagicMock, mock.MagicMock]:
        """Esegue main() con argv finto e restituisce le chiamate registrate."""
        with mock.patch.object(aggiornamenti, "bootstrap") as boot, mock.patch.object(
            aggiornamenti, "run_update_check"
        ) as check, mock.patch("sys.argv", ["aggiornamenti.py", *argv]):
            aggiornamenti.main()
        return boot, check

    def test_di_default_chiede_il_consenso(self) -> None:
        # Il comportamento storico: senza opzioni deve chiedere S/N, altrimenti
        # l'aggiornamento automatico dei pacchetti diventerebbe silenzioso.
        boot, check = self._esegui([])
        boot.assert_called_once()
        check.assert_called_once_with(ask_to_update=True)

    def test_no_update_notifica_senza_installare(self) -> None:
        _, check = self._esegui(["--no-update"])
        check.assert_called_once_with(ask_to_update=False)

    def test_no_update_check_non_controlla_pyPI(self) -> None:
        # `--no-update-check` non e' un parametro di run_update_check: e'
        # l'assenza della chiamata. Se passasse ask_to_update qualunque it'd be
        # un interruttore che non fa niente.
        _, check = self._esegui(["--no-update-check"])
        check.assert_not_called()

    def test_i_due_interruttori_insieme(self) -> None:
        _, check = self._esegui(["--no-update", "--no-update-check"])
        check.assert_not_called()

    def test_opzione_sconosciuta_esce_con_errore(self) -> None:
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

    def _esegui_diagnostica(self, argv: list[str]) -> tuple[mock.MagicMock, mock.MagicMock, mock.MagicMock]:
        """Come `_esegui`, ma registra anche la diagnostica dei pacchetti fermi."""
        with mock.patch.object(aggiornamenti, "bootstrap") as boot, mock.patch.object(
            aggiornamenti, "run_update_check"
        ) as check, mock.patch.object(aggiornamenti, "run_frozen_report") as diag, mock.patch(
            "sys.argv", ["aggiornamenti.py", *argv]
        ):
            aggiornamenti.main()
        return boot, check, diag

    def test_frozen_report_non_installa_e_non_controlla(self) -> None:
        # Un referto non tocca pip: niente bootstrap (e' la parte che installa) e niente
        # controllo aggiornamenti (il referto E' il controllo, spiegato).
        boot, check, diag = self._esegui_diagnostica(["--frozen-report"])
        boot.assert_not_called()
        check.assert_not_called()
        diag.assert_called_once_with()

    def test_frozen_report_vince_su_no_update_check(self) -> None:
        # --no-update-check dice "non controllare", --frozen-report dice "spiega": chi lo
        # chiede deve riceverlo, altrimenti e' un interruttore decorativo.
        _, check, diag = self._esegui_diagnostica(["--no-update-check", "--frozen-report"])
        check.assert_not_called()
        diag.assert_called_once_with()

    def test_lo_stesso_interruttore_anche_in_main(self) -> None:
        # Gli interruttori dei due script devono coincidere (regola del progetto): se il
        # referto esistesse solo qui, si imparerebbe una cosa e se ne userebbe un'altra.
        self.assertTrue(config.parse_args(["--frozen-report"]).frozen_report)

    def test_help_elenca_il_referto(self) -> None:
        # E' l'unico modo in cui l'interruttore si scopre: se sparisse da --help,
        # resterebbe un flag che esiste e non si trova.
        with mock.patch("sys.argv", ["aggiornamenti.py", "--help"]), contextlib.redirect_stdout(
            io.StringIO()
        ) as out, self.assertRaises(SystemExit) as ctx:
            aggiornamenti.main()
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("--frozen-report", out.getvalue())

    def test_help_non_rivela_nulla(self) -> None:
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
