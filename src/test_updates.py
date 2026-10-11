#!/usr/bin/env python3
"""
Test unitari per `updates` (controllo aggiornamenti pacchetti).
Non tocca la rete: mocka PyPI e la cache.

Esegui con: python -m unittest test_updates -v
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import updates


class _TempCacheMixin:
    """Reindirizza UPDATES_CACHE su un file temporaneo per i test."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp_dir.name) / "updates_check.json"
        patcher = mock.patch("updates.UPDATES_CACHE", self.cache_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp_dir.cleanup)


class TestInstalledVersion(unittest.TestCase):
    def test_known_package(self):
        self.assertIsInstance(updates._installed_version("numpy"), str)

    def test_missing_package_returns_none(self):
        self.assertIsNone(updates._installed_version("pacchetto-inesistente-xyz"))


class TestPinned(unittest.TestCase):
    def test_fastembed_pinned(self):
        self.assertTrue(updates._is_pinned("fastembed"))

    def test_openvino_not_pinned(self):
        self.assertFalse(updates._is_pinned("openvino"))


class TestInstalledConstraints(unittest.TestCase):
    """La scansione delle dipendenze installate, con metadata finta.

    E' la parte che permette di dire "questo aggiornamento non esiste": PyPI
    dice cosa e' pubblicato, la metadata installata dice cosa regge insieme.
    """

    class _Dist:
        """Distribuzione finta: i tre soli attributi che la scansione legge."""

        def __init__(self, name, version, requires):
            self.metadata = {"Name": name}
            self.version = version
            self.requires = requires

    def _scan(self, dists):
        with mock.patch("updates.importlib.metadata.distributions", return_value=dists):
            return updates._installed_constraints()

    def test_raccoglie_gli_specifier(self):
        vincoli = self._scan([self._Dist("fastembed", "0.5.1", ["pillow (>=10.3.0,<11.0.0)"])])
        self.assertEqual(vincoli["pillow"], [("fastembed", "0.5.1", "<11.0.0,>=10.3.0")])

    def test_salta_i_requisiti_senza_versione(self):
        # "pillow" senza specifier non vieta niente: registrarlo bloccherebbe
        # qualunque candidata.
        self.assertEqual(self._scan([self._Dist("x", "1.0", ["pillow"])]), {})

    def test_salta_i_marker_che_non_valgono_qui(self):
        # Un requisito valido solo su un'altra piattaforma non deve contare:
        # il blocco dipenderebbe dall'ambiente sbagliato.
        self.assertEqual(self._scan([self._Dist("x", "1.0", ['pillow<11; python_version < "2.0"'])]), {})

    def test_metadata_illeggibile_non_solleva(self):
        class Rotta:
            @property
            def metadata(self):
                raise RuntimeError("metadata illeggibile")

            version = "1.0"
            requires: tuple[str, ...] = ()

        self.assertEqual(self._scan([Rotta()]), {})


class TestBlockers(unittest.TestCase):
    """`_blockers`: chi vieta la versione candidata, secondo quello che e' installato.

    Caso reale misurato il 2026-10-10: Pillow 12 esiste su PyPI, ma fastembed
    0.5.1 dichiara `pillow<11.0.0` e moviepy 2.2.1 `pillow<12.0`. Il report
    annunciava "pillow 10.4.0 -> 12.3.0 — major version" e invitava a
    `pip install -U pillow`, che pero' resta su 10.x (il resolver non sale oltre
    il tetto) e, con la versione fissata a mano, fallisce con ResolutionImpossible.
    """

    @staticmethod
    def _vincoli():
        """Vincoli come li restituisce `_installed_constraints` (finti, ma della forma vera)."""
        return {
            "pillow": [
                ("fastembed", "0.5.1", "<11.0.0,>=10.3.0"),
                ("moviepy", "2.2.1", "<12.0,>=9.2.0"),
            ],
            "numpy": [("pandas", "2.2.0", ">=1.22.4")],
        }

    def test_versione_vietata_dalle_dipendenze(self):
        motivi = updates._blockers("pillow", "12.3.0", self._vincoli())
        self.assertEqual(len(motivi), 2)
        self.assertIn("fastembed 0.5.1 impone <11.0.0,>=10.3.0", motivi)

    def test_versione_ammessa_non_e_bloccata(self):
        # 10.4.0 soddisfa entrambi gli specifier: e' il motivo per cui pip ci sta,
        # e per cui "-> 12.3.0" non era un'azione possibile.
        self.assertEqual(updates._blockers("pillow", "10.4.0", self._vincoli()), [])

    def test_specifier_minimo_vieta_le_versioni_vecchie(self):
        self.assertEqual(updates._blockers("numpy", "1.0.0", self._vincoli()), ["pandas 2.2.0 impone >=1.22.4"])

    def test_pacchetto_senza_vincoli(self):
        self.assertEqual(updates._blockers("tqdm", "99.0", self._vincoli()), [])

    def test_un_pacchetto_non_blocca_se_stesso(self):
        self.assertEqual(
            updates._blockers("pillow", "12.0.0", {"pillow": [("pillow", "10.4.0", "<11")]}),
            [],
        )

    def test_versione_illeggibile_non_accusa_nessuno(self):
        # Meglio tacere (voce normale) che inventare un blocco.
        self.assertEqual(updates._blockers("pillow", "non-una-versione", self._vincoli()), [])

    def test_nome_normalizzato(self):
        self.assertEqual(len(updates._blockers("Pillow", "12.3.0", self._vincoli())), 2)


class TestCheckUpdates(_TempCacheMixin, unittest.TestCase):
    def test_fresh_cache_skips_network(self):
        self.cache_path.write_text(
            json.dumps({"ts": 1e15, "outdated": [{"name": "x", "installed": "1", "latest": "2"}]}),
            encoding="utf-8",
        )
        with mock.patch("updates._latest_version_pypi") as net:
            result = updates.check_updates(ttl_hours=6)
            net.assert_not_called()
        self.assertEqual(len(result), 1)

    def test_expired_cache_queries_network(self):
        self.cache_path.write_text(json.dumps({"ts": 0, "outdated": []}), encoding="utf-8")
        with mock.patch(
            "updates._latest_version_pypi",
            side_effect=lambda p: {"numpy": "9.9.9", "tqdm": "1.0.0"}.get(p),
        ), mock.patch("updates._installed_version", side_effect=lambda p: {"numpy": "1.0.0"}.get(p)):
            result = updates.check_updates(ttl_hours=6)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "numpy")
        self.assertEqual(result[0]["latest"], "9.9.9")

    def test_network_down_is_not_cached_as_all_updated(self):
        """PyPI irraggiungibile non può diventare "tutti aggiornati" in cache.

        Prima: ogni pacchetto dava None, la lista restava vuota, la cache
        veniva scritta comunque e la run successiva (entro TTL) stampava
        "tutti aggiornati" — per 6 ore, indistinguibile dal caso in cui la rete
        funziona davvero. Ora la cache non viene scritta e l'avviso dice
        perché non è stato possibile verificare.
        """
        self.cache_path.write_text(json.dumps({"ts": 0, "outdated": []}), encoding="utf-8")
        prima = self.cache_path.read_text(encoding="utf-8")
        with mock.patch("updates._latest_version_pypi", return_value=None), mock.patch(
            "updates._installed_version", return_value="1.0.0"
        ):
            result = updates.check_updates(ttl_hours=6)
        self.assertEqual(result, [])
        # la cache NON deve essere stata riscritta: il TTL non consuma il check,
        # quindi la run successiva ritenta invece di fidarsi di un "tutto
        # aggiornato" che non e' mai stato verificato
        self.assertEqual(self.cache_path.read_text(encoding="utf-8"), prima)

    def test_partial_network_failure_still_reports_real_updates(self):
        # Se qualcuno risponde, il report e' utilizzabile: i pacchetti senza
        # risposta vengono solo scartati (con un debug), non tuteliamo tutto.
        self.cache_path.write_text(json.dumps({"ts": 0, "outdated": []}), encoding="utf-8")
        with mock.patch(
            "updates._latest_version_pypi",
            side_effect=lambda p: "9.9.9" if p == "numpy" else None,
        ), mock.patch("updates._installed_version", return_value="1.0.0"):
            result = updates.check_updates(ttl_hours=6)
        self.assertEqual([d["name"] for d in result], ["numpy"])
        self.assertTrue(self.cache_path.exists())

    def test_la_versione_bloccata_viene_marcata(self):
        """Una candidata vietata dalle dipendenze installate si marca `blocked`.

        PyPI risponde "esiste" e basta: senza il confronto con la metadata
        installata il report non puo' sapere che pip non ci arriverebbe.
        """
        self.cache_path.write_text(json.dumps({"ts": 0, "outdated": []}), encoding="utf-8")
        vincoli = {"pillow": [("fastembed", "0.5.1", "<11.0.0,>=10.3.0")]}
        with mock.patch(
            "updates._latest_version_pypi", side_effect=lambda p: "12.3.0" if p == "pillow" else None
        ), mock.patch(
            "updates._installed_version", side_effect=lambda p: "10.4.0" if p == "pillow" else None
        ), mock.patch("updates._installed_constraints", return_value=vincoli):
            result = updates.check_updates(ttl_hours=6)
        self.assertEqual([d["name"] for d in result], ["pillow"])
        self.assertEqual(result[0]["blocked"], ["fastembed 0.5.1 impone <11.0.0,>=10.3.0"])

    def test_i_vincoli_si_leggono_una_volta_sola(self):
        # Una scansione della metadata per TUTTI i candidati, non una a testa.
        self.cache_path.write_text(json.dumps({"ts": 0, "outdated": []}), encoding="utf-8")
        with mock.patch("updates._latest_version_pypi", return_value="9.9.9"), mock.patch(
            "updates._installed_version", return_value="1.0.0"
        ), mock.patch("updates._installed_constraints", return_value={}) as scan:
            result = updates.check_updates(ttl_hours=6)
        scan.assert_called_once()
        self.assertGreater(len(result), 1)


class TestIsNewer(unittest.TestCase):
    """Confronto delle versioni per ORDINE, non per disuguaglianza di stringhe.

    `latest != installed` segnalava come outdated anche un downgrade e ogni
    versione con suffisso locale, quindi pacchetti gia' aggiornati finivano in
    _upgradable e l'utente veniva invitato a reinstallarli a ogni run.
    """

    def test_real_upgrades(self):
        for latest, installed in [("1.28.0", "1.27.0"), ("2.5.1", "2.5.0"), ("0.5.1", "0.4.9"), ("1.10.0", "1.9.0")]:
            with self.subTest(latest=latest, installed=installed):
                self.assertTrue(updates._is_newer(latest, installed))

    def test_downgrade_is_not_an_upgrade(self):
        self.assertFalse(updates._is_newer("1.26.3", "1.27.0"))

    def test_local_version_is_not_an_upgrade(self):
        # 2.5.0+cu124 e' piu' recente di 2.5.0: va lasciata stare (succede
        # con torch/cuda, openvino e pacchetti vendorizzati).
        self.assertFalse(updates._is_newer("2.5.0", "2.5.0+cu124"))

    def test_same_version(self):
        self.assertFalse(updates._is_newer("1.0.0", "1.0.0"))

    def test_unparseable_is_treated_as_newer(self):
        # Meglio segnalare un aggiornamento reale che perderlo.
        self.assertTrue(updates._is_newer("qualcosa", "1.0.0"))


class TestPrintUpdates(unittest.TestCase):
    def test_no_updates(self):
        with mock.patch("config.log.info") as info:
            updates.print_updates([])
        self.assertTrue(any("aggiornati" in str(c.args) for c in info.call_args_list))

    def test_with_updates_marks_pinned(self):
        data = [
            {"name": "fastembed", "installed": "0.5.1", "latest": "0.8.0", "pinned": True, "note": "pin"},
            {"name": "tqdm", "installed": "1.0", "latest": "1.1", "pinned": False, "note": ""},
        ]
        with mock.patch("config.log.info") as info:
            updates.print_updates(data)
        full = [" ".join(str(a) for a in c.args) for c in info.call_args_list]
        self.assertTrue(any("fastembed" in line and "pin" in line for line in full))
        self.assertTrue(any("tqdm" in line for line in full))

    def test_la_voce_bloccata_spiega_e_non_invita(self):
        """Chi e' bloccato si legge nel report, ma non entra nell'invito a `pip install -U`.

        Un invito a un comando che non porta alla versione annunciata e' peggio
        del silenzio: l'utente lo esegue, non succede nulla e la segnalazione si
        ripresenta identica alla run successiva.
        """
        data = [
            {
                "name": "pillow",
                "installed": "10.4.0",
                "latest": "12.3.0",
                "pinned": False,
                "major": True,
                "note": "",
                "blocked": [
                    "fastembed 0.5.1 impone <11.0.0,>=10.3.0",
                    "moviepy 2.2.1 impone <12.0,>=9.2.0",
                ],
            },
            {
                "name": "tqdm",
                "installed": "4.66.0",
                "latest": "4.67.0",
                "pinned": False,
                "major": False,
                "note": "",
                "blocked": [],
            },
        ]
        with mock.patch("config.log.info") as info:
            updates.print_updates(data)
        full = [" ".join(str(a) for a in c.args) for c in info.call_args_list]

        righe_pillow = [line for line in full if "pillow" in line and "->" in line]
        self.assertEqual(len(righe_pillow), 1)
        # il motivo vince sull'etichetta "major version": dice perche' non c'e' niente da fare
        self.assertIn("bloccato da fastembed 0.5.1 impone <11.0.0,>=10.3.0", righe_pillow[0])
        self.assertIn("+1", righe_pillow[0])  # il secondo bloccante si conta, non si nasconde
        self.assertNotIn("major version", righe_pillow[0])

        # l'avviso finale nomina i bloccati (trasparenza) ma non li propone
        avvisi = [line for line in full if "Non disponibili in questo ambiente" in line]
        self.assertEqual(len(avvisi), 1)
        self.assertIn("pillow", avvisi[0])
        self.assertTrue(any("pip install -U" in line for line in full))  # per tqdm

    def test_solo_voci_bloccate_niente_invito(self):
        data = [
            {
                "name": "pillow",
                "installed": "10.4.0",
                "latest": "12.3.0",
                "pinned": False,
                "major": True,
                "note": "",
                "blocked": ["fastembed 0.5.1 impone <11.0.0,>=10.3.0"],
            }
        ]
        with mock.patch("config.log.info") as info:
            updates.print_updates(data)
        full = [" ".join(str(a) for a in c.args) for c in info.call_args_list]
        self.assertFalse(any("pip install -U" in line for line in full))
        self.assertTrue(any("bloccato" in line for line in full))


class TestUpgradable(unittest.TestCase):
    def test_filters_pinned(self):
        data = [
            {"name": "numpy", "pinned": False},
            {"name": "fastembed", "pinned": True},
        ]
        names = [d["name"] for d in updates._upgradable(data)]
        self.assertEqual(names, ["numpy"])

    def test_filters_major_jump(self):
        data = [
            {"name": "pillow", "pinned": False, "major": True},
            {"name": "tqdm", "pinned": False, "major": False},
        ]
        names = [d["name"] for d in updates._upgradable(data)]
        self.assertEqual(names, ["tqdm"])

    def test_no_pinned_left(self):
        self.assertEqual(updates._upgradable([{"name": "fastembed", "pinned": True}]), [])

    def test_filters_blocked(self):
        # Non e' una scelta come i pinnati: e' un aggiornamento che non esiste,
        # quindi non va offerto (ne' ora ne' alla prossima run).
        data = [
            {
                "name": "pillow",
                "pinned": False,
                "major": False,
                "blocked": ["fastembed 0.5.1 impone <11.0.0,>=10.3.0"],
            },
            {"name": "tqdm", "pinned": False, "major": False, "blocked": []},
        ]
        names = [d["name"] for d in updates._upgradable(data)]
        self.assertEqual(names, ["tqdm"])


class TestFrozenReasons(unittest.TestCase):
    """I motivi per cui una voce non si aggiorna: sono il complemento di `_upgradable`.

    Se i due elenchi divergessero, la diagnostica spiegherebbe cose diverse da quelle
    che il report esclude: il test qui sotto è il guardiano di quell'uguaglianza.
    """

    def test_nessun_motivo_se_si_aggiorna(self):
        self.assertEqual(updates._frozen_reasons({"pinned": False, "major": False, "blocked": []}), [])

    def test_bloccato(self):
        self.assertEqual(
            updates._frozen_reasons({"pinned": False, "major": False, "blocked": ["x impone <1"]}),
            ["bloccato dalle dipendenze installate"],
        )

    def test_motivi_multipli(self):
        motivi = updates._frozen_reasons({"pinned": True, "major": True, "blocked": ["x impone <1"]})
        self.assertEqual(len(motivi), 3)
        # il blocco è il motivo più forte: va letto per primo
        self.assertIn("bloccato", motivi[0])

    def test_fermi_e_aggiornabili_non_si_sovrappongono(self):
        for d in (
            {"name": "a", "pinned": False, "major": False, "blocked": []},
            {"name": "b", "pinned": True, "major": False, "blocked": []},
            {"name": "c", "pinned": False, "major": True, "blocked": []},
            {"name": "d", "pinned": False, "major": False, "blocked": ["x impone <1"]},
            {"name": "e", "pinned": True, "major": True, "blocked": ["x impone <1"]},
        ):
            with self.subTest(nome=d["name"]):
                self.assertEqual(
                    bool(updates._frozen_reasons(d)),
                    d not in updates._upgradable([d]),
                    "una voce ferma deve essere esattamente una voce non aggiornabile",
                )


class TestIsMajorJump(unittest.TestCase):
    def test_minor_is_not_major(self):
        self.assertFalse(updates._is_major_jump("10.4.0", "10.5.0"))

    def test_patch_is_not_major(self):
        self.assertFalse(updates._is_major_jump("1.28.0", "1.28.2"))

    def test_major_jump(self):
        self.assertTrue(updates._is_major_jump("10.4.0", "12.3.0"))

    def test_unparseable_is_cautious(self):
        self.assertTrue(updates._is_major_jump("unknown", "12.0.0"))


# Caso reale misurato il 2026-10-10, nella forma che produce `_installed_constraints`:
# pillow 12 esiste, ma fastembed 0.5.1 e moviepy 2.2.1 la escludono.
_VINCOLI_PILLOW = [
    ("fastembed", "0.5.1", "<11.0.0,>=10.3.0"),
    ("moviepy", "2.2.1", "<12.0,>=9.2.0"),
    ("ImageIO", "2.38.0", ">=8.3.2"),
    ("pytesseract", "0.3.13", ">=8.0.0"),
]
_VERSIONI_PILLOW = ["8.0.0", "9.2.0", "10.3.0", "10.4.0", "11.0.0", "11.3.0", "12.3.0"]


def _voce_pillow():
    """La voce che `diagnose_frozen_packages` produce per pillow (caso reale)."""
    with mock.patch("updates._is_pinned", side_effect=lambda nome: nome == "fastembed"):
        voce = updates._frozen_diagnosis(
            "pillow", "10.4.0", "12.3.0", list(_VINCOLI_PILLOW), list(_VERSIONI_PILLOW)
        )
    voce["reasons"] = ["bloccato dalle dipendenze installate", "salto di major version"]
    return voce


class TestAllCompatibleVersions(unittest.TestCase):
    """L'elenco COMPLETO delle versioni installabili, che serve alla diagnostica.

    È lo stesso filtro di `_latest_compatible` (regola unica in `_installable_releases`):
    l'ultimo test lo verifica per costruzione, così le due funzioni non possono divergere
    su cosa è installabile.
    """

    def test_ordine_crescente(self):
        data = _pypi(**{"2.0.0": [_file()], "1.0.0": [_file()], "1.10.0": [_file()]})
        self.assertEqual(updates._all_compatible_versions(data), ["1.0.0", "1.10.0", "2.0.0"])

    def test_salta_pre_release_yanked_e_python_incompatibile(self):
        data = _pypi(
            **{
                "1.0.0": [_file()],
                "2.0.0b1": [_file()],
                "3.0.0": [_file(yanked=True)],
                "4.0.0": [_file(requires_python=_PY_IRRAGGIUNGIBILE)],
            }
        )
        self.assertEqual(updates._all_compatible_versions(data), ["1.0.0"])

    def test_nessuna_versione_utilizzabile(self):
        self.assertEqual(updates._all_compatible_versions(_pypi(**{"1.0.0": []})), [])

    def test_la_piu_alta_e_la_stessa_che_dice_latest_compatible(self):
        # invariante fra le due funzioni: se divergessero, il "tetto" della diagnostica
        # e l'aggiornamento annunciato dal report parlerebbero di due liste diverse
        data = _pypi(
            **{"1.0.0": [_file()], "1.5.0": [_file()], "2.0.0b1": [_file()], "3.0.0": [_file(yanked=True)]}
        )
        self.assertEqual(updates._all_compatible_versions(data)[-1], updates._latest_compatible(data))


class TestMaxSatisfying(unittest.TestCase):
    """`_max_satisfying`: la più alta versione che TUTTI i vincoli accettano.

    È il mattone del "fin dove si arriva": il tetto effettivo e l'effetto di ogni singolo
    vincolo si calcolano entrambi qui, quindi una sua svista falserebbe ogni riga del
    referto.
    """

    def test_la_piu_alta_che_soddisfa_tutti(self):
        self.assertEqual(updates._max_satisfying(_VERSIONI_PILLOW, ["<11.0.0,>=10.3.0", ">=8.3.2"]), "10.4.0")

    def test_senza_vincoli_e_la_piu_alta_in_assoluto(self):
        self.assertEqual(updates._max_satisfying(_VERSIONI_PILLOW, []), "12.3.0")

    def test_nessuna_versione_soddisfa(self):
        self.assertIsNone(updates._max_satisfying(_VERSIONI_PILLOW, [">=13.0"]))

    def test_versioni_non_note_non_sono_impossibile(self):
        # None da entrambe le parti: il chiamante distingue "non lo so" da "impossibile"
        # guardando `versions is None` (vedi `versions_known`), quindi il contratto è che
        # l'elenco non disponibile torni None e non una lista vuota
        self.assertIsNone(updates._max_satisfying(None, [">=1"]))
        self.assertIsNone(updates._max_satisfying([], [">=1"]))

    def test_specifier_illeggibile_ignorato(self):
        # stesso criterio di _blockers: un vincolo scritto male non produce un verdetto
        self.assertEqual(updates._max_satisfying(["1.0.0"], ["!=boh", ">=0.5"]), "1.0.0")

    def test_versioni_illeggibili_saltate(self):
        self.assertEqual(updates._max_satisfying(["boh", "1.0.0"], [">=0.1"]), "1.0.0")


class TestFrozenDiagnosis(unittest.TestCase):
    """Il calcolo puro del referto: tetto effettivo ed effetto di ogni singolo vincolo."""

    # `versions=None` è un caso del contratto (PyPI non ha risposto), quindi il default
    # non può essere None: va passato esplicitamente da chi lo vuole testare.
    def _diagnosi(self, vincoli=None, versions=_VERSIONI_PILLOW):
        with mock.patch("updates._is_pinned", side_effect=lambda nome: nome == "fastembed"):
            return updates._frozen_diagnosis(
                "pillow",
                "10.4.0",
                "12.3.0",
                list(_VINCOLI_PILLOW if vincoli is None else vincoli),
                list(versions) if versions is not None else None,
            )

    def test_tetto_effettivo(self):
        # non 12.3.0: il tetto è la più alta che TUTTI i vincoli accettano
        self.assertEqual(self._diagnosi()["ceiling"], "10.4.0")

    def test_solo_i_vincoli_che_escludono_la_candidata(self):
        self.assertEqual([c[0] for c in self._diagnosi()["excluders"]], ["fastembed", "moviepy"])

    def test_leve_una_per_volta(self):
        # sciogliendo moviepy non si guadagna nulla: il tetto resta quello di fastembed
        leva = self._diagnosi()["levers"]
        self.assertEqual([(m["by"], m["reachable"]) for m in leva], [("fastembed", "11.3.0"), ("moviepy", "10.4.0")])

    def test_leva_su_un_pacchetto_a_sua_volta_pinnato(self):
        # il muro di pillow è il pin di fastembed: senza dirlo, la diagnostica manderebbe
        # a cercare la leva nel posto sbagliato
        leva = self._diagnosi()["levers"]
        self.assertTrue(leva[0]["source_pinned"])
        self.assertFalse(leva[1]["source_pinned"])

    def test_pin_del_progetto_e_una_leva_a_se(self):
        d = self._diagnosi(vincoli=[(updates._PIN_SOURCE, "", "==0.5.1")], versions=["0.5.1", "0.9.0"])
        self.assertEqual(d["ceiling"], "0.5.1")
        self.assertTrue(d["levers"][0]["project_pin"])
        self.assertFalse(d["levers"][0]["source_pinned"])
        self.assertEqual(d["levers"][0]["reachable"], "0.9.0")

    def test_versioni_non_note(self):
        d = self._diagnosi(versions=None)
        self.assertFalse(d["versions_known"])
        self.assertIsNone(d["ceiling"])
        self.assertIsNone(d["levers"][0]["reachable"])
        # i vincoli locali restano: si possono comunque elencare, quelli non dipendono da PyPI
        self.assertEqual(len(d["constraints"]), 4)

    def test_nessun_vincolo_esclude(self):
        d = self._diagnosi(vincoli=[("pytesseract", "0.3.13", ">=8.0.0")])
        self.assertEqual(d["excluders"], [])
        self.assertEqual(d["levers"], [])
        self.assertEqual(d["ceiling"], "12.3.0")


def _righe_log(info):
    """Le righe di log come le legge un test: stringa di formato + argomenti, uniti."""
    return [" ".join(str(a) for a in c.args) for c in info.call_args_list]


class TestDiagnoseFrozenPackages(unittest.TestCase):
    """La parte con I/O: quali voci si diagnosticano, e con quali vincoli."""

    @staticmethod
    def _voci():
        """Cosa ha trovato il check aggiornamenti: un aggiornabile e due voci ferme."""
        return [
            {
                "name": "tqdm",
                "installed": "4.66.0",
                "latest": "4.67.0",
                "pinned": False,
                "major": False,
                "note": "",
                "blocked": [],
            },
            {
                "name": "pillow",
                "installed": "10.4.0",
                "latest": "12.3.0",
                "pinned": False,
                "major": True,
                "note": "",
                "blocked": ["fastembed 0.5.1 impone <11.0.0,>=10.3.0"],
            },
        ]

    def test_si_diagnosticano_solo_i_fermi(self):
        with mock.patch("updates.check_updates", return_value=self._voci()), mock.patch(
            "updates._installed_constraints",
            return_value={"pillow": [("fastembed", "0.5.1", "<11.0.0,>=10.3.0")]},
        ), mock.patch("updates._available_versions", return_value=["10.4.0", "12.3.0"]), mock.patch(
            "updates._pinned_requirement", return_value=None
        ), mock.patch("updates._is_pinned", return_value=False):
            diagnosi = updates.diagnose_frozen_packages()
        # tqdm si aggiorna da solo: non c'e' niente da spiegare
        self.assertEqual([d["name"] for d in diagnosi], ["pillow"])
        self.assertEqual(
            diagnosi[0]["reasons"], ["bloccato dalle dipendenze installate", "salto di major version"]
        )

    def test_il_pin_del_progetto_entra_fra_i_vincoli(self):
        # senza il pin, un pacchetto fermo per scelta del progetto sembrerebbe fermo
        # senza motivo
        voci = [dict(self._voci()[1], name="fastembed", pinned=True)]
        with mock.patch("updates.check_updates", return_value=voci), mock.patch(
            "updates._installed_constraints", return_value={}
        ), mock.patch("updates._available_versions", return_value=["0.5.1", "0.9.0"]), mock.patch(
            "updates._pinned_requirement", return_value="==0.5.1"
        ), mock.patch("updates._is_pinned", return_value=True):
            diagnosi = updates.diagnose_frozen_packages()
        self.assertEqual(diagnosi[0]["constraints"], [(updates._PIN_SOURCE, "", "==0.5.1")])
        self.assertEqual(diagnosi[0]["ceiling"], "0.5.1")

    def test_niente_fermo_non_scansiona_la_metadata(self):
        with mock.patch("updates.check_updates", return_value=[self._voci()[0]]), mock.patch(
            "updates._installed_constraints"
        ) as scan:
            self.assertEqual(updates.diagnose_frozen_packages(), [])
        scan.assert_not_called()


class TestPrintFrozenDiagnosis(unittest.TestCase):
    """Il referto: si legge chi vincola cosa, e non si invita a installare niente."""

    def test_nessun_fermo(self):
        with mock.patch("config.log.info") as info:
            updates.print_frozen_diagnosis([])
        self.assertTrue(any("Nessun pacchetto fermo" in riga for riga in _righe_log(info)))

    def test_mostra_chi_vincola_e_chi_ammette(self):
        with mock.patch("config.log.info") as info:
            updates.print_frozen_diagnosis([_voce_pillow()])
        testo = "\n".join(_righe_log(info))
        self.assertIn("fastembed", testo)
        self.assertIn("esclude 12.3.0", testo)
        self.assertIn("ammette 12.3.0", testo)

    def test_tetto_e_leve(self):
        with mock.patch("config.log.info") as info:
            updates.print_frozen_diagnosis([_voce_pillow()])
        righe = _righe_log(info)
        testo = "\n".join(righe)
        tetti = [riga for riga in righe if "tetto" in riga]
        self.assertEqual(len(tetti), 1)
        self.assertIn("10.4.0", tetti[0])  # il tetto, non la candidata
        self.assertIn("vanno sciolti 2 vincoli", testo)
        # una leva per vincolo, con la versione che si raggiungerebbe sciogliendo quello
        self.assertTrue(any("senza" in r and "fastembed" in r and "11.3.0" in r for r in righe))
        self.assertTrue(any("senza" in r and "moviepy" in r and "nessun guadagno" in r for r in righe))
        self.assertIn("a sua volta pinnato dal progetto", testo)

    def test_non_invita_a_installare(self):
        # e' un referto: niente comandi da copiare, nessuna domanda. Chi legge deve
        # capire cosa lo frena, non ricevere un altro suggerimento da eseguire.
        with mock.patch("config.log.info") as info:
            updates.print_frozen_diagnosis([_voce_pillow()])
        self.assertFalse(any("pip install" in riga for riga in _righe_log(info)))

    def test_senza_versioni_dice_che_non_sa(self):
        voce = _voce_pillow()
        voce["versions_known"] = False
        voce["ceiling"] = None
        for leva in voce["levers"]:
            leva["reachable"] = None
        with mock.patch("config.log.info") as info:
            updates.print_frozen_diagnosis([voce])
        testo = "\n".join(_righe_log(info))
        self.assertIn("tetto non calcolabile", testo)
        self.assertIn("non calcolabile (PyPI non ha risposto)", testo)
        self.assertNotIn("nessun guadagno", testo)

    def test_major_senza_vincoli_escludenti(self):
        # un salto di major non ha bisogno di colpevoli: la candidata è installabile, è
        # il tool che non la fa da solo. Il referto lo dice invece di lasciare il vuoto.
        voce = updates._frozen_diagnosis(
            "numpy", "1.26.0", "2.0.0", [("pandas", "2.2.0", ">=1.22.4")], ["1.26.0", "2.0.0"]
        )
        voce["reasons"] = ["salto di major version"]
        with mock.patch("config.log.info") as info:
            updates.print_frozen_diagnosis([voce])
        testo = "\n".join(_righe_log(info))
        self.assertIn("non è esclusa da nessun vincolo", testo)
        self.assertIn("il freno è la riga 'motivo'", testo)

    def test_pin_del_progetto(self):
        voce = updates._frozen_diagnosis(
            "fastembed", "0.5.1", "0.9.0", [(updates._PIN_SOURCE, "", "==0.5.1")], ["0.5.1", "0.9.0"]
        )
        voce["reasons"] = ["pinnato dal progetto"]
        with mock.patch("config.log.info") as info:
            updates.print_frozen_diagnosis([voce])
        testo = "\n".join(_righe_log(info))
        self.assertIn("pin del progetto", testo)
        self.assertIn("scelta del progetto", testo)
        self.assertIn("va sciolto 1 vincolo", testo)


class TestRunFrozenReport(unittest.TestCase):
    """La diagnostica e' un referto: non chiede conferme e non installa niente."""

    def test_non_chiede_e_non_installa(self):
        with mock.patch("updates.diagnose_frozen_packages", return_value=[]) as diag, mock.patch(
            "updates.print_frozen_diagnosis"
        ) as stampa, mock.patch("builtins.input") as inp, mock.patch("updates._pip_upgrade") as upg:
            updates.run_frozen_report()
        diag.assert_called_once_with(ttl_hours=0.0)
        stampa.assert_called_once_with([])
        inp.assert_not_called()
        upg.assert_not_called()


class TestRunUpdateCheck(_TempCacheMixin, unittest.TestCase):
    def test_no_updates_no_prompt(self):
        self.cache_path.write_text(json.dumps({"ts": 1e15, "outdated": []}), encoding="utf-8")
        with mock.patch("builtins.input") as inp, mock.patch("updates._pip_upgrade") as upg:
            updates.run_update_check(ttl_hours=6, ask_to_update=True)
            inp.assert_not_called()
            upg.assert_not_called()

    def test_prompt_yes_upgrades_non_pinned(self):
        self.cache_path.write_text(
            json.dumps(
                {
                    "ts": 1e15,
                    "outdated": [
                        {"name": "numpy", "installed": "1", "latest": "2", "pinned": False, "note": ""},
                        {"name": "fastembed", "installed": "1", "latest": "2", "pinned": True, "note": "pin"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with mock.patch("builtins.input", return_value="s"), mock.patch("updates._pip_upgrade") as upg, mock.patch(
            "updates._run_pinned_ab_test", return_value=None
        ):
            updates.run_update_check(ttl_hours=6, ask_to_update=True)
        upg.assert_called_once_with(["numpy"])
        self.assertFalse(self.cache_path.exists())

    def test_pinned_equivalent_included_in_upgrade(self):
        self.cache_path.write_text(
            json.dumps(
                {
                    "ts": 1e15,
                    "outdated": [
                        {"name": "fastembed", "installed": "1", "latest": "2", "pinned": True, "note": "pin"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with mock.patch("builtins.input", return_value="s"), mock.patch("updates._pip_upgrade") as upg, mock.patch(
            "updates._run_pinned_ab_test", return_value="EQUIVALENTE"
        ):
            updates.run_update_check(ttl_hours=6, ask_to_update=True)
        upg.assert_called_once_with(["fastembed"])

    def test_pinned_divergent_stays_pinned(self):
        self.cache_path.write_text(
            json.dumps(
                {
                    "ts": 1e15,
                    "outdated": [
                        {"name": "fastembed", "installed": "1", "latest": "2", "pinned": True, "note": "pin"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with mock.patch("builtins.input") as inp, mock.patch("updates._pip_upgrade") as upg, mock.patch(
            "updates._run_pinned_ab_test", return_value="DIVERGENTE"
        ):
            updates.run_update_check(ttl_hours=6, ask_to_update=True)
        upg.assert_not_called()
        inp.assert_not_called()

    def test_major_not_upgraded_automatically(self):
        self.cache_path.write_text(
            json.dumps(
                {
                    "ts": 1e15,
                    "outdated": [
                        {
                            "name": "pillow",
                            "installed": "10.4.0",
                            "latest": "12.3.0",
                            "pinned": False,
                            "major": True,
                            "note": "",
                        },
                        {
                            "name": "tqdm",
                            "installed": "1.0",
                            "latest": "1.1",
                            "pinned": False,
                            "major": False,
                            "note": "",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        with mock.patch("builtins.input", return_value="s"), mock.patch("updates._pip_upgrade") as upg:
            updates.run_update_check(ttl_hours=6, ask_to_update=True)
        upg.assert_called_once_with(["tqdm"])

    def test_prompt_no_skips(self):
        self.cache_path.write_text(
            json.dumps(
                {
                    "ts": 1e15,
                    "outdated": [
                        {"name": "tqdm", "installed": "1.0", "latest": "1.1", "pinned": False, "note": ""},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with mock.patch("builtins.input", return_value="n"), mock.patch("updates._pip_upgrade") as upg:
            updates.run_update_check(ttl_hours=6, ask_to_update=True)
        upg.assert_not_called()

    def test_bloccato_non_viene_offerto(self):
        """Se l'unico candidato e' bloccato non si chiede niente, perche' non c'e' cosa installare."""
        self._cache_con_bloccato(solo_pillow=True)
        with mock.patch("builtins.input") as inp, mock.patch("updates._pip_upgrade") as upg:
            updates.run_update_check(ttl_hours=6, ask_to_update=True)
        inp.assert_not_called()
        upg.assert_not_called()

    def test_bloccato_escluso_dal_batch(self):
        # I bloccati non viaggiano insieme agli aggiornabili: si aggiorna tqdm,
        # pillow no (e la cache si invalida solo se qualcosa e' stato installato).
        self._cache_con_bloccato(solo_pillow=False)
        with mock.patch("builtins.input", return_value="s"), mock.patch("updates._pip_upgrade") as upg:
            updates.run_update_check(ttl_hours=6, ask_to_update=True)
        upg.assert_called_once_with(["tqdm"])

    def test_pinnato_bloccato_non_fa_il_test_ab(self):
        """Un pinnato bloccato non finisce nel test A/B.

        Il test crea una venv temporanea e installa il candidato: se il grafo
        delle dipendenze non lo installa, il verdetto non sarebbe spendibile.
        """
        self.cache_path.write_text(
            json.dumps(
                {
                    "ts": 1e15,
                    "outdated": [
                        {
                            "name": "fastembed",
                            "installed": "0.5.1",
                            "latest": "0.8.0",
                            "pinned": True,
                            "major": True,
                            "note": "pin",
                            "blocked": ["qualcosa 1.0 impone <0.6"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with mock.patch("updates._run_pinned_ab_test") as ab, mock.patch("builtins.input") as inp, mock.patch(
            "updates._pip_upgrade"
        ) as upg:
            updates.run_update_check(ttl_hours=6, ask_to_update=True)
        ab.assert_not_called()
        inp.assert_not_called()
        upg.assert_not_called()

    def _cache_con_bloccato(self, *, solo_pillow: bool) -> None:
        """Cache con pillow bloccato (major, ma non pinnato) e, se richiesto, tqdm aggiornabile."""
        outdated = [
            {
                "name": "pillow",
                "installed": "10.4.0",
                "latest": "12.3.0",
                "pinned": False,
                "major": True,
                "note": "",
                "blocked": ["fastembed 0.5.1 impone <11.0.0,>=10.3.0"],
            }
        ]
        if not solo_pillow:
            outdated.append(
                {
                    "name": "tqdm",
                    "installed": "4.66.0",
                    "latest": "4.67.0",
                    "pinned": False,
                    "major": False,
                    "note": "",
                    "blocked": [],
                }
            )
        self.cache_path.write_text(json.dumps({"ts": 1e15, "outdated": outdated}), encoding="utf-8")


class TestBootstrapEnvBlocked(unittest.TestCase):
    """Bootstrap: solo gli errori di ambiente contano come "installato ma
    bloccato"; un ModuleNotFoundError (pacchetto o dipendenza davvero
    mancante) deve essere segnalato come mancante e quindi installato."""

    def test_missing_package_is_not_blocked(self):
        from config import _is_env_blocked_import

        self.assertFalse(
            _is_env_blocked_import(
                "faster_whisper", ModuleNotFoundError("No module named 'faster_whisper'")
            )
        )
        self.assertFalse(
            _is_env_blocked_import("moviepy", ModuleNotFoundError("No module named 'numpy'"))
        )

    def test_env_blocked_import_is_true(self):
        from config import _is_env_blocked_import

        # DLL di PyAV bloccata su Windows / ffmpeg di sistema mancante
        self.assertTrue(_is_env_blocked_import("faster_whisper", ImportError("DLL load failed")))
        self.assertTrue(_is_env_blocked_import("moviepy", RuntimeError("ffmpeg missing")))

    def test_other_packages_never_blocked(self):
        from config import _is_env_blocked_import

        self.assertFalse(_is_env_blocked_import("numpy", ImportError("x")))
        self.assertFalse(_is_env_blocked_import("pymupdf", RuntimeError("x")))


# Python che nessuna delle versioni di CI soddisfa: serve per i test su
# requires_python senza dipendere dalla versione in esecuzione.
_PY_IRRAGGIUNGIBILE = ">=3.99"
_PY_QUALSIASI = ">=3.9"


def _file(yanked=False, requires_python=None):
    return {"filename": "x.whl", "yanked": yanked, "requires_python": requires_python}


def _pypi(**releases):
    return {"releases": {v: files for v, files in releases.items()}}


class TestLatestCompatible(unittest.TestCase):
    """La "latest" deve essere una versione che pip installerebbe davvero."""

    def test_sceglie_la_piu_alta(self):
        data = _pypi(**{"1.0.0": [_file()], "1.5.0": [_file()], "2.0.0": [_file()]})
        self.assertEqual(updates._latest_compatible(data), "2.0.0")

    def test_salta_pre_release(self):
        # pip senza --pre non installa una beta: annunciarla sarebbe un falso
        # aggiornamento (il report direbbe "1.0.0 -> 2.0.0b1" e pip non farebbe nulla)
        data = _pypi(**{"1.0.0": [_file()], "2.0.0b1": [_file()]})
        self.assertEqual(updates._latest_compatible(data), "1.0.0")

    def test_salta_release_interamente_yanked(self):
        data = _pypi(**{"1.0.0": [_file()], "2.0.0": [_file(yanked=True)]})
        self.assertEqual(updates._latest_compatible(data), "1.0.0")

    def test_requires_python_dal_primo_file_utile(self):
        # files[0] è yanked e dichiara un Python più nuovo: non deve escludere
        # la release, perché esiste un file che il Python corrente lo accetta
        data = _pypi(
            **{
                "2.0.0": [
                    _file(yanked=True, requires_python=_PY_IRRAGGIUNGIBILE),
                    _file(requires_python=_PY_QUALSIASI),
                ]
            }
        )
        self.assertEqual(updates._latest_compatible(data), "2.0.0")

    def test_salta_requires_python_incompatibile(self):
        data = _pypi(**{"1.0.0": [_file()], "2.0.0": [_file(requires_python=_PY_IRRAGGIUNGIBILE)]})
        self.assertEqual(updates._latest_compatible(data), "1.0.0")

    def test_salta_release_senza_file_e_versioni_illeggibili(self):
        data = _pypi(**{"1.0.0": [_file()], "2.0.0": [], "boh": [_file()]})
        self.assertEqual(updates._latest_compatible(data), "1.0.0")

    def test_nessuna_release_utilizzabile(self):
        data = _pypi(**{"1.0.0": [_file(yanked=True)]})
        self.assertIsNone(updates._latest_compatible(data))


def _urlopen_con(contenuto: bytes):
    """Finto urlopen che restituisce il JSON indicato."""
    gestore = mock.MagicMock()
    gestore.__enter__.return_value.read.return_value = contenuto
    return gestore


class TestLatestVersionPypiNonSolleva(unittest.TestCase):
    """`_latest_version_pypi` è interrogata in un thread pool: mai eccezioni."""

    def test_json_valido(self):
        data = json.dumps({"releases": {"1.0.0": [_file()]}}).encode()
        with mock.patch("updates.urllib.request.urlopen", return_value=_urlopen_con(data)):
            self.assertEqual(updates._latest_version_pypi("qualsiasi"), "1.0.0")

    def test_errore_interno_non_solleva(self):
        data = json.dumps({"releases": {"1.0.0": [_file()]}}).encode()
        with mock.patch("updates.urllib.request.urlopen", return_value=_urlopen_con(data)), mock.patch(
            "updates._latest_compatible", side_effect=RuntimeError("versione assurda")
        ), mock.patch("updates.log"):
            self.assertIsNone(updates._latest_version_pypi("qualsiasi"))

    def test_rete_assente_non_solleva(self):
        with mock.patch("updates.urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertIsNone(updates._latest_version_pypi("qualsiasi"))


class TestTailLines(unittest.TestCase):
    def test_ultime_righe_non_vuote(self):
        testo = "prima\n\nseconda\n   \nterza\n"
        self.assertEqual(updates._tail_lines(testo, 2), ["seconda", "terza"])

    def test_testo_vuoto(self):
        self.assertEqual(updates._tail_lines(None), [])


class TestPipUpgrade(unittest.TestCase):
    def _risultato(self, returncode, stdout="", stderr=""):
        return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_successo_mostra_la_riga_di_pip(self):
        res = self._risultato(0, stdout="Successfully installed numpy-1.26.4")
        with mock.patch("updates.subprocess.run", return_value=res) as run, mock.patch("updates.log") as log:
            self.assertTrue(updates._pip_upgrade(["numpy"]))
        comando = run.call_args.args[0]
        self.assertEqual(comando[-2:], ["-U", "numpy"])
        messaggi = " ".join(str(argomento) for chiamata in log.info.call_args_list for argomento in chiamata.args)
        self.assertIn("Successfully installed numpy-1.26.4", messaggi)

    def test_fallimento_mostra_le_ultime_righe_di_pip(self):
        # prima si vedeva solo "non-zero exit status 1": inutile per capire se
        # è la rete, un conflitto di versioni o i permessi
        res = self._risultato(1, stderr="ERROR: ResolutionImpossible\nERROR: No matching distribution found")
        with mock.patch("updates.subprocess.run", return_value=res), mock.patch("updates.log") as log:
            self.assertFalse(updates._pip_upgrade(["numpy>=1.26.0"]))
        # il codice di uscita di pip è il primo argomento della prima riga
        self.assertEqual(log.warning.call_args_list[0].args[1], 1)
        messaggi = " ".join(str(argomento) for chiamata in log.warning.call_args_list for argomento in chiamata.args)
        self.assertIn("No matching distribution", messaggi)

    def test_fallimento_senza_stderr_usa_stdout(self):
        res = self._risultato(2, stdout="qualcosa e' andato storto")
        with mock.patch("updates.subprocess.run", return_value=res), mock.patch("updates.log") as log:
            self.assertFalse(updates._pip_upgrade(["numpy"]))
        messaggi = " ".join(str(argomento) for chiamata in log.warning.call_args_list for argomento in chiamata.args)
        self.assertIn("qualcosa e' andato storto", messaggi)

    def test_timeout(self):
        with mock.patch(
            "updates.subprocess.run", side_effect=subprocess.TimeoutExpired("pip", 900)
        ), mock.patch("updates.log"):
            self.assertFalse(updates._pip_upgrade(["numpy"]))


if __name__ == "__main__":
    unittest.main()
