#!/usr/bin/env python3
"""
Test unitari per `check_embedding_models` (monitoraggio regola modello e5).
Non tocca la rete: usa dati finti per verificare logica di baseline,
raccomandazione ed esito.

Esegui con: python -m unittest test_check_embedding_models -v
"""

import unittest
from unittest import mock

import check_embedding_models as cm


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


if __name__ == "__main__":
    unittest.main()
