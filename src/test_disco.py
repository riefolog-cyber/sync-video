#!/usr/bin/env python3
"""Test del controllo dello spazio disco al primo avvio (P2 #18).

Esegui con: python -m unittest test_disco -v

Il problema che copre: il progetto scarica ~6.9 GB al primo avvio senza
aver mai controllato che il disco li regga. Su un PC con poco spazio il
download parte, il disco si riempie e la run muore a meta' con un errore
che non nomina la causa.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import config
import hardware

GB = 1024**3


class TestDiskFreeBytes(unittest.TestCase):
    def test_ritorna_un_numero_positivo(self) -> None:
        libero = hardware.disk_free_bytes(Path(tempfile.gettempdir()))
        if libero is not None:
            self.assertGreater(libero, 0)

    def test_percorso_inesistente_risale_alla_cartella_esistente(self) -> None:
        # La cache non esiste ancora al primo avvio: shutil.disk_usage su un
        # percorso inesistente leverebbe, quindi si risale all'antenato.
        profondo = Path(tempfile.gettempdir()) / "non-esiste-xyz" / "a" / "b" / "c"
        libero = hardware.disk_free_bytes(profondo)
        assert libero is not None
        self.assertGreater(libero, 0)

    def test_nessun_dato_ritorna_none_e_non_solleva(self) -> None:
        with patch.object(hardware.shutil, "disk_usage", side_effect=OSError("drive non pronto")):
            self.assertIsNone(hardware.disk_free_bytes(Path("C:/")))


class TestSpazioNecessario(unittest.TestCase):
    """Le cifre mostrate all'utente e i byte usati dal controllo non
    possono divergere: altrimenti l'avviso promette una cosa e ne controlla
    un'altra."""

    def test_tabelle_coerenti(self) -> None:
        for chiave, testo in config._DIMENSIONI_WHISPER.items():
            trovato = re.search(r"([\d.]+)", testo)
            assert trovato is not None, testo
            numero = float(trovato.group(1))
            fattore = 1024**3 if "GB" in testo else 1024**2
            with self.subTest(modello=chiave):
                self.assertAlmostEqual(
                    config._BYTES_WHISPER[chiave] / fattore, numero, delta=numero * 0.02
                )
        trovato = re.search(r"([\d.]+)", config._CACHE_EMBEDDING_DEFAULT)
        assert trovato is not None
        numero = float(trovato.group(1))
        self.assertAlmostEqual(
            config._BYTES_CACHE_EMBEDDING / 1024**3, numero, delta=numero * 0.02
        )

    def test_somma_di_embedding_e_whisper(self) -> None:
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(config, "_cache_whisper_faster", return_value=Path(d) / "hf"),
        ):
            totale = config._spazio_modelli_necessario(
                Path(d) / "emb", "small", config.DEFAULT_EMBEDDING_MODEL
            )
        assert totale is not None
        self.assertAlmostEqual(totale / GB, 6.88, delta=0.05)

    def test_il_modello_piu_piccolo_occupa_meno(self) -> None:
        importanti: dict[str, int] = {}
        with tempfile.TemporaryDirectory() as d:
            for modello in ("tiny", "base", "small"):
                with patch.object(config, "_cache_whisper_faster", return_value=Path(d) / "hf"):
                    got = config._spazio_modelli_necessario(
                        Path(d) / f"e_{modello}", modello, config.DEFAULT_EMBEDDING_MODEL
                    )
                assert got is not None, modello
                importanti[modello] = got
        self.assertLess(importanti["tiny"], importanti["base"])
        self.assertLess(importanti["base"], importanti["small"])

    def test_dimensione_ignora_ritorna_none(self) -> None:
        # whisper large non ha una dimensione misurata: meglio nessun
        # controllo che un controllo con una cifra inventata.
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(config, "_cache_whisper_faster", return_value=Path(d) / "hf"),
        ):
            got = config._spazio_modelli_necessario(
                Path(d) / "emb", "large", config.DEFAULT_EMBEDDING_MODEL
            )
        self.assertIsNone(got)

    def test_semantic_model_alterno_ignora_ritorna_none(self) -> None:
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(config, "_cache_whisper_faster", return_value=None),
        ):
            got = config._spazio_modelli_necessario(Path(d) / "emb", "small", "altro/modello")
        self.assertIsNone(got)

    def test_già_scaricati_ritorna_zero(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            emb = Path(d) / "emb"
            emb.mkdir()
            (emb / "modello.bin").write_bytes(b"x" * 10)
            with patch.object(config, "_cache_whisper_faster", return_value=None):
                got = config._spazio_modelli_necessario(
                    emb, "small", config.DEFAULT_EMBEDDING_MODEL
                )
        self.assertEqual(got, 0)


class TestDownloadIncompleto(unittest.TestCase):
    """Un download interrotto non puo' passare per uno finito.

    Il difetto che copre: bastava che ci fosse "un file con dentro
    qualcosa" per dichiarare la cache scaricata. Un download interrotto a
    meta' lascia esattamente quello, e il programmo non avvisava, non
    controllava lo spazio, e falliva molto dopo con un errore che non
    nominava la causa.
    """

    def _cache(self, *file: tuple[str, int]) -> Path:
        with tempfile.TemporaryDirectory() as d:
            radice = Path(d)
            for nome, byte in file:
                p = radice / nome
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"x" * byte)
            # La directory temporanea muore con il test, ma il filesystem
            # no: qui basta il risultato, percio' si copia la struttura in
            # una directory che sopravvive alla chiamata.
            import shutil

            copia = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, copia, ignore_errors=True)
            shutil.copytree(radice, copia, dirs_exist_ok=True)
            return copia

    def test_cache_vuota_non_e_scaricata(self) -> None:
        self.assertFalse(config._gia_scaricato(self._cache()))

    def test_file_veri_sono_scaricati(self) -> None:
        cache = self._cache(("modello.onnx", 100), ("blobs/abc", 50))
        self.assertTrue(config._gia_scaricato(cache))

    def test_download_interrotto_non_e_scaricato(self) -> None:
        """Il caso vero: il file c'è, ha byte dentro, ma è a metà."""
        cache = self._cache(("blobs/abc.incomplete", 500), ("config.json", 20))
        self.assertFalse(
            config._gia_scaricato(cache),
            "una cache con un file .incomplete non puo' dirsi scaricata",
        )

    def test_solo_file_temporaneo_non_e_scaricato(self) -> None:
        cache = self._cache(("modello.onnx.part", 900))
        self.assertFalse(config._gia_scaricato(cache))

    def test_download_completato_dopo_e_rilevato(self) -> None:
        """Il rimedio non blocca lo scaricamento vero: quando finisce, passa."""
        cache = self._cache(("blobs/abc", 500))
        self.assertTrue(config._gia_scaricato(cache))


class TestBloccoSuBootstrapReale(unittest.TestCase):
    """Il blocco deve scattare nel percorso vero, non solo nella funzione.

    Si chiama `bootstrap()` come fa `main()`, con i modelli dati per mancanti
    e il disco finto piccolo: se il parametro non arrivasse, il programma
    partirebbe con il download e il test qui sotto passerebbe senza accorgersi
    che il blocco è scollegato.
    """

    def _esegui(self, *, ignora_spazio: bool) -> None:
        # `_spazio_modelli_necessario` va finto insieme a `_modelli_mancanti`:
        # il controllo del disco ricalcola da solo se i modelli mancano, e su
        # questa macchina sono gia' scaricati (senza il finto restituirebbe 0
        # e nonFermerebbe mai niente, con un test che passerebbe per il
        # motivo sbagliato).
        with (
            patch.object(config, "disk_free_bytes", return_value=3 * GB),
            patch.object(config, "_modelli_mancanti", return_value=[("modello embedding", "~2.1 GB")]),
            patch.object(config, "_spazio_modelli_necessario", return_value=8 * GB),
            patch.object(config, "_cache_whisper_faster", return_value=None),
            patch.object(config, "_ensure_pip_packages"),
        ):
            config.bootstrap(ignora_spazio=ignora_spazio)

    def test_bootstrap_ferma_quando_lo_spazio_non_basta(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self._esegui(ignora_spazio=False)
        self.assertEqual(ctx.exception.code, 1)

    def test_bootstrap_prosegue_con_ignora_spazio(self) -> None:
        """Con l'override la run deve arrivare in fondo, non fermarsi."""
        self._esegui(ignora_spazio=True)


class TestAvvisoSpazio(unittest.TestCase):
    def _cattura(self, libero: int | None, modello_whisper: str = "small", *, ignora: bool = False) -> list[str]:
        """Righe emesse dall'avviso, con il disco finto a `libero`.

        Di default lo spazio insufficiente FERMA il download (SystemExit 1):
        la funzione non ritorna. Con `ignora=True` torna al vecchio
        comportamento di avviso e prosegue.
        """
        import logging

        with tempfile.TemporaryDirectory() as d:
            buf = StringIO()
            handler = logging.StreamHandler(buf)
            logger = logging.getLogger()
            vecchio = logger.level
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            try:
                with (
                    patch.object(config, "disk_free_bytes", return_value=libero),
                    patch.object(config, "_cache_whisper_faster", return_value=Path(d) / "hf"),
                ):
                    config._avvisa_spazio_insufficiente(
                        Path(d) / "emb", modello_whisper, config.DEFAULT_EMBEDDING_MODEL, ignora=ignora
                    )
            finally:
                logger.removeHandler(handler)
                logger.setLevel(vecchio)
        return [riga for riga in buf.getvalue().splitlines() if riga.strip()]

    def _ferma(self, libero: int | None, modello_whisper: str = "small") -> list[str]:
        """Come _cattura, ma annota l'uscita invece di propagarla."""
        import logging

        with tempfile.TemporaryDirectory() as d:
            buf = StringIO()
            handler = logging.StreamHandler(buf)
            logger = logging.getLogger()
            vecchio = logger.level
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            try:
                with (
                    patch.object(config, "disk_free_bytes", return_value=libero),
                    patch.object(config, "_cache_whisper_faster", return_value=Path(d) / "hf"),
                    self.assertRaises(SystemExit) as ctx,
                ):
                    config._avvisa_spazio_insufficiente(
                        Path(d) / "emb", modello_whisper, config.DEFAULT_EMBEDDING_MODEL
                    )
                self.assertEqual(ctx.exception.code, 1)
            finally:
                logger.removeHandler(handler)
                logger.setLevel(vecchio)
        return [riga for riga in buf.getvalue().splitlines() if riga.strip()]

    # --- Il contratto nuovo: di default FERMA, non avvisa e basta ---

    def test_ferma_quando_lo_spazio_non_basta(self) -> None:
        righe = self._ferma(3 * GB)
        testo = "\n".join(righe)
        self.assertIn("Spazio", testo)
        self.assertIn("3.0 GB", testo)
        self.assertIn("8.9 GB", testo)

    def test_il_blocco_dice_come_proseguire(self) -> None:
        """Un blocco senza via d'uscita e' un vicolo cieco."""
        testo = "\n".join(self._ferma(3 * GB))
        self.assertIn("--ignora-spazio", testo)
        self.assertIn("--embedding-cache-dir", testo)

    def test_con_ignora_avvisa_e_prosegue(self) -> None:
        """Il comportamento di prima resta disponibile per chi lo chiede."""
        righe = self._cattura(3 * GB, ignora=True)
        self.assertTrue(righe, "con --ignora-spazio deve comunque avvisare")
        self.assertIn("--ignora-spazio", "\n".join(righe))

    def test_ferma_su_entrambe_le_destinazioni(self) -> None:
        # La cache embedding segue il progetto, quella whisper segue la home:
        # possono stare su dischi diversi, e vanno controllate entrambe.
        testo = "\n".join(self._ferma(3 * GB))
        self.assertIn("cartella del progetto", testo)
        self.assertIn("HuggingFace", testo)

    def test_spazio_sufficiente_non_blocca(self) -> None:
        self.assertEqual(self._cattura(100 * GB), [])

    def test_dimensione_ignora_non_blocca(self) -> None:
        # whisper large non e' misurato: niente blocco con cifra inventata.
        self.assertEqual(self._cattura(1 * GB, modello_whisper="large"), [])

    def test_spazio_non_rilevabile_non_blocca(self) -> None:
        # Se il disco non e' interrogabile non si inventa nulla: bloccherebbe
        # un PC su cui lo spazio c'è.
        self.assertEqual(self._cattura(None), [])


class TestIntegrazioneAnnuncio(unittest.TestCase):
    def test_modelli_mancanti_stessa_firma(self) -> None:
        # La coppia (nome, dimensione) e' il formato che i test e i chiamanti
        # fanno: il refactor non deve averla cambiata sotto di loro.
        with tempfile.TemporaryDirectory() as d:
            coppie = config._modelli_mancanti(Path(d) / "emb", "small")
            for voce in coppie:
                self.assertIsInstance(voce, tuple)
                self.assertEqual(len(voce), 2)
                self.assertIsInstance(voce[0], str)
                self.assertIsInstance(voce[1], str)

    def test_annuncio_puo_scattare_il_controllo(self) -> None:
        # _annuncia_primo_avvio non deve più essere un vicolo cieco sul disco.
        with patch.object(config, "_avvisa_spazio_insufficiente") as avviso:
            config._annuncia_primo_avvio([("modello embedding", "~6.4 GB")])
        avviso.assert_called_once()

    def test_annuncio_senza_modelli_non_controlla(self) -> None:
        with patch.object(config, "_avvisa_spazio_insufficiente") as avviso:
            config._annuncia_primo_avvio([])
        avviso.assert_not_called()

    def test_il_controllo_riceve_le_stesse_cache_annunciate(self) -> None:
        # Se il controllo ricalcolasse da solo i modelli mancanti con i
        # percorsi di default, guarderebbe una cache diversa da quella che
        # l'annuncio sta per far scrivere: su una macchina vergine direbbe
        # "tutto gia' scaricato" e non avviserebbe mai.
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(config, "_avvisa_spazio_insufficiente") as avviso,
        ):
            emb = Path(d) / "emb"
            config._annuncia_primo_avvio(
                [("modello embedding", "~6.4 GB")],
                embedding_cache=emb,
                whisper_model="base",
                embedding_model="altro/modello",
            )
        avviso.assert_called_once_with(emb, "base", "altro/modello", ignora=False)

    def test_il_blocco_riceve_l_override_dalla_cli(self) -> None:
        """--ignora-spazio deve arrivare intatto al controllo del disco."""
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(config, "_avvisa_spazio_insufficiente") as avviso,
        ):
            config._annuncia_primo_avvio(
                [("modello embedding", "~6.4 GB")],
                embedding_cache=Path(d) / "emb",
                whisper_model="base",
                embedding_model="altro/modello",
                ignora_spazio=True,
            )
        self.assertTrue(avviso.call_args.kwargs["ignora"])


if __name__ == "__main__":
    unittest.main()
