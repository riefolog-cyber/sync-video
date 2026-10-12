#!/usr/bin/env python3
"""Codice che sembra morto ma non lo e': qui viene protetto.

In questo progetto ci sono funzioni che `main.py` non chiama mai. Non e'
un errore di pulizia: sono le implementazioni di RIFERIMENTO, usate dai
test per dimostrare che le versioni veloci che le hanno sostituite danno
lo stesso risultato.

Il pericolo e' il contrario di quello che sembra: cancellarle lascia
tutti i test VERDI (l'implementazione nuova resta testata) e pero'
togli l'unico confronto che poteva accorgersi di una regressione
nell'ottimizzazione. Un codice morto che, se rimosso, rompe la rete di
sicurezza.

`test_cerca_il_codice_morto.py` elenca questi nomi; se uno sparisce o
perde la spiegazione, questo test fallisce.

Esegui con: python -m unittest test_codice_morto -v
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

import semantic_sync
import timeline
import video

SRC = Path(__file__).resolve().parent

# nome -> (modulo, perche' resta, nome del test che lo usa come riferimento)
ORACOLI: dict[str, tuple[object, str, str]] = {
    "image_similarity": (
        video,
        "riferimento per il confronto vettoriale accelerato (79s -> 1.4s)",
        "test_video.py",
    ),
    "extract_timeline_from_transcript": (
        timeline,
        "riferimento per l'estrazione timeline a finestra, superata dalle ancore dichiarate",
        "test_sync.py",
    ),
    "reset_embed_cache": (
        semantic_sync,
        "gancio per i test: svuota il memo embedding fra un caso e l'altro",
        "test_sync.py",
    ),
}

# frase che deve restare nel docstring: e' la spiegazione, e deve viaggiare
# con il codice invece di stare in un messaggio di commit che nessuno legge
MARCA = "NON CHIAMARE DA `main.py`"
MARCA_TEST = "usato dai test"


def _chiamate_da_main() -> set[str]:
    """Nomi chiamati dal file d'ingresso, esclusi i suoi import."""
    albero = ast.parse((SRC / "main.py").read_text(encoding="utf-8"))
    chiamati: set[str] = set()
    for nodo in ast.walk(albero):
        if not isinstance(nodo, ast.Call):
            continue
        func = nodo.func
        chiamati.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    return chiamati


def _citati_dai_test(nome: str) -> list[str]:
    """File di test che chiamano davvero il nome.

    Conta sia `nome(...)` sia `modulo.nome(...)`: gli oracoli si chiamano
    in tutti e due i modi e il primo controllo ne ignorava meta'.
    """
    trovati: list[str] = []
    for path in sorted(SRC.glob("test_*.py")):
        albero = ast.parse(path.read_text(encoding="utf-8"))
        for nodo in ast.walk(albero):
            if not isinstance(nodo, ast.Call):
                continue
            func = nodo.func
            chiamato = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if chiamato == nome:
                trovati.append(path.name)
                break
    return trovati


class TestCodiceCheSembraMorto(unittest.TestCase):
    def test_gli_oracoli_esistono_ancora(self) -> None:
        """Il primo motivo per cui questo file esiste."""
        for nome, (modulo, _, _) in ORACOLI.items():
            with self.subTest(nome=nome):
                self.assertTrue(
                    callable(getattr(modulo, nome, None)),
                    f"{nome} e' sparito: senza il riferimento i test verificano "
                    f"l'implementazione nuova contro se stessa",
                )

    def test_il_docstring_spiega_perche_restano(self) -> None:
        """La spiegazione deve stare nel codice, non in un commit."""
        for nome, (modulo, motivo, _) in ORACOLI.items():
            with self.subTest(nome=nome):
                doc = getattr(modulo, nome).__doc__ or ""
                marcatore = MARCA_TEST if nome == "reset_embed_cache" else MARCA
                self.assertIn(marcatore, doc, f"{nome}: docstring senza la spiegazione ({motivo})")

    def test_sono_davvero_usati_dai_test(self) -> None:
        """Un oracolo non citato da nessun test e' solo codice morto."""
        for nome, (_, _, atteso) in ORACOLI.items():
            with self.subTest(nome=nome):
                citati = _citati_dai_test(nome)
                self.assertTrue(
                    citati,
                    f"{nome} non e' chiamato da nessun test: e' codice morto, "
                    f"non un riferimento (atteso: {atteso})",
                )

    def test_main_non_le_chiama(self) -> None:
        """L'oracolo non deve finire nel flusso vero: e' lento per scelta.

        Se un giorno main.py la chiama, il confronto con la versione
        veloce non ha piu' senso: questo test segnala di ripensare il
        riferimento invece di lasciare due implementazioni in concorrenza
        silenziosamente.
        """
        chiamati = _chiamate_da_main()
        for nome in ORACOLI:
            if nome == "reset_embed_cache":
                continue  # gancio di test, non un oracolo di confronto
            with self.subTest(nome=nome):
                self.assertNotIn(nome, chiamati)


if __name__ == "__main__":
    unittest.main()
