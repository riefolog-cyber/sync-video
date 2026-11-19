#!/usr/bin/env python3
"""Igiene del repository pubblico.

Il progetto e' pubblico, e `.gitignore` esiste anche per non committare cose
personali (`SETUP_ARM.md`). Un incollaggio sbagliato pero' basta: un percorso
`C:\\Users\\...`, un token, un modello da 2 GB. Una volta nella storia di git,
toglierlo non basta piu'.

Qui si guardano solo i file VERSIONATI: quello che e' in `.cache/` o in
`.gitignore` non finisce nel repository e non riguarda questo test.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

RADICE = Path(__file__).resolve().parent.parent

# Dimensione oltre la quale un file versionato e' quasi certamente un errore
# (modelli, video, PDF personali). I file del progetto stanno sotto 1 MB.
MASSIMO_BYTE = 3 * 1024 * 1024

# Segreti veri: non esistono esempi legittimi, quindi si cercano sempre.
PATTERN = {
    "token GitHub": re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    "chiave OpenAI/Anthropic": re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    "chiave privata": re.compile(r"BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY"),
}


def _percorsi_personali() -> list[re.Pattern[str]]:
    """I percorsi di QUESTA macchina, non gli esempi.

    Un esempio generico (``C:\\Users\\X``) e' normale nei test e nella
    documentazione: qui conta il percorso VERO, quello che si incolla per
    sbaglio — la cartella del progetto, o la cartella utente di chi esegue il
    test. Su un'altra macchina il nome utente e' diverso, quindi il controllo
    non produce falsi allarmi.
    """
    progetto = str(RADICE).replace("\\", "/")
    utente = Path.home().name
    return [
        re.compile(re.escape(progetto), re.IGNORECASE),
        re.compile(
            rf"[A-Za-z]:\\\\Users\\\\{re.escape(utente)}(?![A-Za-z0-9_-])",
            re.IGNORECASE,
        ),
    ]


def _versionati() -> list[Path]:
    """I file che finiscono nel repository: tracciati + nuovi non ignorati.

    I secondi sono quelli che stai per committare: e' li' che un percorso
    personale incollato ieri non ha ancora nessun controllo addosso. Gli
    ignorati (`.cache/`, gli audio del progetto) restano fuori: non sono
    pubblici e il loro contenuto non riguarda il repository.
    """
    comandi = (
        ["git", "ls-files"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    )
    file: list[Path] = []
    for comando in comandi:
        try:
            esito = subprocess.run(
                comando,
                cwd=RADICE,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise unittest.SkipTest(f"git non disponibile: {e}") from e
        if esito.returncode != 0:
            raise unittest.SkipTest("git non risponde (non e' un checkout git?)")
        file += [RADICE / riga.strip() for riga in esito.stdout.splitlines() if riga.strip()]
    if not file:
        raise unittest.SkipTest("nessun file versionato")
    return file


class TestIgieneRepository(unittest.TestCase):
    def test_nessun_file_enorme(self) -> None:
        enormi = [
            (str(p.relative_to(RADICE)), p.stat().st_size)
            for p in _versionati()
            if p.is_file() and p.stat().st_size > MASSIMO_BYTE
        ]
        self.assertEqual(
            enormi,
            [],
            "file troppo grandi versionati (vanno in .gitignore): "
            + ", ".join(f"{n} ({b // 1024} KB)" for n, b in enormi),
        )

    def test_nessun_percorso_o_chiave_personale(self) -> None:
        trovati: list[str] = []
        for path in _versionati():
            try:
                testo = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # binario o illeggibile: lo guarda il test qui sopra
            nome = str(path.relative_to(RADICE))
            for riga_numero, riga in enumerate(testo.splitlines(), 1):
                for etichetta, pattern in PATTERN.items():
                    if pattern.search(riga):
                        trovati.append(f"{nome}:{riga_numero} ({etichetta})")
                for pattern in _percorsi_personali():
                    if pattern.search(riga):
                        trovati.append(f"{nome}:{riga_numero} (percorso di questa macchina)")
        self.assertEqual(trovati, [], "dati personali nel repository:\n" + "\n".join(trovati))


if __name__ == "__main__":
    unittest.main()
