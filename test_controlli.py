#!/usr/bin/env python3
"""I controlli eseguiti da `controlli.py` devono coprire quelli della CI.

Perche' esiste: `controlli.py` promette "quello che gira in CI, piu' gli extra
locali". Se qualcuno aggiunge un passo alla CI e non al comando (o viceversa),
i due divergono in silenzio e la promessa diventa un'opinione. Qui le due
liste si confrontano: la CI si legge dal workflow, i passi dal codice.

COSA NON CATTURA, e va detto: si confrontano i MODULI eseguiti (`python -m X`),
non le loro opzioni riga per riga. Cambiare `mypy .` in `mypy --strict .` nella
CI senza aggiornare `controlli.py` non farebbe scattare nulla.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import controlli

RADICE = Path(__file__).resolve().parent
WORKFLOW = RADICE / ".github" / "workflows" / "ci.yml"

# Un passo eseguito dal workflow: `run: python -m <modulo> ...`.
_RUN = re.compile(r"run:\s*python\s+-m\s+(?P<modulo>[A-Za-z_][\w.]*)")


def _moduli_ci() -> set[str]:
    testo = WORKFLOW.read_text(encoding="utf-8", errors="replace")
    return {m.group("modulo") for m in _RUN.finditer(testo)}


def _moduli_controlli() -> set[str]:
    moduli: set[str] = set()
    for controllo in controlli._passi():
        argomenti = controllo.argomenti
        for i, arg in enumerate(argomenti):
            if arg == "-m" and i + 1 < len(argomenti):
                moduli.add(argomenti[i + 1])
    return moduli


class TestControlliCopronoLaCi(unittest.TestCase):
    def test_il_workflow_della_ci_esiste(self) -> None:
        self.assertTrue(WORKFLOW.exists(), f"{WORKFLOW} non c'e' piu'")

    def test_ogni_modulo_della_ci_ha_un_passo_nei_controlli(self) -> None:
        moduli_ci = _moduli_ci()
        self.assertTrue(moduli_ci, "nessun comando trovato nella CI: il test non sta guardando niente")
        mancanti = moduli_ci - _moduli_controlli()
        self.assertEqual(mancanti, set(), f"la CI esegue {sorted(mancanti)} ma controlli.py non lo fa")

    def test_il_type_check_copre_le_tre_piattaforme(self) -> None:
        piattaforme: set[str] = set()
        for controllo in controlli._passi():
            argomenti = controllo.argomenti
            for i, arg in enumerate(argomenti):
                if arg == "--platform" and i + 1 < len(argomenti):
                    piattaforme.add(argomenti[i + 1])
        self.assertEqual(piattaforme, {"win32", "linux", "darwin"})


if __name__ == "__main__":
    unittest.main()
