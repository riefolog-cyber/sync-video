"""Mette la radice del progetto nel sys.path degli script in `scripts/`.

Un eseguibile in `scripts/` parte con `scripts/` come sys.path[0], non la
radice: i moduli core (`config`, `hardware`, `semantic_sync`...) non sarebbero
importabili. Questo file risolve il problema e va importato **per primo**,
prima di qualsiasi modulo del progetto:

    from _bootstrap import RADICE   # noqa: I001  (deve stare per primo)

    import config

Unica eccezione alla regola "una sola definizione della radice": qui la
ricerca dei segni e' riscritta invece di riusare `hardware.project_root()`,
perche' `hardware.py` sta nella radice e importarlo richiederebbe gia' di
averla trovata. Sono quattro righe, e sono l'unico posto in cui la logica
appare due volte in tutto il progetto: qui e in `hardware.project_root()`.

Le due devono restare d'accordo sui segni cercati: se ne aggiungi uno qui,
aggiungilo anche li', o uno script in `scripts/` e un modulo in radice
finiranno per puntare a cartelle diverse.
"""

from __future__ import annotations

import sys
from pathlib import Path

QUI = Path(__file__).resolve().parent

# Gli stessi segni di hardware.project_root(): la cartella che contiene
# requirements.txt oppure il checkout git.
RADICE = next(
    (
        candidato
        for candidato in (QUI, *QUI.parents)
        if (candidato / "requirements.txt").exists() or (candidato / ".git").exists()
    ),
    QUI,
)

if str(RADICE) not in sys.path:
    sys.path.insert(0, str(RADICE))
