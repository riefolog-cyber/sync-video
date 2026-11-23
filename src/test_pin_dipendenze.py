#!/usr/bin/env python3
"""I pin delle dipendenze non possono scindersi fra requirements.txt e config.

Esegui con: python -m unittest test_pin_dipendenze -v

`requirements.txt` e' quello che installa chi usa il progetto, ma il bootstrap
di `config._REQUIRED_PACKAGES` e' quello che installa e verifica il programma
all'avvio. Se i due elenchi divergono, l'utente puo' avere una versione diversa
da quella che il programma dice di richiedere, senza che nessun import fallisca:
il danno e' silenzioso (nel caso di fastembed, embedding diversi e sync sbagliata).

Qui si confrontano i requisiti dichiarati, non le versioni installate: e' il
contratto che deve restare identico. Per fastembed si legge il valore EFFETTIVO
di `config._REQUIRED_PACKAGES` (override A/B inclusi), perche' `updates.py`,
dopo un A/B accettato, aggiorna requirements.txt e il file di override senza
toccare il letterale in config.py: confrontare i due sorgenti testuali darebbe
un falso allarme mentre il bootstrap installa gia' la versione nuova.
"""

from __future__ import annotations

import re
import sys
import unittest

import config
from hardware import project_root

RADICE = project_root()
REQUIREMENTS = RADICE / "requirements.txt"
CONFIG = RADICE / "src" / "config.py"


def _riga_requirements(pacchetto: str) -> str:
    """La riga di requirements.txt che dichiara il pacchetto dato."""
    testo = REQUIREMENTS.read_text(encoding="utf-8")
    trovato = re.search(
        rf"^\s*{re.escape(pacchetto)}(?=\s|$|[=<>!~;\[])[^#\n]*",
        testo,
        re.M | re.I,
    )
    if not trovato:
        raise AssertionError(f"{REQUIREMENTS.name}: manca {pacchetto}")
    return trovato.group(0).strip()


def _voce_config(chiave: str) -> str:
    """Il requisito di `config._REQUIRED_PACKAGES` per la chiave di import.

    Sono ammesse entrambe le forme usate nel file: la voce nel dizionario
    letterale (`"PIL": "..."`) e l'aggiunta condizionale (`_REQUIRED_PACKAGES
    ["audioop"] = "..."`), che e' come audioop-lts entra sopra Python 3.13.
    """
    testo = CONFIG.read_text(encoding="utf-8")
    chiave = re.escape(chiave)
    trovato = re.search(
        rf'(?:"{chiave}"\s*:\s*"|\["{chiave}"\]\s*=\s*")([^"]+)"',
        testo,
    )
    if not trovato:
        raise AssertionError(f"config.py: manca la voce {chiave!r} in _REQUIRED_PACKAGES")
    return trovato.group(1).strip()


def _voce_config_effettiva(chiave: str) -> str:
    """Il requisito di `config._REQUIRED_PACKAGES` dopo gli override A/B.

    `_voce_config()` legge il letterale nel sorgente ed e' ancora usata dagli
    altri test; qui serve invece il valore che il bootstrap legge davvero,
    quindi dopo che `_pin_overrides()` ha applicato `.cache/pin_overrides.json`.
    """
    try:
        return str(config._REQUIRED_PACKAGES[chiave])
    except KeyError:
        raise AssertionError(f"config._REQUIRED_PACKAGES: manca la voce {chiave!r}") from None


def _versione_pin(requisito: str, origine: str) -> str:
    """La versione dopo `==` in un requisito, con messaggio che indica il file."""
    trovato = re.search(r"==\s*([^\s;]+)", requisito)
    if not trovato:
        raise AssertionError(f"{origine}: {requisito!r} non e' pinnato con ==")
    return trovato.group(1)


def _specifica(requisito: str) -> str:
    """Requisito senza marcatore PEP 508, normalizzato per il confronto."""
    return requisito.split(";", 1)[0].strip().lower()


class TestPinDipendenze(unittest.TestCase):
    def test_pin_di_fastembed_coincidono(self) -> None:
        """Dopo un A/B accettato i due pin devono cambiare insieme.

        Si confronta il valore EFFETTIVO della configurazione runtime (override
        inclusi), non il testo di config.py: e' quello che il bootstrap usa.
        Il controllo resta sulla coincidenza requirements/configurazione, cosi'
        un aggiornamento a meta' (solo requirements o solo override) viene
        preso.
        """
        riga_requirements = _riga_requirements("fastembed")
        requisito_runtime = _voce_config_effettiva("fastembed")
        atteso = _versione_pin(riga_requirements, REQUIREMENTS.name)
        trovata = _versione_pin(requisito_runtime, "config._REQUIRED_PACKAGES")
        self.assertEqual(
            trovata,
            atteso,
            "il pin effettivo di config deve coincidere con requirements.txt "
            f"(requirements: {riga_requirements}, config: {requisito_runtime})",
        )

    def test_vincolo_di_pillow_coincide(self) -> None:
        """Il tetto <11 serve a far risolvere pip con fastembed 0.5.1."""
        self.assertEqual(
            _specifica(_voce_config("PIL")),
            _specifica(_riga_requirements("pillow")),
            "pillow: config.py e requirements.txt non dichiarano lo stesso requisito",
        )

    def test_vincolo_di_audioop_coincide(self) -> None:
        """Sotto Python 3.13 audioop e' nella stdlib: config non ha la voce."""
        if sys.version_info < (3, 13):
            self.skipTest("audioop-lts serve solo da Python 3.13")
        self.assertEqual(
            _specifica(_voce_config("audioop")),
            _specifica(_riga_requirements("audioop-lts")),
            "audioop-lts: config.py e requirements.txt non dichiarano lo stesso requisito",
        )


if __name__ == "__main__":
    unittest.main()
