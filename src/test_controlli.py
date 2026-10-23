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

import controlli
from hardware import project_root

# I file che questo test legge (.github/workflows/ci.yml) stanno in radice,
# non in src/ dove vivono i moduli: `project_root()` risale fino a trovarla,
# mentre `Path(__file__).parent` punterebbe a src/ e il file non esisterebbe.
RADICE = project_root()
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

    def test_il_job_windows_verifica_ffmpeg_e_ha_un_ripiego(self) -> None:
        """Non basta l'exit code di Chocolatey per dire "ffmpeg c'e'".

        Misurato il 19/10/2026: il feed ha risposto 504, `choco install
        ffmpeg` e' uscito 0 senza installare niente, e il KO e' arrivato dal
        test del PC sano ("ffmpeg non trovato nel PATH") come se il codice
        fosse rotto. Il passo deve quindi controllare il binario e avere un
        ripiego che non dipende dalla rete (imageio-ffmpeg, gia' installato
        come dipendenza di moviepy).
        """
        import yaml

        dati = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        passi = [str(p.get("run", "")) for p in dati["jobs"]["windows-rapido"]["steps"] if "run" in p]
        testi = [t for t in passi if "ffmpeg" in t]
        self.assertTrue(testi, "nessun passo del job Windows nomina ffmpeg")
        passo = "\n".join(testi)
        self.assertIn("Get-Command ffmpeg", passo, "l'installazione va verificata sul binario, non sull'exit code")
        self.assertIn("imageio_ffmpeg", passo, "senza ripiego un 504 di Chocolatey fa fallire i test")

    def test_i_job_ubuntu_installano_ffmpeg_e_tesseract(self) -> None:
        """ubuntu-latest non porta piu' gli strumenti: li deve mettere la CI.

        Il 19/10/2026 ubuntu-latest e' passata a Ubuntu 26.04, che non ha
        ffmpeg ne' Tesseract preinstallati. Senza un passo dedicato li
        installa il bootstrap dentro la suite, dove ogni comando ha un
        timeout di 600s: su un mirror lento scade e test_disco e
        test_verifica_pc falliscono come se il codice fosse rotto (run
        39921954759, nightly 3.11). Qui si controlla che entrambi i job che
        eseguono i test su Linux li installino e ne verifichino il binario.
        """
        import yaml

        dati = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for job in ("lint-typecheck-test", "nightly-full"):
            with self.subTest(job=job):
                passi = [str(p.get("run", "")) for p in dati["jobs"][job]["steps"] if "run" in p]
                testi = [t for t in passi if "ffmpeg" in t and "tesseract" in t]
                self.assertTrue(testi, f"il job {job} non installa ffmpeg e Tesseract")
                passo = "\n".join(testi)
                self.assertIn("command -v ffmpeg", passo, "l'installazione va verificata sul binario")
                self.assertIn("command -v tesseract", passo, "l'installazione va verificata sul binario")

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
