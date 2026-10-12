#!/usr/bin/env python3
"""Su Python 3.13+ `audioop` non esiste piu' e pydub non puo' importare.

Il buco: `_python.bat` puo' scegliere un 3.13 (o il launcher di default, che
su un PC del 2026 e' un 3.13/3.14), e a quel punto la trascrizione moriva
con "ModuleNotFoundError: No module named 'audioop'" DOPO che i 2,7 GB di
modelli erano gia' scaricati, con un messaggio che non nominava la causa.

Qui non serve avere un 3.13 installato: si ricarica config.py in una COPIA
del modulo con `sys.version_info` finto, quindi il config vero del processo
non viene toccato e gli altri test non se ne accorgono.

Esegui con: python -m unittest test_audioop_313 -v
"""

from __future__ import annotations

import builtins
import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

import verifica_pc

SRC = Path(__file__).resolve().parent
CONFIG_PY = SRC / "config.py"

# Come definire un sys.version_info finto: sys.version_info e' una tupla
# nominata, e il confronto `>= (3, 13)` funziona su una tupla semplice.
InfoVersione = tuple[int, int, int, str, int]
VERSIONE_313: InfoVersione = (3, 13, 0, "final", 0)
VERSIONE_312: InfoVersione = (3, 12, 10, "final", 0)


def _config_con_versione(versione: InfoVersione) -> ModuleType:
    """Ricarica config.py in un modulo NUOVO con quella versione di Python."""
    spec = importlib.util.spec_from_file_location("config_simulato", CONFIG_PY)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "version_info", versione):
        spec.loader.exec_module(modulo)
    return modulo


class _BloccoAudioop:
    """Rende `import audioop` impossibile, come su un 3.13 senza il pacchetto."""

    def __init__(self) -> None:
        self._vero_import = builtins.__import__

    def __enter__(self) -> _BloccoAudioop:
        def import_(nome: str, *a: object, **k: object) -> object:
            if nome == "audioop" or nome.startswith("audioop."):
                raise ModuleNotFoundError("No module named 'audioop'")
            return self._vero_import(nome, *a, **k)  # type: ignore[arg-type]

        builtins.__import__ = import_  # type: ignore[assignment]
        return self

    def __exit__(self, *_: object) -> None:
        builtins.__import__ = self._vero_import  # type: ignore[assignment]


class TestAudioopSuPython313(unittest.TestCase):
    """Il pacchetto che rimette `audioop` dev'essere dichiarato."""

    def test_su_313_e_dichiarato(self) -> None:
        cfg = _config_con_versione(VERSIONE_313)
        self.assertIn("audioop", cfg._REQUIRED_PACKAGES)
        self.assertEqual(cfg._REQUIRED_PACKAGES["audioop"], "audioop-lts")

    def test_la_chiave_e_il_nome_di_import(self) -> None:
        """`verifica_pc` fa `__import__(chiave)`: la chiave deve essere `audioop`.

        audioop-lts installa il modulo `audioop`, NON `audioop_lts` (verificato
        dentro la wheel: c'e' audioop/__init__.py). Se la chiave fosse
        `audioop_lts`, verifica_pc direbbe "mancanti" su un PC sanissimo.
        """
        cfg = _config_con_versione(VERSIONE_313)
        chiave = next(k for k, v in cfg._REQUIRED_PACKAGES.items() if v == "audioop-lts")
        self.assertEqual(chiave, "audioop")

    def test_su_312_non_e_dichiarato(self) -> None:
        """Sotto la 3.13 `audioop` e' nella standard library.

        Dichiararlo comunque romperebbe il bootstrap: audioop-lts dichiara
        requires-python >=3.13, pip lo rifiuterebbe, e ogni PC con 3.10-3.12
        si fermerebbe all'avvio. Il silenzio qui e' il comportamento giusto.
        """
        cfg = _config_con_versione(VERSIONE_312)
        self.assertNotIn("audioop", cfg._REQUIRED_PACKAGES)
        self.assertNotIn("audioop", cfg._active_required_packages())

    def test_requirements_dichiara_lo_stesso_marcatore(self) -> None:
        """requirements.txt e il bootstrap devono concordare.

        Se il marker manca solo in requirements.txt, `pip install -r` su un
        3.13 installa tutto il resto e lascia fuori audioop-lts: il fallimento
        si vede solo dopo il download dei modelli.
        """
        testo = (SRC.parent / "requirements.txt").read_text(encoding="utf-8")
        righe = [r for r in testo.splitlines() if r.strip().startswith("audioop-lts")]
        self.assertEqual(len(righe), 1, f"audioop-lts deve comparire una volta sola: {righe}")
        self.assertIn('python_version >= "3.13"', righe[0])

    def test_su_312_audioop_e_importabile_da_stdlib(self) -> None:
        """Controesempio: su 3.12 non serve audioop-lts perche' lo standard lo dà."""
        if sys.version_info >= (3, 13):
            self.skipTest("questa Python e' 3.13+: audioop non e' piu' nella standard library")
        import audioop  # noqa: F401 - l'import e' il test

    def test_verifica_pc_vede_la_chiarenza(self) -> None:
        """Il diagnostico deve dire che manca audioop, non dichiararlo a posto.

        E' il punto di tutto: senza la voce in _REQUIRED_PACKAGES, un PC 3.13
        col pacchetto non installato passerebbe `verifica_pc` con "[ ok ]
        Pacchetti" e si romperebbe piu' avanti, senza lasciare traccia.
        """
        rel = verifica_pc.Relazione()
        # prova_pacchetti fa `from config import _active_required_packages`
        # DENTRO la funzione, quindi va patchato config, non verifica_pc.
        import config

        with _BloccoAudioop(), mock.patch.object(
            config, "_active_required_packages", _dict_con_audioop
        ):
            verifica_pc.prova_pacchetti(rel)
        testi = [f"{e.nome}: {e.dettaglio}" for e in rel.esiti]
        self.assertTrue(
            any("audioop-lts" in t for t in testi),
            f"verifica_pc non ha visto che manca audioop: {testi}",
        )
        self.assertTrue(
            any(e.stato == verifica_pc.KO for e in rel.esiti),
            f"audioop mancante non deve risultare OK: {testi}",
        )


def _dict_con_audioop() -> dict[str, str]:
    """`_active_required_packages` come sarebbe su un 3.13."""
    import config

    d = dict(config._REQUIRED_PACKAGES)
    d["audioop"] = "audioop-lts"
    return d


if __name__ == "__main__":
    unittest.main()
