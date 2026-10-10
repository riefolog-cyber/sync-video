#!/usr/bin/env python3
"""Test del modulo pipeline_cache (P2 #9).

Esegui con: python -m unittest test_pipeline_cache -v

Copre le tre funzioni che l'estrazione da main.py aveva lasciate senza
test diretto: ``save_final_timeline``, ``save_sync_report`` e la policy
``KEEP_CACHE_STEMS``. Ogni test passa una ``cache_dir`` esplicita: nessun
file finisce nella cache reale del progetto.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import config
from pipeline_cache import (
    KEEP_CACHE_STEMS,
    cache_path,
    clean_orphan_cache,
    clean_stale_llm_cache,
    file_hash,
    load_cache,
    save_cache,
    save_final_timeline,
    save_sync_report,
)


class _CacheTemp(unittest.TestCase):
    """Base: cartella temporanea pulita, senza toccare la cache reale."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)


class TestSaveFinalTimeline(_CacheTemp):
    def test_scrive_slide_start_end_ordinati(self) -> None:
        # Timeline disordinata in ingresso: l'output deve essere ordinato
        # per slide, con `end` = start della successiva.
        timeline = {3: 20.0, 1: 0.0, 2: 10.0}
        save_final_timeline(timeline, total_duration=30.0, cache_dir=self.base)

        entries = json.loads((self.base / "llm_timeline_finale.json").read_text(encoding="utf-8"))
        self.assertEqual([e["slide"] for e in entries], [1, 2, 3])
        self.assertEqual(entries[0], {"slide": 1, "start": 0.0, "end": 10.0})
        self.assertEqual(entries[1], {"slide": 2, "start": 10.0, "end": 20.0})

    def test_ultima_usa_total_duration(self) -> None:
        # L'ultima slide non ha una successiva: deve chiudere a total_duration,
        # non lasciare `end` mancante o infinity.
        save_final_timeline({1: 5.0, 2: 9.0}, total_duration=42.5, cache_dir=self.base)

        entries = json.loads((self.base / "llm_timeline_finale.json").read_text(encoding="utf-8"))
        self.assertEqual(entries[-1]["end"], 42.5)

    def test_timeline_vuota_non_rompe(self) -> None:
        save_final_timeline({}, total_duration=10.0, cache_dir=self.base)
        self.assertEqual(json.loads((self.base / "llm_timeline_finale.json").read_text(encoding="utf-8")), [])

    def test_arrotonda_a_3_cifre(self) -> None:
        # analysis_sync.py legge questo file: la precisione deve essere
        # sufficiente e non mostrare float residui.
        save_final_timeline({1: 0.123456}, total_duration=1.0, cache_dir=self.base)
        entries = json.loads((self.base / "llm_timeline_finale.json").read_text(encoding="utf-8"))
        self.assertEqual(entries[0]["start"], 0.123)


class TestSaveSyncReport(_CacheTemp):
    def test_scrive_json_aggiornabile(self) -> None:
        save_sync_report({"run": 1, "errori": []}, cache_dir=self.base)
        report = self.base / "sync_report.json"
        self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["run"], 1)

        # Seconda run: deve sovrascrivere, non accodare.
        save_sync_report({"run": 2}, cache_dir=self.base)
        self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["run"], 2)

    def test_crea_la_cartella_se_mancante(self) -> None:
        profonda = self.base / "a" / "b"
        save_sync_report({"ok": True}, cache_dir=profonda)
        self.assertTrue((profonda / "sync_report.json").exists())

    def test_errore_scrittura_non_pha_esplodere(self) -> None:
        # sync_report.json è diagnostico: un file non scrivibile (cache in
        # sola lettura, disco pieno) non deve mai fermare la pipeline.
        occupato = self.base / "sync_report.json"
        occupato.mkdir()  # una cartella dove dovrebbe stare un file: IsADirectoryError
        save_sync_report({"run": 3}, cache_dir=self.base)  # non deve sollevare


class TestKeepCacheStems(_CacheTemp):
    def test_housekeeping_equivalente_a_cache_maintenance(self) -> None:
        # Due moduli elencano le chiavi housekeeping. Se divergono, la
        # pulizia --clean-cache (cache_maintenance.KEEP_STEMS) e quella
        # durante la run (pipeline_cache.KEEP_CACHE_STEMS) salvano cose
        # diverse: il test fissa la lista corretta e la scopre al primo slide.
        from cache_maintenance import KEEP_STEMS

        self.assertEqual(
            KEEP_CACHE_STEMS,
            frozenset({"machine_setup", "updates_check", "fastembed_ab", "sync_report"}),
        )
        self.assertEqual(KEEP_STEMS, KEEP_CACHE_STEMS)

    def test_chiavi_housekeeping_sono_protette(self) -> None:
        for nome in KEEP_CACHE_STEMS:
            (self.base / f"{nome}.json").write_text("{}", encoding="utf-8")
        (self.base / "orfano.json").write_text("{}", encoding="utf-8")

        rimossi = clean_orphan_cache(set(), cache_dir=self.base)

        self.assertEqual(rimossi, 1)
        for nome in KEEP_CACHE_STEMS:
            self.assertTrue((self.base / f"{nome}.json").exists(), f"{nome} non doveva sparire")


class TestCleanOrphanCache(_CacheTemp):
    def test_rimuove_solo_le_chiavi_non_attive(self) -> None:
        for nome in ("slide1", "slide2", "orfano"):
            (self.base / f"{nome}.json").write_text("{}", encoding="utf-8")

        rimossi = clean_orphan_cache({"slide1", "slide2"}, cache_dir=self.base)

        self.assertEqual(rimossi, 1)
        self.assertTrue((self.base / "slide1.json").exists())
        self.assertFalse((self.base / "orfano.json").exists())

    def test_mai_tocca_gli_llm(self) -> None:
        # Gli llm_*.json sono cache di contenuto del LLM: la loro pulizia
        # è un compito separato (clean_stale_llm_cache).
        (self.base / "llm_abc123.json").write_text("{}", encoding="utf-8")
        self.assertEqual(clean_orphan_cache(set(), cache_dir=self.base), 0)
        self.assertTrue((self.base / "llm_abc123.json").exists())

    def test_cartella_mancante_ritorna_zero(self) -> None:
        self.assertEqual(clean_orphan_cache(set(), cache_dir=self.base / "non-esiste"), 0)


class TestCleanStaleLlmCache(_CacheTemp):
    def test_rimuove_llm_non_usati(self) -> None:
        for nome in ("llm_usato", "llm_vecchio"):
            (self.base / f"{nome}.json").write_text("{}", encoding="utf-8")

        rimossi = clean_stale_llm_cache({"llm_usato"}, cache_dir=self.base)

        self.assertEqual(rimossi, 1)
        self.assertTrue((self.base / "llm_usato.json").exists())
        self.assertFalse((self.base / "llm_vecchio.json").exists())

    def test_non_tocca_gli_llm_review(self) -> None:
        # llm_review_* è la cache della revisione: sopravvive alla pulizia
        # come le chiavi housekeeping sopravvivono a clean_orphan_cache.
        from llm_sync import LLM_REVIEW_CACHE_PREFIX

        recensione = f"{LLM_REVIEW_CACHE_PREFIX}xyz"
        (self.base / f"{recensione}.json").write_text("{}", encoding="utf-8")

        self.assertEqual(clean_stale_llm_cache(set(), cache_dir=self.base), 0)
        self.assertTrue((self.base / f"{recensione}.json").exists())

    def test_ignora_i_non_llm(self) -> None:
        (self.base / "slide1.json").write_text("{}", encoding="utf-8")
        self.assertEqual(clean_stale_llm_cache(set(), cache_dir=self.base), 0)
        self.assertTrue((self.base / "slide1.json").exists())


class TestCachePath(_CacheTemp):
    def test_crea_la_cartella(self) -> None:
        profonda = self.base / "nuova"
        percorso = cache_path("chiave", profonda)
        self.assertTrue(profonda.is_dir())
        self.assertEqual(percorso.name, "chiave.json")

    def test_cache_default_usa_la_cache_di_progetto(self) -> None:
        # Il default (config.CACHE_DIR) e' gia' creato e popolato dal progetto:
        # il test non lo tocca, verifica solo che il default produca un percorso
        # coerente senza scriverci dentro.
        self.assertEqual(cache_path("chiave-di-prova").parent, Path(config.CACHE_DIR))


class TestLoadSaveCache(_CacheTemp):
    def test_round_trip(self) -> None:
        save_cache("chiave", {"a": 1, "b": [2, 3]}, cache_dir=self.base)
        self.assertEqual(load_cache("chiave", cache_dir=self.base), {"a": 1, "b": [2, 3]})

    def test_mancante_ritorna_none(self) -> None:
        self.assertIsNone(load_cache("inesistente", cache_dir=self.base))

    def test_json_corrotto_ritorna_none(self) -> None:
        # Una cache corrotta (crash a metà scrittura) non deve propagare
        # l'eccezione: si ricomputa.
        (self.base / "rotta.json").write_text("{non è json", encoding="utf-8")
        self.assertIsNone(load_cache("rotta", cache_dir=self.base))

    def test_json_non_dizionario_ritorna_none(self) -> None:
        (self.base / "lista.json").write_text("[1, 2, 3]", encoding="utf-8")
        self.assertIsNone(load_cache("lista", cache_dir=self.base))


class TestFileHash(_CacheTemp):
    def test_stabile_e_dipende_dai_contenuti(self) -> None:
        a, b = self.base / "a.bin", self.base / "b.bin"
        a.write_bytes(b"stesso contenuto")
        b.write_bytes(b"stesso contenuto")
        c = self.base / "c.bin"
        c.write_bytes(b"diverso")

        self.assertEqual(file_hash(a), file_hash(b))
        self.assertNotEqual(file_hash(a), file_hash(c))

    def test_file_vuoto(self) -> None:
        vuoto = self.base / "vuoto.bin"
        vuoto.write_bytes(b"")
        self.assertEqual(len(file_hash(vuoto)), 32)  # md5 hex


class TestShimMainCompatibile(_CacheTemp):
    """I test esistenti importano da `main`: la compatibilità va tenuta."""

    def test_main_riesporta_i_nomi(self) -> None:
        import main

        for nome in (
            "_file_hash",
            "_load_cache",
            "_save_cache",
            "_cache_path",
            "_clean_orphan_cache",
            "_clean_stale_llm_cache",
            "_save_final_timeline",
            "_save_sync_report",
            "_format_time",
            "_print_timing",
            "_append_timing_history",
            "_slide_list_text",
        ):
            self.assertTrue(hasattr(main, nome), f"main.{nome} mancante: i test lo importano")


if __name__ == "__main__":
    unittest.main()
