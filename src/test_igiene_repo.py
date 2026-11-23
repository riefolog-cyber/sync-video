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
        # [\\/]+: su Windows il percorso vero ha backslash singoli, ma nei
        # file di test/doc comparisono anche doppi (stringhe Python) o slash
        # (percorsi normalizzati). Con `\\\\` in raw string il regex trovava
        # solo il backslash DOPPIO e lasciava passare quello normale.
        re.compile(
            rf"[A-Za-z]:[\\/]+Users[\\/]+{re.escape(utente)}(?![A-Za-z0-9_-])",
            re.IGNORECASE,
        ),
    ]


def _da_git(*extra: str) -> list[Path]:
    """I file che `git ls-files` elenca (skip se git non c'e')."""
    try:
        esito = subprocess.run(
            ["git", "ls-files", *extra],
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
    return [RADICE / riga.strip() for riga in esito.stdout.splitlines() if riga.strip()]


def _file_da_controllare() -> tuple[list[Path], list[Path]]:
    """(tracciati, nuovi non ignorati).

    Il limite di DIMENSIONE si applica solo ai tracciati: un nuovo file puo'
    essere un artefatto locale legittimo (il video di una lezione, il PDF del
    deck) che `.gitignore` non copre per nome, e farlo passare per "file
    enorme" renderebbe la suite rossa per una cosa normale.

    I SEGRETI e i percorsi personali si cercano in entrambi: e' proprio nei
    file che stai per committare che non c'e' ancora nessun controllo.
    """
    tracciati = _da_git()
    nuovi = _da_git("--others", "--exclude-standard")
    if not tracciati and not nuovi:
        raise unittest.SkipTest("nessun file da controllare (non e' un checkout git?)")
    return tracciati, nuovi


class TestIgieneRepository(unittest.TestCase):
    def test_nessun_file_enorme(self) -> None:
        tracciati, _nuovi = _file_da_controllare()
        enormi = [
            (str(p.relative_to(RADICE)), p.stat().st_size)
            for p in tracciati
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
        tracciati, nuovi = _file_da_controllare()
        for path in tracciati + nuovi:
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

    def test_percorso_personale_singolo_doppio_e_con_slash(self) -> None:
        """Il controllo deve vedere il percorso vero, in tutte le sue forme."""
        utente = Path.home().name
        pattern = _percorsi_personali()[1]  # quello del nome utente
        for percorso in (
            rf"C:\Users\{utente}\x",
            rf"C:\\Users\\{utente}\\x",
            f"C:/Users/{utente}/x",
        ):
            with self.subTest(percorso=percorso):
                self.assertRegex(percorso, pattern)

        # Un altro utente non e' un dato personale di QUESTA macchina. Il
        # suffisso '_altro' verifica anche che il nome non sia solo un prefisso.
        self.assertNotRegex(rf"C:\Users\{utente}_altro\x", pattern)
        self.assertNotRegex(r"C:\Users\qualcun_altro\x", pattern)


if __name__ == "__main__":
    unittest.main()
