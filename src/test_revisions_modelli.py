#!/usr/bin/env python3
"""Le revisioni dei modelli devono ESISTERE davvero su HuggingFace.

Perche' questo test esiste: durante la stesura avevo scritto a mano un
commit hash per `OpenVINO/whisper-tiny-fp16-ov` senza leggerlo. Sembrava
un SHA plausibile, e avrebbe fatto fallire il download con un 404 su ogni
PC - cioe' esattamente il guasto che le revisioni servono a prevenire.

Un hash plausibile non e' un hash vero. L'unica verifica e' chiedere
all'API di HuggingFace se quella revisione esiste, e qui non si fa rete:
si controlla che il valore sia un SHA-1 valido e che non siail segnaposto
usato nelle stringhe d'esempio.

Per il controllo vero sull'API c'e' il comando lento (SYNC_VIDEO_RUN_SLOW=1)
in fondo, che salta senza rete.

Esegui con: python -m unittest test_revisions_modelli -v
"""

from __future__ import annotations

import re
import unittest

from config import OPENVINO_MODEL_REVISIONI, WHISPER_REVISIONI
from test_slow import slow

SHA1 = re.compile(r"^[0-9a-f]{40}$")

# Hash usati nelle stringhe d'esempio della documentazione: compaiono nella
# maggior parte degli esempi di commit SHA, e non identificano niente.
SEGNAPOSTI = {
    "3f1a1e5c0d4e9b3f2b1a4c5d6e7f8091a2b3c4d",  # inventato scrivendo a mano
    "0000000000000000000000000000000000000000",
    "1234567890abcdef1234567890abcdef12345678",
}


class TestRevisioniModelli(unittest.TestCase):
    """Ogni revisione deve essere un SHA-1 vero, non un segnaposto."""

    def test_tutte_le_whisper_sono_sha1_validi(self) -> None:
        for taglio, sha in WHISPER_REVISIONI.items():
            with self.subTest(taglio=taglio):
                self.assertRegex(sha, SHA1, f"whisper {taglio}: {sha!r} non e' un SHA-1")

    def test_tutte_le_openvino_sono_sha1_validi(self) -> None:
        for repo, sha in OPENVINO_MODEL_REVISIONI.items():
            with self.subTest(repo=repo):
                self.assertRegex(sha, SHA1, f"{repo}: {sha!r} non e' un SHA-1")

    def test_nessun_segnaposto(self) -> None:
        """Il fallimento vero e' stato scrivere un hash plausibile a mano."""
        for sha in list(WHISPER_REVISIONI.values()) + list(OPENVINO_MODEL_REVISIONI.values()):
            with self.subTest(sha=sha):
                self.assertNotIn(sha, SEGNAPOSTI, f"{sha} e' un segnaposto, non un commit reale")

    def test_nessuna_revisione_vuota_o_spuria(self) -> None:
        self.assertTrue(WHISPER_REVISIONI, "la tabella whisper e' vuota")
        for taglio, sha in WHISPER_REVISIONI.items():
            with self.subTest(taglio=taglio):
                self.assertTrue(sha.strip(), f"whisper {taglio}: revisione vuota")

    def test_copre_i_tagli_supportati(self) -> None:
        """Un taglio ammesso dal CLI ma non pinnato lavora senza rete."""
        for taglio in ("tiny", "base", "small", "medium", "large-v3"):
            with self.subTest(taglio=taglio):
                self.assertIn(taglio, WHISPER_REVISIONI)

    def test_il_modello_openvino_di_default_e_pinnato(self) -> None:
        from config import DEFAULT_OPENVINO_MODEL_ID

        self.assertIn(
            DEFAULT_OPENVINO_MODEL_ID,
            OPENVINO_MODEL_REVISIONI,
            f"{DEFAULT_OPENVINO_MODEL_ID} non ha una revisione: tornerebbe a scaricare il main di oggi",
        )


class TestRevisioniEsistonoDavvero(unittest.TestCase):
    """Il controllo vero, in rete. Salta senza SYNC_VIDEO_RUN_SLOW=1."""

    @slow("interroga l'API di HuggingFace: serve rete")
    def test_ogni_revisione_risponde_200(self) -> None:
            from huggingface_hub import HfApi

            api = HfApi()
            controlli = [(f"Systran/faster-whisper-{t}", s) for t, s in WHISPER_REVISIONI.items()]
            controlli += list(OPENVINO_MODEL_REVISIONI.items())
            for repo, sha in controlli:
                with self.subTest(repo=repo):
                    info = api.model_info(repo, revision=sha)
                    self.assertIsNotNone(info, f"{repo}@{sha} non esiste")


if __name__ == "__main__":
    unittest.main()
