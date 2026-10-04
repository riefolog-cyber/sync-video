#!/usr/bin/env python3
"""
Test unitari per `config._ensure_pip_packages` (bootstrap delle dipendenze).
Non tocca pip né la rete: mocka i metadata delle versioni e l'installazione.

Copre il guasto reale del 16/09/2026: numpy 1.24.3 nell'ambiente rendeva
pandas non importabile, e l'errore compariva come un guasto di pytesseract.

Esegui con: python -m unittest test_bootstrap -v
"""

import builtins
import os
import unittest
from pathlib import Path
from unittest import mock

import config


class TestReqName(unittest.TestCase):
    def test_nome_senza_versione(self):
        self.assertEqual(config._req_name("pymupdf"), "pymupdf")

    def test_nome_con_versione_minima(self):
        self.assertEqual(config._req_name("numpy>=1.26.0"), "numpy")

    def test_nome_con_trattino(self):
        self.assertEqual(config._req_name("faster-whisper>=1.2.1"), "faster-whisper")


class TestSpecOk(unittest.TestCase):
    def test_versione_sufficiente(self):
        self.assertTrue(config._spec_ok("numpy>=1.26.0", "1.26.4"))

    def test_versione_troppo_vecchia(self):
        self.assertFalse(config._spec_ok("numpy>=1.26.0", "1.24.3"))

    def test_nessuna_versione_richiesta(self):
        self.assertTrue(config._spec_ok("pymupdf", "0.0.1"))

    def test_spec_non_parseabile_non_blocca_avvio(self):
        self.assertTrue(config._spec_ok("numpy>=>1", "1.0.0"))


class TestMissingOrOld(unittest.TestCase):
    def _con_versione(self, versione):
        return mock.patch("config._installed_version", return_value=versione)

    def test_assente(self):
        with self._con_versione(None):
            self.assertEqual(config._missing_or_old("numpy>=1.26.0"), "assente")

    def test_vecchio(self):
        with self._con_versione("1.24.3"):
            self.assertEqual(config._missing_or_old("numpy>=1.26.0"), "versione 1.24.3")

    def test_a_posto(self):
        with self._con_versione("1.26.4"):
            self.assertIsNone(config._missing_or_old("numpy>=1.26.0"))


class TestImportErrorDetail(unittest.TestCase):
    def test_include_la_causa_radice(self):
        causa = ImportError("Please upgrade numpy to >= 1.26.0 to use this pandas version")
        errore = ImportError("C extension: None not built")
        errore.__cause__ = causa
        dettaglio = config._import_error_detail(errore)
        self.assertIn("C extension", dettaglio)
        self.assertIn("causa", dettaglio)
        self.assertIn("1.26.0", dettaglio)

    def test_senza_causa(self):
        self.assertEqual(
            config._import_error_detail(ImportError("solo esterno")),
            "ImportError: solo esterno",
        )


class TestEnsurePipPackages(unittest.TestCase):
    """Comportamento di `_ensure_pip_packages` con pip e metadata mockati."""

    @staticmethod
    def _versioni(numpy_vecchio=False, assente=None):
        def side_effect(pip_name):
            if pip_name == "numpy" and numpy_vecchio:
                return "1.24.3"
            if pip_name == assente:
                return None
            # Sopra ogni soglia minima dichiarata in _REQUIRED_PACKAGES
            return "99.0.0"

        return side_effect

    def test_aggiorna_numpy_vecchio(self):
        with (
            mock.patch("config._installed_version", side_effect=self._versioni(numpy_vecchio=True)),
            mock.patch("config._try_pip_install", return_value=True) as pip,
            mock.patch("config.log"),
        ):
            config._ensure_pip_packages()
        pip.assert_called_once_with("numpy>=1.26.0", upgrade=True)

    def test_installa_pacchetto_assente(self):
        with (
            mock.patch("config._installed_version", side_effect=self._versioni(assente="pymupdf")),
            mock.patch("config._try_pip_install", return_value=True) as pip,
            mock.patch("config.log"),
        ):
            config._ensure_pip_packages()
        pip.assert_called_once_with("pymupdf", upgrade=True)

    def test_niente_da_fare_se_tutto_a_posto(self):
        with (
            mock.patch("config._installed_version", side_effect=self._versioni()),
            mock.patch("config._try_pip_install") as pip,
            mock.patch("config.log"),
        ):
            config._ensure_pip_packages()
        pip.assert_not_called()

    def test_installazione_fallita_esce_con_errore(self):
        with (
            mock.patch("config._installed_version", side_effect=self._versioni(numpy_vecchio=True)),
            mock.patch("config._try_pip_install", return_value=False),
            mock.patch("config.log"),
            self.assertRaises(SystemExit),
        ):
            config._ensure_pip_packages()

    def test_import_rotto_esce_con_diagnosi_e_causa(self):
        import_reale = builtins.__import__

        def import_guardato(name, *args, **kwargs):
            if name == "pytesseract":
                causa = ImportError("Please upgrade numpy to >= 1.26.0 to use this pandas version")
                errore = ImportError("C extension: None not built")
                errore.__cause__ = causa
                raise errore
            return import_reale(name, *args, **kwargs)

        with (
            mock.patch("config._installed_version", side_effect=self._versioni()),
            mock.patch("config._try_pip_install") as pip,
            mock.patch("builtins.__import__", side_effect=import_guardato),
            mock.patch("config.log") as log,
            self.assertRaises(SystemExit),
        ):
            config._ensure_pip_packages()
        pip.assert_not_called()
        messaggi = " ".join(str(argomento) for chiamata in log.error.call_args_list for argomento in chiamata.args)
        self.assertIn("1.26.0", messaggi)
        self.assertIn("pip check", messaggi)
        self.assertIn("pytesseract", messaggi)

    def test_import_bloccato_dall_ambiente_non_esce(self):
        """MoviePy con la DLL di PyAV bloccata: pacchetto presente, ambiente no."""
        import_reale = builtins.__import__

        def import_guardato(name, *args, **kwargs):
            if name == "moviepy":
                raise ImportError("DLL load failed while importing av")
            return import_reale(name, *args, **kwargs)

        with (
            mock.patch("config._installed_version", side_effect=self._versioni()),
            mock.patch("config._try_pip_install") as pip,
            mock.patch("builtins.__import__", side_effect=import_guardato),
            mock.patch("config.log"),
        ):
            config._ensure_pip_packages()  # non deve uscire
        pip.assert_not_called()


class TestLoadEnvFile(unittest.TestCase):
    """Parser del .env: commenti, virgolette, `export`, BOM.

    Il caso che motivò il test è la virgoletta di apertura SENZA chiusura:
    il valore tornava con l'apostrofo dentro (`"ciao`) e un `_env_int`/
    `_env_float` lo rifiutava con "non numerica", cioè un avviso che
    puntava al numero invece che al .env malformato.
    """

    def _carica(self, testo: str) -> dict[str, str]:
        import os
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text(testo, encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=False):
                for k in list(os.environ):
                    if k.startswith("S2VTEST_"):
                        del os.environ[k]
                config._load_env_file(p)
                return {k: v for k, v in os.environ.items() if k.startswith("S2VTEST_")}

    def test_unclosed_quote_does_not_swallow_comment(self):
        env = self._carica('S2VTEST_A="ciao # nota\n')
        self.assertEqual(env["S2VTEST_A"], "ciao")

    def test_unclosed_quote_is_numeric_when_it_should_be(self):
        # Il caso reale: SEMANTIC_WINDOW="4 # finestre" -> non deve diventare
        # '"4', che _env_float rifiutava come "non numerica".
        env = self._carica('S2VTEST_B="4 # quattro secondi\n')
        self.assertEqual(env["S2VTEST_B"], "4")

    def test_closed_quote_with_trailing_comment(self):
        env = self._carica('S2VTEST_C="[a-z]+" # pattern\n')
        self.assertEqual(env["S2VTEST_C"], "[a-z]+")

    def test_single_quotes(self):
        env = self._carica("S2VTEST_D='ciao mondo'\n")
        self.assertEqual(env["S2VTEST_D"], "ciao mondo")

    def test_hash_inside_closed_quotes_is_kept(self):
        env = self._carica('S2VTEST_E="a#b"\n')
        self.assertEqual(env["S2VTEST_E"], "a#b")

    def test_unquoted_value_stops_at_hash(self):
        env = self._carica("S2VTEST_F=ab # nota\n")
        self.assertEqual(env["S2VTEST_F"], "ab")

    def test_export_prefix_and_comments(self):
        env = self._carica("# commento\nexport S2VTEST_G=7\n\n")
        self.assertEqual(env["S2VTEST_G"], "7")

    def test_line_without_equals_is_ignored(self):
        env = self._carica("S2VTEST_H\n")
        self.assertEqual(env, {})

    def test_existing_env_is_not_overwritten(self):
        import os
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("S2VTEST_I=da_env_file\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"S2VTEST_I": "gia_impostata"}):
                config._load_env_file(p)
                self.assertEqual(os.environ["S2VTEST_I"], "gia_impostata")


class TestAnnunciaPrimoAvvio(unittest.TestCase):
    """Il primo avvio scarica qualche GB: dirlo PRIMA, non durante.

    Il silenzio di un download da gigabyte è indistinguibile da un blocco, e la
    cosa più probabile a quel punto è chiudere il programma. Il caso da
    coprire è anche l'opposto: quando i modelli ci sono già l'annuncio non deve
    ripetersi, altrimenti è rumore che impedisce di leggere il resto.
    """

    def test_cache_vuota_annuncia(self):
        import tempfile
        from pathlib import Path

        from config import _modelli_mancanti

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # fastembed crea la cartella PRIMA di scaricare: una cartella che
            # esiste ma è vuota non significa "già scaricato".
            (root / "embedding_model").mkdir()
            mancanti = _modelli_mancanti(root / "embedding_model", "small")
            self.assertTrue(mancanti)
            self.assertIn("embedding", mancanti[0][0])

    def test_cache_piena_non_annuncia_nulla(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        from config import _modelli_mancanti

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "embedding_model"
            cache.mkdir()
            (cache / "modello.onnx").write_bytes(b"x" * 32)
            # Anche whisper "in cache": con i due presenti non deve stampare.
            with mock.patch("config._cache_whisper_faster") as hf:
                hf.return_value = cache
                self.assertEqual(_modelli_mancanti(cache, "small"), [])

    def test_una_cartella_di_zero_byte_non_conta(self):
        # Un file da 0 byte è il segnale di un download interrotto: contarlo
        # come "scaricato" riporterebbe il problema al primo vero avvio utile.
        import tempfile
        from pathlib import Path

        from config import _modelli_mancanti

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "m"
            cache.mkdir()
            (cache / "vuoto.bin").write_bytes(b"")
            self.assertTrue(_modelli_mancanti(cache, "small"))

    def test_il_modello_whisper_annunciato_e_quello_usato(self):
        # faster-whisper scarica dalla cache HUB, non in .cache/: annunciare la
        # directory sbagliata promette un download che non parte e nasconde
        # quello che parte davvero.
        import tempfile
        from pathlib import Path
        from unittest import mock

        from config import _modelli_mancanti

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "embedding_model"
            cache.mkdir()
            (cache / "m.onnx").write_bytes(b"x")
            with mock.patch("config._cache_whisper_faster") as hf:
                hf.return_value = Path(td) / "non_esiste"
                mancanti = _modelli_mancanti(cache, "small")
            nomi = " ".join(n for n, _ in mancanti)
            self.assertIn("whisper small", nomi)
            self.assertNotIn("openvino", nomi.lower())

    def test_la_cache_whisper_e_indicata_con_il_nome_del_modello(self):
        # faster-whisper indicizza i modelli come Systran/faster-whisper-<taglia>,
        # quindi con WHISPER_MODEL=base va cercato -base e non -small.
        with mock.patch("config._cache_hub_huggingface") as hub:
            hub.return_value = Path("/hub")
            d = config._cache_whisper_faster("base")
        self.assertEqual(d, Path("/hub/models--Systran--faster-whisper-base"))

    def test_la_cache_whisper_su_HF_HUB_CACHE_non_su_HF_HOME(self):
        # HF_HUB_CACHE ha precedenza su HF_HOME nella libreria: guardando solo
        # HF_HOME/hub, un utente con HF_HUB_CACHE riceverebbe l'annuncio del
        # primo avvio a ogni run perche' la cache vera e' altrove.
        with mock.patch("huggingface_hub.constants.HF_HUB_CACHE", str(Path("/custom/hub"))):
            self.assertEqual(config._cache_hub_huggingface(), Path("/custom/hub"))

    def test_cache_hub_se_huggingface_hub_non_c_e_in_importabile(self):
        # La libreria onora HF_HOME aggiungendo /hub: senza di lei la riserva
        # deve ricostruire la stessa catena.
        vuoto_hub = mock.patch("huggingface_hub.constants.HF_HUB_CACHE", "")
        with mock.patch.dict(os.environ, {"HF_HOME": str(Path("/home/hf"))}, clear=False), vuoto_hub:
            self.assertEqual(config._cache_hub_huggingface(), Path("/home/hf/hub"))

    def test_cache_hub_da_HF_HUB_cache_senza_la_libreria(self):
        # HF_HUB_CACHE e' la cache GIA' (non un parent come HF_HOME): aggiungere
        # /hub darebbe un percorso inesistente e l'annuncio ripeterebbe ogni run.
        with mock.patch.dict(os.environ, {"HF_HUB_CACHE": str(Path("/direct/hub"))},
                             clear=False), mock.patch("huggingface_hub.constants.HF_HUB_CACHE", ""):
            self.assertEqual(config._cache_hub_huggingface(), Path("/direct/hub"))

    def test_modello_whisper_non_standard_non_produce_annuncio(self):
        # Un repo custom o un percorso locale non ha una cartella prevedibile:
        # controllare una cartella che non verra' mai scritta significa
        # annunciare il download a ogni run, per sempre.
        self.assertIsNone(config._cache_whisper_faster("deepdml/faster-whisper-large-v3"))
        self.assertIsNone(config._cache_whisper_faster(r"C:\modelli\mio"))
        self.assertIsNone(config._cache_whisper_faster(""))

    def test_modello_whisper_custom_non_rompe_l_annuncio_embedding(self):
        with mock.patch("config._gia_scaricato", return_value=False):
            nomi = [n for n, _ in config._modelli_mancanti(Path("/tmp/e"),
                                                          "deepdml/qualcosa")]
        self.assertEqual(len(nomi), 1)
        self.assertIn("embedding", nomi[0])

    def test_dimensione_embedding_solo_per_il_modello_misurato(self):
        # Con --semantic-model diverso la cache occupa un altro spazio: la
        # dimensione del default sarebbe una cifra falsa.
        with mock.patch("config._gia_scaricato", return_value=False):
            default = dict(config._modelli_mancanti(
                Path("/tmp/e"), "small", "intfloat/multilingual-e5-large"))
            altro = dict(config._modelli_mancanti(
                Path("/tmp/e"), "small", "BAAI/bge-m3"))
        self.assertEqual(default["modello embedding (intfloat/multilingual-e5-large)"],
                         "~6.4 GB")
        # Il nome riportato e' quello configurato, non quello del default.
        self.assertEqual(altro["modello embedding (BAAI/bge-m3)"], "")

    def test_il_nome_del_modello_embedding_segue_la_configurazione(self):
        with mock.patch("config._gia_scaricato", return_value=False):
            nomi = [n for n, _ in config._modelli_mancanti(
                Path("/tmp/e"), "small", "BAAI/bge-m3")]
        self.assertIn("BAAI/bge-m3", nomi[0])

    def test_dimensione_whisper_solo_se_misurata(self):
        # Per un modello senza misura (medium/large) si nomma il modello senza
        # inventare una cifra: e' l'unica cosa che fa capire quanto manca.
        from pathlib import Path

        with mock.patch("config._gia_scaricato", return_value=False):
            grandi = dict(config._modelli_mancanti(Path("/tmp/embedding"), "large"))
            piccoli = dict(config._modelli_mancanti(Path("/tmp/embedding"), "small"))
        self.assertEqual(grandi["modello di trascrizione (whisper large)"], "")
        self.assertEqual(piccoli["modello di trascrizione (whisper small)"], "~490 MB")

    def test_il_messaggio_dice_che_non_e_un_blocco(self):
        import io
        import logging

        import config

        buf = io.StringIO()
        precedente = config.log.handlers[:]
        config.log.handlers = [logging.StreamHandler(buf)]
        try:
            config._annuncia_primo_avvio([("modello embedding", "~6.4 GB")])
        finally:
            config.log.handlers = precedente
        testo = buf.getvalue()
        self.assertIn("PRIMO AVVIO", testo)
        self.assertIn("NON", testo)
        self.assertIn("6.4 GB", testo)

    def test_il_messaggio_non_inventa_una_dimensione(self):
        import io
        import logging

        import config

        buf = io.StringIO()
        precedente = config.log.handlers[:]
        config.log.handlers = [logging.StreamHandler(buf)]
        try:
            config._annuncia_primo_avvio([("modello whisper large", "")])
        finally:
            config.log.handlers = precedente
        testo = buf.getvalue()
        self.assertIn("modello whisper large", testo)
        self.assertNotIn("()", testo)

    def test_nessun_modello_mancante_non_stampa(self):
        import io
        import logging

        import config

        buf = io.StringIO()
        precedente = config.log.handlers[:]
        config.log.handlers = [logging.StreamHandler(buf)]
        try:
            config._annuncia_primo_avvio([])
        finally:
            config.log.handlers = precedente
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
