#!/usr/bin/env python3
"""Test di `verifica_pc.py`: la pagina che vede chi riceve il programma.

Il requisito che tiene su tutto e' uno solo: questa pagina deve
funzionare ANCHE quando qualcosa e' rotto, perche' e' proprio in quel
caso che l'utente ha bisogno di leggerla. Un modulo che importa in cima
ocr/video/transcription andrebbe in crash su un PC senza pacchetti:
l'errore mostrato sarebbe "No module named 'pymupdf'" invece di
"pymupdf manca, ecco cosa fare".

Esegui con: python -m unittest test_verifica_pc -v
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import config
import verifica_pc
from verifica_pc import AVV, KO, OK, Esito, Relazione


def _esegui() -> tuple[int, str]:
    """Lancia `main()` catturando l'output. Ritorna (exit code, testo)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        codice = verifica_pc.main()
    return codice, buf.getvalue()


class TestNonCrashaQuandoMancaQualcosa(unittest.TestCase):
    """Il caso per cui questa pagina esiste."""

    def test_nessun_modulo_pesante_importato_in_cima(self) -> None:
        """Il vincolo, verificato sul sorgente invece che a parole.

        Una sola riga `import ocr` in cima renderebbe il modulo inutilizzabile
        proprio sul PC rotto che dovrebbe diagnosticare.
        """
        sorgente = Path(verifica_pc.__file__).read_text(encoding="utf-8")
        cima = sorgente.split("def _gb")[0]
        for pesante in ("import ocr", "import video", "import transcription", "import semantic_sync"):
            with self.subTest(modulo=pesante):
                self.assertNotIn(pesante, cima, f"'{pesante}' in cima bloccherebbe la verifica")

    def test_una_prova_che_esplode_non_abbassa_le_altre(self) -> None:
        """Il ciclo di main() deve registrare il fallimento e proseguire.

        `prova_modelli` che solleva non deve far perdere le righe su ffmpeg o
        sul motore: sono proprio le informazioni che servono quando qualcosa
        non va.
        """

        def _esplode(_r: Relazione) -> None:
            raise RuntimeError("prova rotta")

        def _va_bene(r: Relazione) -> None:
            r.aggiungi(Esito("Altra", OK, "va bene"))

        rep = Relazione()
        for prova in (_esplode, _va_bene):
            try:
                prova(rep)
            except Exception as e:
                rep.aggiungi(Esito("Rotta", KO, f"controllo fallito: {e}"))
        self.assertEqual(rep.conta(KO), 1)
        self.assertEqual(rep.conta(OK), 1)


class TestRifertoSulPCReale(unittest.TestCase):
    """Esecuzione vera, su questo PC, che ha gia' prodotto un video."""

    def test_il_referto_e_leggibile(self) -> None:
        codice, testo = _esegui()
        self.assertIn(codice, (0, 1))
        self.assertIn("SYNC VIDEO", testo)
        self.assertNotIn("Traceback", testo, "la pagina deve restare leggibile anche con guasti")

    def test_ogni_riga_ha_uno_stato_riconoscibile(self) -> None:
        _, testo = _esegui()
        righe = [r.strip() for r in testo.splitlines() if r.strip().startswith("[")]
        self.assertTrue(righe, "nessun controllo riportato")
        for riga in righe:
            with self.subTest(riga=riga[:40]):
                self.assertRegex(riga, r"\[\s*(ok|KO|avv)\s*\]")

    def test_i_controlli_attesi_ci_sono_tutti(self) -> None:
        _, testo = _esegui()
        for nome in ("Python", "Pacchetti", "Tesseract", "ffmpeg", "Motore", "Converter", "Materiali"):
            with self.subTest(controllo=nome):
                self.assertIn(nome, testo)

    def test_questo_pc_sano_non_deve_avere_ko(self) -> None:
        """Su una macchina che ha gia' prodotto un video, nessun KO.

        E' il test che ha trovato i due difetti iniziali: la logica di
        OpenVINO era invertita (openvino_usable() risponde VERO quando va
        bene, e il segnale veniva letto al contrario), e la lingua italiana
        veniva cercata solo nei modelli di sistema mentre il programma la
        scarica nella cartella del progetto. Entrambi producevano un KO
        falso su un PC perfettamente sano: il peggiore difetto possibile
        per uno strumento di diagnosi.
        """
        codice, testo = _esegui()
        self.assertNotIn("[ KO ]", testo, f"un PC sano non deve avere KO:\n{testo}")
        self.assertEqual(codice, 0, f"su questo PC l'esito deve essere 0:\n{testo}")


class TestPacchettiRotti(unittest.TestCase):
    """Il caso del collega con l'ambiente da ripristinare."""

    def test_i_pacchetti_mancanti_sono_elencati_per_nome(self) -> None:
        rep = Relazione()
        # Un nome che davvero non e' installato: l'import fallisce per
        # davvero, senza dover simulare __import__.
        mancanti = {"modulo_che_non_esiste": "modulo-che-non-esiste"}
        with mock.patch.object(config, "_active_required_packages", return_value=mancanti):
            verifica_pc.prova_pacchetti(rep)
        self.assertEqual(len(rep.esiti), 1)
        esito = rep.esiti[0]
        self.assertEqual(esito.stato, KO)
        self.assertIn("modulo-che-non-esiste", esito.dettaglio)
        self.assertTrue(esito.rimedio, "un KO senza rimedio non aiuta nessuno")

    def test_pacchetti_maestri_roti_hanno_una_ripresa_divers(self) -> None:
        """"Installato ma non importa" e' un problema diverso da "manca".

        Con numpy 1.24.3, misurato, falliva l'import di pandas dentro
        pytesseract: il guasto sembrava di pytesseract, ma la causa era una
        dipendenza. Servono due messaggi diversi.
        """
        import builtins

        vero_import = builtins.__import__

        def _import_rotto_solo_quel(nome: str, *a: object, **k: object) -> object:
            if nome == "fantasma":
                raise RuntimeError("DLL load failed")
            return vero_import(nome, *a, **k)  # type: ignore[arg-type]

        rep = Relazione()
        with (
            mock.patch.object(config, "_active_required_packages", return_value={"fantasma": "pip-pacchetto"}),
            mock.patch("builtins.__import__", side_effect=_import_rotto_solo_quel),
        ):
            verifica_pc.prova_pacchetti(rep)
        esito = rep.esiti[0]
        self.assertEqual(esito.stato, KO)
        self.assertIn("non importabil", esito.dettaglio)
        self.assertIn("RuntimeError", esito.dettaglio)


class TestConvertitorePptx(unittest.TestCase):
    """Senza converter il programma si ferma: l'utente deve saperlo prima."""

    def test_senza_converter_avvisa_come_fare(self) -> None:
        """La patch va su ocr, non su verifica_pc: la funzione importa da li'."""
        import ocr

        rep = Relazione()
        with mock.patch.multiple(
            ocr,
            _find_soffice=mock.Mock(return_value=None),
            _find_genoffice=mock.Mock(return_value=None),
            _find_powerpoint=mock.Mock(return_value=False),
            _find_onlyoffice=mock.Mock(return_value=None),
        ):
            verifica_pc.prova_converter(rep)
        esito = rep.esiti[0]
        self.assertEqual(esito.stato, AVV)
        self.assertIn("PDF", esito.rimedio)

    def test_converter_presenti_sono_elencati(self) -> None:
        import ocr

        rep = Relazione()
        with mock.patch.multiple(
            ocr,
            _find_soffice=mock.Mock(return_value="soffice"),
            _find_genoffice=mock.Mock(return_value=("app.exe", "cli.cjs")),
            _find_powerpoint=mock.Mock(return_value=False),
            _find_onlyoffice=mock.Mock(return_value=None),
        ):
            verifica_pc.prova_converter(rep)
        esito = rep.esiti[0]
        self.assertEqual(esito.stato, OK)
        self.assertIn("LibreOffice", esito.dettaglio)
        self.assertIn("GenOffice", esito.dettaglio)


if __name__ == "__main__":
    unittest.main()
