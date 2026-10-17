"""Mette nel sys.path degli script in `src/scripts/` le cartelle che servono.

Un eseguibile in `scripts/` parte con `scripts/` come sys.path[0]: non la
cartella del codice (i moduli core `config`, `semantic_sync`, `chunks`...) ne'
la radice del progetto (`.cache/`, `video_finale.mp4`, `.env`). Questo file
risolve il problema e va importato **per primo**, prima di qualsiasi modulo
del progetto:

    from _bootstrap import RADICE   # noqa: I001  (deve stare per primo)

    import config

Espone due percorsi, e NON sono la stessa cartella:

- `RADICE`, la cartella del progetto: ci stanno i DATI (`requirements.txt`,
  `.git`, `.cache/`, il video finito). Era l'unica prima dello spostamento
  del codice in `src/`, quando codice e dati stavano insieme.
- `CODICE`, la cartella dei moduli core: un livello sopra `scripts/`, cioe'
  `src/`. E' quella che serve agli `import`.

Tenerle distinte e' l'intero punto: confonderle e' esattamente il modo in cui
questo file si e' rotto (metteva solo `RADICE`, e da `scripts/` nessuno
importava piu' niente: `analysis_sync.py` e `check_fastembed_upgrade.py`
uscivano subito con `ModuleNotFoundError`).

Unica eccezione alla regola "una sola definizione della radice": qui la
ricerca dei segni e' riscritta invece di riusare `hardware.project_root()`,
perche' `hardware.py` sta nella cartella del codice e importarlo richiederebbe
gia' di averla trovata. Sono quattro righe, e sono l'unico posto in cui la
logica appare due volte in tutto il progetto: qui e in
`hardware.project_root()`.

Le due devono restare d'accordo sui segni cercati: se ne aggiungi uno qui,
aggiungilo anche li', o uno script in `scripts/` e un modulo core finiranno
per puntare a cartelle diverse.
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

# I moduli core stanno un livello sopra `scripts/`: e' una convenzione di
# questa cartella (scripts/ sta DENTRO la cartella del codice), non un segno
# da cercare. Se un giorno `scripts/` si sposta, questo e' il punto da
# cambiare, e va cambiato con i test: `test_fastembed_ab` e
# `test_check_embedding_models` importano questi script da `src/`.
CODICE = QUI.parent

# Prima il codice, poi la radice (inserendo per ultimo il primo della lista):
# cosi' `import config` trova il modulo in `src/`, e `RADICE / ".cache"`
# continua a puntare ai dati del progetto. La radice non contiene moduli
# importabili, quindi non puo' nasconderne nessuno.
for _cartella in (RADICE, CODICE):
    if str(_cartella) not in sys.path:
        sys.path.insert(0, str(_cartella))
