#!/usr/bin/env python3
"""Test dell'avviso di RAM bassa (P2 #19).

Esegui con: python -m unittest test_ram_warning -v

Il punto: i MODELLI non si adattano alla RAM (a differenza di batch, thread
ed encoder). Su una macchina piccola i default sono pesanti, e il progetto
sceglie di AVVISARE invece di abbassare il modello di nascosto, perche' un
risultato peggiore che nessuno ha scelto e' peggio di un avviso.
"""

from __future__ import annotations

import logging
import unittest
from io import StringIO
from unittest.mock import patch

import config
import machine_setup as ms

GB = 1024**3


class _Args:
    """args minimi, come li prepara argparse."""

    transcriber = "auto"
    whisper_device = "cpu"
    whisper_compute_type = "int8"
    openvino_device = "GPU"
    openvino_model_dir = "x"
    video_encoder = "auto"
    whisper_model = "small"
    semantic_model = config.DEFAULT_EMBEDDING_MODEL


class TestWarnRamBassa(unittest.TestCase):
    def _cattura(
        self,
        ram_gb: float | None,
        modello: str,
        modello_embedding: str | None = None,
    ) -> list[str]:
        buf = StringIO()
        handler = logging.StreamHandler(buf)
        logger = logging.getLogger()
        vecchio = logger.level
        logger.addHandler(handler)
        # INFO, non WARNING: l'utente vede anche le righe informative, e
        # quello che va provato e' "cosa dice a chi legge", non "quali righe
        # hanno una certa priorita'".
        logger.setLevel(logging.INFO)
        try:
            args = _Args()
            args.whisper_model = modello
            if modello_embedding is not None:
                args.semantic_model = modello_embedding
            with patch("hardware.ram_total_bytes", return_value=ram_gb):
                ms._warn_ram_bassa(args)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(vecchio)
        return [r for r in buf.getvalue().splitlines() if r.strip()]

    def test_avvisa_su_macchina_piccola(self) -> None:
        righe = self._cattura(4 * GB, "small")
        self.assertTrue(righe, "4 GB devono far scattare l'avviso")
        testo = "\n".join(righe)
        self.assertIn("4.0 GB", testo)
        self.assertIn("WHISPER_MODEL=tiny", testo)

    def test_avvisa_anche_su_macchina_media(self) -> None:
        # 8 GB: il batch e' gia' ridotto, ma i modelli restano pesanti.
        self.assertTrue(self._cattura(8 * GB, "small"))

    def test_non_avvisa_su_macchina_generosa(self) -> None:
        self.assertEqual(self._cattura(16 * GB, "small"), [])

    def test_non_avvisa_su_macchina_ignota(self) -> None:
        # Se la RAM non e' rilevabile non si inventa un verdetto.
        self.assertEqual(self._cattura(None, "small"), [])

    def test_non_avvisa_se_ha_gia_scelto_un_modello_piccolo(self) -> None:
        # Dirgli di scendere quando ci e' gia' sceso sarebbe rumore. Il
        # modello che pesa e' quello di embedding: e' lui da 4,3 GB, non
        # whisper. Abbassare solo whisper NON giustifica il silenzio.
        self.assertEqual(
            self._cattura(
                4 * GB, "tiny", config.DEFAULT_EMBEDDING_MODEL_ALTERNATE
            ),
            [],
        )

    def test_avvisa_con_whisper_piccolo_e_embedding_grande(self) -> None:
        # Il caso pericoloso: 4 GB, WHISPER_MODEL=tiny e l'embedding grande
        # di default. Prima l'avviso taceva e l'utente scopriva l'OOM a run
        # avviata, senza sapere che restava il modello da 4,3 GB.
        righe = self._cattura(4 * GB, "tiny")
        self.assertTrue(righe)
        self.assertIn("4,3 GB", "\n".join(righe))

    def test_dice_sempre_cosa_mettere_in_env(self) -> None:
        testo = "\n".join(self._cattura(4 * GB, "small"))
        self.assertIn(".env", testo)
        self.assertIn("WHISPER_MODEL=tiny", testo)

    def test_dichiara_che_il_batch_e_gia_ridotto(self) -> None:
        # Altrimenti l'utente pensa che non sia stato adattato nulla.
        testo = "\n".join(self._cattura(4 * GB, "small"))
        self.assertIn("batch", testo.lower())

    def test_cita_il_modello_in_use(self) -> None:
        testo = "\n".join(self._cattura(4 * GB, "medium"))
        self.assertIn("medium", testo)

    # --- Criterio di progetto: la qualita' non si baratta con la fattibilita' ---

    def test_dice_che_il_modello_migliore_resta(self) -> None:
        """Su un PC piccolo si spendono tempo e risorse, non qualita'."""
        testo = "\n".join(self._cattura(4 * GB, "small")).lower()
        self.assertIn("resta il migliore", testo)

    def test_il_modello_piu_leggero_porta_con_se_il_costo_misurato(self) -> None:
        """Non basta proporlo: bisogna dire che peggiora la sincronizzazione.

        Offrire l'alternativa senza il costo fa scegliere all'utente credendo
        che sia equivalente: sull'A/B col materiale reale la similarita' media
        scende da 0.791 a 0.421.
        """
        testo = "\n".join(self._cattura(4 * GB, "small"))
        self.assertIn("EMBEDDING_MODEL=", testo)
        self.assertIn("0.791", testo)
        self.assertIn("0.421", testo)

    def test_non_presenta_il_modello_piccolo_como_alleggerimento_gratuito(self) -> None:
        """"Per alleggerire" toglieva la natura del compromesso."""
        testo = "\n".join(self._cattura(4 * GB, "small")).lower()
        self.assertNotIn("per alleggerire", testo)


class TestNonAlzaMai(unittest.TestCase):
    def test_ram_ignota_non_rompe(self) -> None:
        with patch("hardware.ram_total_bytes", return_value=None):
            ms._warn_ram_bassa(_Args())  # non deve sollevare

    def test_args_senza_whisper_model(self) -> None:
        # Non tutti i chiamanti passano args completi (i test, gli script di
        # analisi): l'avviso non deve dipendere da un attributo che manca.

        class _ArgsMinimali:
            transcriber = "auto"
            whisper_device = "cpu"
            whisper_compute_type = "int8"
            openvino_device = "GPU"
            openvino_model_dir = "x"
            video_encoder = "auto"

        with patch("hardware.ram_total_bytes", return_value=4 * GB):
            ms._warn_ram_bassa(_ArgsMinimali())  # type: ignore[arg-type]
        # senza attributo: deve comunque reggere (messaggio con il default)


if __name__ == "__main__":
    unittest.main()
