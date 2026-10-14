#!/usr/bin/env python3
"""Test del primo avvio su un PC nuovo: installazione e assenza di materiali.

Il bug che questi test coprono e' stato trovato dall'utente, non da un
controllo: scaricando lo ZIP e avviando, il programma moriva con

    File "src/main.py", line 21, in <module>
        from moviepy import AudioFileClip, VideoFileClip
    ModuleNotFoundError: No module named 'moviepy'

perche' gli import delle terze parti stanno in cima al file, mentre
bootstrap() (che sa installare tutto) gira dentro main(). Su un PC dove i
pacchetti ci sono non se ne accorge nessuno: e' il caso normale.

Esegui con: python -m unittest test_primo_avvio -v
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import config


class TestOrdineImportEDipendenze(unittest.TestCase):
    """L'installazione deve avvenire PRIMA del primo import pesante.

    E' l'unico controllo che regge su questo progetto: qui i pacchetti sono
    gia' installati, quindi ogni altro test passa anche con l'ordine sbagliato
    (ed e' cosi' che il bug e' arrivato in main).
    """

    def _sorgente_main(self) -> str:
        import main

        return Path(main.__file__).read_text(encoding="utf-8")

    def test_la_chiamata_esiste_una_volta(self) -> None:
        sorgente = self._sorgente_main()
        chiamate = re.findall(r"^installa_dipendenze_se_mancano\(\)\s*$", sorgente, re.MULTILINE)
        self.assertEqual(
            len(chiamate),
            1,
            "main.py deve chiamare installa_dipendenze_se_mancano() esattamente una volta, "
            "a livello di modulo e PRIMA degli import pesanti",
        )

    def test_viene_prima_del_primo_import_pesante(self) -> None:
        """Il cuore del bug: se l'ordine si inverte, il PC nuovo muole."""
        sorgente = self._sorgente_main()

        chiamata = re.search(r"^installa_dipendenze_se_mancano\(\)\s*$", sorgente, re.MULTILINE)
        if chiamata is None:
            self.fail("la chiamata di installazione non c'e' piu' in main.py")

        # I moduli che tirano dentro una terza parte: senza dipendenze
        # installate, sono loro a far fallire l'avvio su un PC vuoto.
        pesanti = re.search(
            r"^(?:from|import)\s+(moviepy|ocr|video|semantic_sync|embedder|transcription)\b",
            sorgente,
            re.MULTILINE,
        )
        if pesanti is None:
            self.fail("nessun import pesante trovato: il test non starebbe testando niente")

        def _riga(pos: int) -> int:
            return sorgente[:pos].count("\n") + 1

        self.assertLess(
            chiamata.start(),
            pesanti.start(),
            f"installa_dipendenze_se_mancano() (riga {_riga(chiamata.start())}) "
            f"deve precedere l'import di {pesanti.group(1)!r} "
            f"(riga {_riga(pesanti.start())}), altrimenti su un PC "
            "nuovo l'import fallisce prima che le dipendenze vengano installate",
        )


class TestInstallaDipendenzeSeMancano(unittest.TestCase):
    """La funzione non deve installare nulla quando non serve."""

    def test_non_installa_se_tutto_e_presente(self) -> None:
        with (
            mock.patch.object(config, "_missing_or_old", return_value=None),
            mock.patch.object(config, "_ensure_pip_packages") as installa,
        ):
            self.assertFalse(config.installa_dipendenze_se_mancano())
        installa.assert_not_called()

    def test_installa_se_manca_qualcosa(self) -> None:
        with (
            mock.patch.object(config, "_missing_or_old", return_value="assente"),
            mock.patch.object(config, "_ensure_pip_packages") as installa,
        ):
            self.assertTrue(config.installa_dipendenze_se_mancano())
        installa.assert_called_once()

    def test_controlla_tutti_i_pacchetti_richiesti(self) -> None:
        """Deve guardare TUTTI i pacchetti: uno solo saltato resta un PC rotto."""
        with mock.patch.object(config, "_missing_or_old", return_value=None) as guarda:
            config.installa_dipendenze_se_mancano()
        richiesti = len(config._active_required_packages())
        self.assertEqual(guarda.call_count, richiesti)


class TestSenzaMateriali(unittest.TestCase):
    """Senza presentazione e senza audio non e' un errore: e' il primo avvio.

    Prima di questo comportamento il programma installava tutto e poi usciva
    con "[ERRORE] File presentazione non trovato", lasciando l'utente con un
    programma che sembrava rotto invece che semplicemente vuoto.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.radice = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _main(self, argv: list[str]) -> None:
        """Esegue main() con bootstrap/machine_setup/addornamenti spenti."""
        import main

        main.main(
            [
                "--no-update-check",
                "--no-confirm",
                "--no-auto-setup",
                *argv,
            ]
        )

    def _esegui_senza_materiali(self, argv: list[str]) -> Any:
        import main

        with (
            mock.patch.object(config, "BASE_DIR", self.radice),
            mock.patch.object(main, "BASE_DIR", self.radice),
            mock.patch.object(main, "bootstrap"),
            mock.patch.object(main, "machine_setup"),
            mock.patch.object(main, "find_audio_file", return_value=None),
            mock.patch.object(main, "_prefetch_models") as prefetch,
        ):
            self._main(argv)
        return prefetch

    def test_assenza_materiali_prepara_il_pc_e_non_e_errore(self) -> None:
        prefetch = self._esegui_senza_materiali([])
        prefetch.assert_called_once()

    def test_percorso_esplicito_inesistente_resta_un_errore(self) -> None:
        """Un `--pdf` sbagliato e' quasi sempre un refuso: niente download da 3 GB.

        Deve uscire con codice 1 come prima: qui l'errore e' vero, il file
        esiste forse ma altrove e l'utente deve accorgersene.
        """
        with self.assertRaises(SystemExit) as ctx:
            prefetch = self._esegui_senza_materiali(["--pdf", str(self.radice / "refuso.pptx")])
            prefetch.assert_not_called()
        self.assertEqual(ctx.exception.code, 1)

    def test_esce_normalmente_perche_e_un_avvio_e_non_un_errore(self) -> None:
        """Ritorno pulito, nessuna SystemExit: il programma ha fatto il suo lavoro.

        `main.main()` che ritorna senza eccezioni = exit code 0 lato script
        (`if __name__ == "__main__": main()`). E' la differenza rispetto al
        ramo di errore sopra, che solleva SystemExit(1).
        """
        prefetch = self._esegui_senza_materiali([])
        prefetch.assert_called_once()


class TestRicercaLiberaPresentazione(unittest.TestCase):
    """La presentazione vale anche con nome non standard (doppio download, nome proprio).

    Senza, il primo avvio diceva "manca il materiale" con il file li' accanto:
    il caso "presentazione (1).pptx" scaricato due volte dal browser.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.radice = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _tocca(self, nome: str) -> Path:
        percorso = self.radice / nome
        percorso.write_bytes(b"finto")
        return percorso

    def test_nome_esatto_ha_la_precedenza(self) -> None:
        import main

        vecchio = self._tocca("mie_slide.pptx")
        vecchio.touch()
        esatto = self._tocca("presentazione.pptx")
        # Il libero e' piu' recente, ma l'esatto vince comunque.
        passato = time.time() - 100
        os.utime(esatto, (passato, passato))
        self.assertEqual(main.find_presentation_file(self.radice), esatto)

    def test_nome_personalizzato_viene_trovato(self) -> None:
        import main

        atteso = self._tocca("mie_slide.pptx")
        self.assertEqual(main.find_presentation_file(self.radice), atteso)

    def test_doppio_download_viene_trovato(self) -> None:
        import main

        atteso = self._tocca("presentazione (1).pptx")
        self.assertEqual(main.find_presentation_file(self.radice), atteso)

    def test_fra_piu_liberi_vince_il_piu_recente(self) -> None:
        import main

        vecchio = self._tocca("vecchie.pptx")
        vecchio.touch()
        passato = time.time() - 100
        os.utime(vecchio, (passato, passato))
        recente = self._tocca("nuove.pdf")
        self.assertEqual(main.find_presentation_file(self.radice), recente)

    def test_senza_presentazioni_torna_none(self) -> None:
        import main

        self._tocca("podcast.m4a")
        self.assertIsNone(main.find_presentation_file(self.radice))

    def test_ignora_maiuscole_e_sottocartelle(self) -> None:
        import main

        sub = self.radice / "sub"
        sub.mkdir()
        (sub / "PRESENTAZIONE.PPTX").write_bytes(b"finto")
        # Solo un livello sopra: dentro le sottocartelle non si cerca.
        self.assertIsNone(main.find_presentation_file(self.radice))
        atteso = self._tocca("PRESENTAZIONE.PPTX")
        self.assertEqual(main.find_presentation_file(self.radice), atteso)


class TestMessaggioDiagnostico(unittest.TestCase):
    """Il messaggio di primo avvio dice QUALE manca e cosa c'e' nella cartella.

    Prima era un testo unico ("manca il materiale") per due casi diversi
    (presentazione assente o audio assente): dallo screenshot non si capiva
    quale ramo fosse scattato.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.radice = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _main_senza_rete(self, argv: list[str]) -> Any:
        """Esegue main() con bootstrap/setup/prefetch spenti (nessun download)."""
        import main

        with (
            mock.patch.object(config, "BASE_DIR", self.radice),
            mock.patch.object(main, "BASE_DIR", self.radice),
            mock.patch.object(main, "bootstrap"),
            mock.patch.object(main, "machine_setup"),
            mock.patch.object(main, "_prefetch_models"),
            self.assertLogs("slide2video", level="INFO") as catturati,
        ):
            main.main(
                [
                    "--no-update-check",
                    "--no-confirm",
                    "--no-auto-setup",
                    *argv,
                ]
            )
        return "\n".join(catturati.output)

    def test_senza_nulla_dice_che_mancano_entrambi(self) -> None:
        import main

        with mock.patch.object(main, "find_audio_file", return_value=None):
            uscita = self._main_senza_rete([])
        self.assertIn("entrambi i file", uscita)
        self.assertIn("non contiene alcun file", uscita)

    def test_con_sola_presentazione_libera_manca_l_audio(self) -> None:
        import main

        # .pdf e non .pptx: il .pptx passerebbe per la conversione (niente
        # LibreOffice qui) prima del controllo audio; la ricerca libera e'
        # esercitata comunque dal nome non standard.
        (self.radice / "mie_slide.pdf").write_bytes(b"finto")
        with mock.patch.object(main, "find_audio_file", return_value=None):
            uscita = self._main_senza_rete([])
        self.assertIn("Manca l'audio del podcast", uscita)
        self.assertIn("Presentazione trovata: mie_slide.pdf", uscita)

    def test_con_solo_audio_manca_la_presentazione_ed_elenca_i_file(self) -> None:
        # find_audio_file qui gira davvero: trova podcast.m4a e il ramo che
        # scatta e' solo "manca la presentazione".
        (self.radice / "podcast.m4a").write_bytes(b"finto")
        uscita = self._main_senza_rete([])
        self.assertIn("Manca la presentazione", uscita)
        self.assertIn("Audio trovato: podcast.m4a", uscita)
        self.assertIn('"podcast.m4a"', uscita)

    def test_materiale_nella_sottocartella_viene_segnalato(self) -> None:
        import main

        sub = self.radice / "vecchia"
        sub.mkdir()
        (sub / "presentazione.pptx").write_bytes(b"finto")
        (sub / "podcast.m4a").write_bytes(b"finto")
        with mock.patch.object(main, "find_audio_file", return_value=None):
            uscita = self._main_senza_rete([])
        self.assertIn("vecchia/presentazione.pptx", uscita)
        self.assertIn("Spostalo nella cartella qui sopra", uscita)

    def test_presentazione_libera_supera_la_validazione(self) -> None:
        """Il file dal nome libero arriva oltre la validazione, senza prefetch.

        `_file_hash` solleva un sentinella: se la validazione avesse fermato
        tutto, il prefetch sarebbe stato chiamato e l'hash mai toccato.
        """
        import main

        (self.radice / "mie_slide.pdf").write_bytes(b"finto")
        audio = self.radice / "voce.mp3"
        audio.write_bytes(b"finto")
        with (
            mock.patch.object(config, "BASE_DIR", self.radice),
            mock.patch.object(main, "BASE_DIR", self.radice),
            mock.patch.object(main, "bootstrap"),
            mock.patch.object(main, "machine_setup"),
            mock.patch.object(main, "find_audio_file", return_value=audio),
            mock.patch.object(main, "_prefetch_models") as prefetch,
            mock.patch.object(main, "_file_hash", side_effect=RuntimeError("VALIDAZIONE_SUPERATA")),
            self.assertRaisesRegex(RuntimeError, "VALIDAZIONE_SUPERATA"),
        ):
            main.main(["--no-update-check", "--no-confirm", "--no-auto-setup"])
        prefetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
