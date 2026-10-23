#!/usr/bin/env python3
"""Il referto non deve mentire sul motore di trascrizione.

Il buco che copre: `verifica_pc` scriveva "whisper su CPU" e basta. Su un PC
appena estratto dal ZIP, col runtime OpenVINO gia' installato e solo il
modello IR da scaricare, quel messaggio si legge come "questo PC non ha
accelerazione". Non e' vero: al primo run il motore diventa OpenVINO.

Misurato sullo stesso PC e nella stessa cartella:
    prima del run:  [ ok ] Motore  whisper su CPU
    dopo il run:   [ ok ] Motore  openvino (iGPU/CPU Intel)

Un referto che dà una impressione sbagliata e' peggio di uno che non parla:
e' il tool che i colleghi aprono quando qualcosa non va, e se racconta una
causa falsa li porta a cercare il difetto nel posto sbagliato.

Le tre cause vanno tenute distinte, con rimedi diversi:
  1. runtime OpenVINO assente       -> installarlo
  2. runtime presente, nessuna GPU  -> resta su CPU di proposito, va bene
  3. runtime e GPU presenti, manca
     solo il modello                -> TEMPORANEO, si risolve al primo run

Confondere 2 con 3 prometterebbe OpenVINO su un PC che non lo avrà mai:
stesso errore di prima, con un'altra frase.

Esegui con: python -m unittest test_motore_referto -v
"""

from __future__ import annotations

import unittest
from unittest import mock

import verifica_pc


class TestMotoreReferto(unittest.TestCase):
    def _esito(self, *, genai: bool, gpu: bool) -> verifica_pc.Esito:
        """Il referto nei tre scenari: runtime assente, senza GPU, senza modello.

        Il runtime si blocca agendo su `__import__`, perche' dentro la funzione
        `import openvino_genai` deve sollevare ImportError e su questo PC il
        pacchetto c'e': l'unico modo per toglierlo e' questo.
        """
        import builtins

        import transcription

        vero_import = builtins.__import__

        def import_(nome: str, *a: object, **k: object) -> object:
            if nome.split(".")[0] == "openvino_genai" and not genai:
                raise ImportError("assente per test")
            return vero_import(nome, *a, **k)  # type: ignore[arg-type]

        with (
            mock.patch.object(builtins, "__import__", import_),
            mock.patch.object(transcription, "openvino_device_available", lambda: gpu),
            # Questi test parlano di OpenVINO: senza il patch, su un PC con
            # GPU NVIDIA e CUDA funzionante il referto direbbe "su CUDA" e i
            # tre scenari non sarebbero piu' deterministici.
            mock.patch("config.cuda_available", return_value=False),
        ):
            return verifica_pc._motore_senza_openvino("whisper")

    # --- 1. runtime assente ---
    def test_runtime_assente_dice_come_installarlo(self) -> None:
        e = self._esito(genai=False, gpu=False)
        self.assertIn("non e' installato", e.dettaglio)
        self.assertIn("aggiornamenti.bat", e.rimedio or "")

    # --- 2. runtime presente, nessuna GPU ---
    def test_senza_gpu_non_promette_openvino(self) -> None:
        """Il caso che un messaggio generico sbaglierebbe.

        Qui il modello scaricato NON cambierebbe niente: promettere OpenVINO
        sarebbe falso esattamente come lo era "whisper su CPU" da solo.
        """
        e = self._esito(genai=True, gpu=False)
        testo = f"{e.dettaglio} {e.rimedio}"
        self.assertIn("iGPU", e.dettaglio)
        self.assertNotIn("Temporaneo", testo)
        self.assertNotIn("passa a OpenVINO", testo)

    # --- 3. runtime e GPU, manca solo il modello ---
    def test_manca_il_modello_lo_dice_temporaneo(self) -> None:
        e = self._esito(genai=True, gpu=True)
        self.assertIn("Temporaneo", e.rimedio or "")
        self.assertIn("passa a OpenVINO", e.rimedio or "")

    # --- il difetto originale ---
    def test_il_dettaglio_non_e_mai_bare(self) -> None:
        """Nessuno dei tre casi puo' restare un "whisper su CPU" nudo.

        Era esattamente quello il buco: la stringa senza contesto.
        """
        for genai, gpu, atteso in ((False, False, "installato"), (True, False, "iGPU"), (True, True, "modello")):
            with self.subTest(caso=atteso):
                e = self._esito(genai=genai, gpu=gpu)
                self.assertNotEqual(e.dettaglio, "whisper su CPU")
                self.assertIn(atteso, e.dettaglio)
                self.assertTrue(e.rimedio, f"caso {atteso}: nessun rimedio")

    def test_nessun_caso_puoo_dire_che_e_tutto_a_posto(self) -> None:
        """Motore su CPU e' un avviso, non un [ ok ].

        Se fosse [ ok ] il collega passerebbe oltre senza leggere il resto,
        che e' proprio il caso in cui si accetta che il PC non acceleri.
        """
        for genai, gpu in ((False, False), (True, False), (True, True)):
            with self.subTest(genai=genai, gpu=gpu):
                self.assertEqual(self._esito(genai=genai, gpu=gpu).stato, verifica_pc.AVV)


class TestDalVeroIngresso(unittest.TestCase):
    """Passa per `prova_motore`, non per l'helper.

    Senza questo i test sopra verificano una funzione che il percorso vero
    potrebbe non chiamare: tornato il `Esito(... OK, f"{scelto} su CPU")` nel
    ramo `else`, l'helper esiste e funziona benissimo, i suoi test passano
    tutti e il difetto e' di nuovo a posto senza che nessuno se ne accorga.
    Provato: la mutazione passava senza errori.

    E' quindi questo il test che deve fallire quando il messaggio torna nudo.
    """

    def _esito_da_prova_motore(self) -> verifica_pc.Esito:
        import transcription

        r = verifica_pc.Relazione()
        with (
            mock.patch.object(transcription, "resolved_transcriber", lambda *a, **k: "whisper"),
            mock.patch.object(transcription, "openvino_usable", lambda *a, **k: False),
            mock.patch("config.cuda_available", return_value=False),
        ):
            verifica_pc.prova_motore(r)
        return next(e for e in r.esiti if e.nome == "Motore")

    def test_il_referto_pubblico_non_e_un_messaggio_nudo(self) -> None:
        e = self._esito_da_prova_motore()
        self.assertNotEqual(e.dettaglio, "whisper su CPU", "il difetto originale e' tornato")
        self.assertTrue(e.rimedio, "il referto pubblico non ha rimedi")
        self.assertEqual(e.stato, verifica_pc.AVV)

    def test_il_referto_pubblico_distingue_le_cause(self) -> None:
        """Almeno la causa deve essere nominata, quale che sia."""
        e = self._esito_da_prova_motore()
        cause = ("non e' installato", "iGPU", "manca solo il modello")
        self.assertTrue(
            any(c in e.dettaglio for c in cause),
            f"nessuna delle cause note ({cause}) compare in: {e.dettaglio}",
        )


class TestCudaNelReferto(unittest.TestCase):
    """GPU NVIDIA: il referto deve dire se CUDA e' DAVVERO utilizzabile.

    Il caso reale (10/2026): una GPU c'era e il driver la esponeva, ma
    mancavano cuBLAS/cuDNN. Il referto non poteva dirlo, e la run moriva a
    meta' trascrizione con "Library cublas64_12.dll is not found or cannot be
    loaded". Ora il difetto si vede prima, col rimedio.
    """

    def _esito(self, mancante: str | None) -> verifica_pc.Esito:
        with (
            mock.patch("config.cuda_available", return_value=True),
            mock.patch("config.cuda_runtime_lib_mancante", return_value=mancante),
        ):
            return verifica_pc._motore_senza_openvino("whisper")

    def test_gpu_e_librerie_ok(self) -> None:
        e = self._esito(None)
        self.assertEqual(e.stato, verifica_pc.OK)
        self.assertIn("CUDA", e.dettaglio)

    def test_gpu_senza_librerie_dice_cosa_installare(self) -> None:
        e = self._esito("cublas64_12.dll")
        self.assertEqual(e.stato, verifica_pc.AVV)
        self.assertIn("cublas64_12.dll", e.dettaglio)
        self.assertIn("nvidia-cublas-cu12", e.rimedio)
        self.assertIn("CPU", e.rimedio)


if __name__ == "__main__":
    unittest.main()
