#!/usr/bin/env python3
"""
Test unitari per `check_embedding_models` (monitoraggio regola modello e5).
Non tocca la rete: usa dati finti per verificare logica di baseline,
raccomandazione ed esito.

Esegui con: python -m unittest test_check_embedding_models -v
"""

import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

# Il modulo e' in scripts/ (non e' un pacchetto: si esegue come script), quindi
# la sua cartella va aggiunta al sys.path per poterlo importare da qui.
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

import check_embedding_models as cm

_DT = datetime(2026, 4, 2, tzinfo=timezone.utc)
_CANDIDATO = "foo/multilingual-e5-onnx"


class _FakeModelInfo:
    def __init__(self, lastModified=None, downloads=None):
        self.lastModified = lastModified
        self.downloads = downloads


class _FakeLiteModel:
    def __init__(self, model_id, lastModified=None, downloads=None, tags=None):
        self.modelId = model_id
        self.lastModified = lastModified
        self.downloads = downloads
        self.tags = tags or []


class TestModelUpdated(unittest.TestCase):
    def test_latest_none(self):
        self.assertFalse(cm._model_updated(None, "2026-01-01T00:00:00+00:00"))

    def test_no_baseline_means_updated(self):
        self.assertTrue(cm._model_updated("2026-04-02T00:00:00+00:00", None))

    def test_same_date_not_updated(self):
        self.assertFalse(
            cm._model_updated(
                "2026-04-02T00:00:00+00:00",
                "2026-04-02T00:00:00+00:00",
            )
        )

    def test_newer_date_updated(self):
        self.assertTrue(
            cm._model_updated(
                "2026-04-02T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
        )

    def test_invalid_date_falls_back_to_string_compare(self):
        self.assertTrue(cm._model_updated("x", "y"))


class TestRecommendation(unittest.TestCase):
    def _data(self, pref_last="2026-04-02T00:00:00+00:00", candidates=None):
        tracked = {
            cm.PREFERRED_MODEL: {"lastModified": pref_last, "downloads": 1},
            "intfloat/multilingual-e5-base": {"lastModified": "2026-04-02T00:00:00+00:00", "downloads": 2},
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": {
                "lastModified": "2026-01-28T00:00:00+00:00",
                "downloads": 3,
            },
        }
        return {"tracked": tracked, "new_candidates": candidates or {}}

    def test_first_run_no_action(self):
        action, reasons = cm._recommendation(None, self._data())
        self.assertFalse(action)
        self.assertTrue(any("Prima esecuzione" in r for r in reasons))

    def test_unchanged_no_action(self):
        prev = {
            "tracked": {
                "intfloat/multilingual-e5-large": {"lastModified": "2026-04-02T00:00:00+00:00"},
                "intfloat/multilingual-e5-base": {"lastModified": "2026-04-02T00:00:00+00:00"},
                "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": {
                    "lastModified": "2026-01-28T00:00:00+00:00"
                },
            }
        }
        action, reasons = cm._recommendation(prev, self._data())
        self.assertFalse(action)
        self.assertEqual(reasons, [])

    def test_preferred_updated_triggers_action(self):
        prev = {"tracked": {"intfloat/multilingual-e5-large": {"lastModified": "2026-01-01T00:00:00+00:00"}}}
        action, reasons = cm._recommendation(prev, self._data())
        self.assertTrue(action)
        self.assertTrue(any(cm.PREFERRED_MODEL in r for r in reasons))

    def test_new_candidate_triggers_action(self):
        prev = {"tracked": {"intfloat/multilingual-e5-large": {"lastModified": "2026-04-02T00:00:00+00:00"}}}
        data = self._data(candidates={"foo/multilingual-e5-onnx": {"lastModified": "2026-08-01T00:00:00+00:00"}})
        action, reasons = cm._recommendation(prev, data)
        self.assertTrue(action)
        self.assertTrue(any("foo/multilingual-e5-onnx" in r for r in reasons))

    def test_alternate_updated_triggers_action(self):
        prev = {
            "tracked": {
                "intfloat/multilingual-e5-large": {"lastModified": "2026-04-02T00:00:00+00:00"},
                "intfloat/multilingual-e5-base": {"lastModified": "2026-01-01T00:00:00+00:00"},
            }
        }
        action, _ = cm._recommendation(prev, self._data())
        self.assertTrue(action)


class TestGather(unittest.TestCase):
    def test_filters_only_onnx_multilingual(self):
        api = mock.Mock()
        api.model_info.side_effect = lambda mid: _FakeModelInfo(downloads=10)
        api.list_models.return_value = [
            _FakeLiteModel("foo/multilingual-e5-onnx", tags=["onnx", "multilingual"]),
            _FakeLiteModel("bar/german-only", tags=["onnx"]),
            _FakeLiteModel("baz/multilingual-e5", tags=["multilingual"]),
            _FakeLiteModel("intfloat/multilingual-e5-large", tags=["onnx", "multilingual"]),
        ]
        data = cm._gather(api)
        self.assertEqual(list(data["new_candidates"]), ["foo/multilingual-e5-onnx"])

    def test_tutti_i_modelli_in_rete_raises(self):
        api = mock.Mock()
        api.model_info.side_effect = OSError("connection unreachable")
        api.list_models.side_effect = OSError("connection unreachable")
        with self.assertRaises(cm.NetworkError):
            cm._gather(api)

    def test_ricerca_candidati_offline_raises(self):
        api = mock.Mock()
        api.model_info.side_effect = lambda mid: _FakeModelInfo(lastModified=_DT, downloads=1)
        api.list_models.side_effect = OSError("connection unreachable")
        with self.assertRaises(cm.NetworkError):
            cm._gather(api)

    def test_singolo_404_non_e_errore_di_rete(self):
        api = mock.Mock()

        def info(mid):
            if mid == cm.PREFERRED_MODEL:
                raise RuntimeError("404 Not Found")
            return _FakeModelInfo(lastModified=_DT, downloads=1)

        api.model_info.side_effect = info
        api.list_models.return_value = []
        data = cm._gather(api)
        self.assertIn(cm.PREFERRED_MODEL, data["tracked"])


class TestKnownModels(unittest.TestCase):
    @staticmethod
    def _data(candidates=None):
        return {
            "tracked": {cm.PREFERRED_MODEL: {"lastModified": "2026-04-02T00:00:00+00:00", "downloads": 1}},
            "new_candidates": candidates or {},
        }

    def test_candidato_gia_noto_non_scatta(self):
        # La baseline del modello preferito è invariata: l'unico segnale
        # sarebbe il candidato, che però è già stato visto.
        prev = {
            "tracked": {cm.PREFERRED_MODEL: {"lastModified": "2026-04-02T00:00:00+00:00"}},
            cm.KNOWN_MODELS_KEY: [_CANDIDATO],
        }
        data = self._data({_CANDIDATO: {"lastModified": "2026-08-01T00:00:00+00:00"}})
        action, reasons = cm._recommendation(prev, data)
        self.assertFalse(action)
        self.assertFalse(any("Nuovo candidato" in r for r in reasons))

    def test_gather_filtra_modelli_gia_visti(self):
        api = mock.Mock()
        api.model_info.side_effect = lambda mid: _FakeModelInfo(lastModified=_DT, downloads=1)
        api.list_models.return_value = [_FakeLiteModel(_CANDIDATO, tags=["onnx", "multilingual"])]
        data = cm._gather(api, known=set(cm.KNOWN_MODELS) | {_CANDIDATO})
        self.assertEqual(data["new_candidates"], {})


class TestNetworkExitCode(unittest.TestCase):
    def test_main_errore_di_rete_ritorna_3(self):
        fake_hf = types.SimpleNamespace(HfApi=mock.Mock(return_value=mock.Mock()))
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.dict(sys.modules, {"huggingface_hub": fake_hf}), \
             mock.patch.object(cm, "_check_network"), \
             mock.patch.object(cm, "_gather", side_effect=cm.NetworkError("offline")), \
             mock.patch.object(cm, "_write_report") as write_report, \
             mock.patch.object(cm, "_save_state") as save_state, \
             contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = cm.main([])
        self.assertEqual(rc, 3)
        self.assertIn("non verificabile", err.getvalue())
        self.assertIn("NON VERIFICABILE", out.getvalue())
        # Uno stato vuoto non va scritto: la rete mancante non è "nessuna azione".
        write_report.assert_not_called()
        save_state.assert_not_called()


class TestStatePersistence(unittest.TestCase):
    @staticmethod
    def _fake_hf():
        api = mock.Mock()
        api.model_info.side_effect = lambda mid: _FakeModelInfo(lastModified=_DT, downloads=1)
        api.list_models.return_value = [_FakeLiteModel(_CANDIDATO, tags=["onnx", "multilingual"])]
        return types.SimpleNamespace(HfApi=mock.Mock(return_value=api))

    def test_secondo_run_non_ripete_il_candidato(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            state, report = p / "state.json", p / "report.md"
            with mock.patch.object(cm, "CACHE_DIR", p), \
                 mock.patch.object(cm, "STATE_FILE", state), \
                 mock.patch.object(cm, "REPORT_FILE", report), \
                 mock.patch.dict(sys.modules, {"huggingface_hub": self._fake_hf()}), \
                 contextlib.redirect_stdout(io.StringIO()):
                rc1 = cm.main([])
                primo = report.read_text(encoding="utf-8")
                salvato = json.loads(state.read_text(encoding="utf-8"))
                rc2 = cm.main([])
                secondo = report.read_text(encoding="utf-8")
        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)
        self.assertIn(_CANDIDATO, primo)
        self.assertIn(_CANDIDATO, salvato[cm.KNOWN_MODELS_KEY])
        self.assertIn("Nessun nuovo candidato.", secondo)
        self.assertNotIn(f"Nuovo candidato rilevato: {_CANDIDATO}", secondo)


class TestAtomicWrite(unittest.TestCase):
    def test_stato_e_report_usano_atomic_write(self):
        with mock.patch.object(cm, "_atomic_write") as atomic:
            cm._save_state({"tracked": {}}, known=set())
            cm._write_report(False, [], {"tracked": {}, "new_candidates": {}})
        self.assertEqual(atomic.call_count, 2)
        self.assertEqual(atomic.call_args_list[0].args[0], cm.STATE_FILE)
        self.assertEqual(atomic.call_args_list[1].args[0], cm.REPORT_FILE)

    def test_atomic_write_delega_config(self):
        import config

        with mock.patch.object(config, "atomic_write_text") as write:
            cm._atomic_write(Path("x"), "testo")
        write.assert_called_once_with(Path("x"), "testo")


if __name__ == "__main__":
    unittest.main()
