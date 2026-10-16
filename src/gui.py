#!/usr/bin/env python3
"""Finestra minimale per Sync Video (prototipo).

Non riscrive niente: raccoglie i file con finestre di dialogo, preme gli
stessi tasti di genera_video.bat e mostra il diario di bordo mentre lavora.
Solo tkinter (libreria standard): gira sul PC nudo dove gira lo ZIP.

La logica costruisci_comando() e' pura e testata (test_gui.py); la finestra
vive solo dentro main(), cosi' importare il modulo non apre niente (anche
senza schermo, come in CI).
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import cast

import verifica_pc

BASE_DIR = Path(__file__).resolve().parent.parent
MAIN_PY = Path(__file__).resolve().parent / "main.py"

PRESENTAZIONE_PREDEFINITA = "presentazione.pdf"
AUDIO_PREDEFINITO = "podcast.m4a"


def costruisci_comando(
    presentazione: str,
    audio: str,
    *,
    solo_cpu: bool = False,
    riverifica: bool = True,
    bozza: bool = False,
) -> list[str]:
    """Argomenti per src/main.py dagli scelte della finestra.

    Stessi default di genera_video.bat (niente controlli aggiornamenti,
    niente conferme, niente LLM, motore ffmpeg). Solleva ValueError se i
    file non esistono: meglio un riquadro subito che un traceback dopo.
    """
    pdf = Path(presentazione)
    if not pdf.is_absolute():
        pdf = BASE_DIR / pdf
    if not pdf.is_file():
        raise ValueError(f"presentazione non trovata: {presentazione}")
    aud = Path(audio)
    if not aud.is_absolute():
        aud = BASE_DIR / aud
    if not aud.is_file():
        raise ValueError(f"audio non trovato: {audio}")
    comando = [
        sys.executable,
        str(MAIN_PY),
        "--no-update-check",
        "--no-confirm",
        "--llm",
        "off",
        "--engine",
        "ffmpeg",
        "--pdf",
        str(pdf),
        "--audio",
        str(aud),
    ]
    if solo_cpu:
        comando += ["--transcriber", "whisper"]
    if riverifica:
        comando += ["--verify-video"]
    if bozza:
        comando += ["--whisper-model", "tiny"]
    return comando


def _fase_da_riga(riga: str) -> str | None:
    """Fase in parole semplici dalle righe di diario (None = ignora)."""
    minuscola = riga.lower()
    if "ocr" in minuscola and ("slide" in minuscola or "recuper" in minuscola):
        return "Leggo le slide…"
    if "trascrizione" in minuscola:
        return "Ascolto l'audio e lo trascrivo…"
    if "sincronizzazione" in minuscola or "timeline" in minuscola:
        return "Allineo slide e parlato…"
    if "video" in minuscola and (
        "gener" in minuscola or "mont" in minuscola or "ffmpeg" in minuscola
    ):
        return "Monto il video…"
    if "verifica" in minuscola and "frame" in minuscola:
        return "Controllo il video finito…"
    if "il video" in minuscola and "pronto" in minuscola:
        return "Pronto!"
    if "manca" in minuscola and "materiale" in minuscola:
        return "Manca il materiale: leggi il messaggio qui sotto."
    if "errore" in minuscola or "error" in minuscola or "traceback" in minuscola:
        return "C'è stato un problema: leggi in fondo al diario."
    return None


def _pagine_pdf(percorso: Path) -> int | None:
    """Pagine della presentazione, o None (libreria assente o file rotto)."""
    try:
        import fitz
    except ImportError:
        return None
    try:
        doc = fitz.open(percorso)
        try:
            return int(doc.page_count)
        finally:
            doc.close()
    except Exception:  # noqa: BLE001 - file rotto o illeggibile: non e' un errore
        return None


def _durata_audio(percorso: Path) -> float | None:
    """Durata in secondi via ffprobe, o None (assente o illeggibile)."""
    if shutil.which("ffprobe") is None:
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(percorso)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return float(out.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _minuti(secondi: float) -> str:
    """774.8 -> '12:54'."""
    return f"{int(secondi // 60)}:{int(secondi % 60):02d}"


def _gb(byte: int) -> str:
    """Byte in forma breve per la scheda."""
    return f"{byte / 1024**3:.1f} GB" if byte >= 1024**3 else f"{byte / 1024**2:.0f} MB"


def _risolvi(nome: str, base: Path) -> Path:
    percorso = Path(nome)
    return percorso if percorso.is_absolute() else base / percorso


def riepilogo_materiali(
    presentazione: str, audio: str, base: Path = BASE_DIR
) -> dict[str, object]:
    """Scheda dei file prima di partire: conteggi o cosa manca.

    Non solleva mai: ogni misura e' protetta, i buchi diventano None.
    """
    pdf = _risolvi(presentazione, base)
    aud = _risolvi(audio, base)
    pdf_ok, aud_ok = pdf.is_file(), aud.is_file()
    pagine = _pagine_pdf(pdf) if pdf_ok else None
    durata = _durata_audio(aud) if aud_ok else None
    try:
        liberi = shutil.disk_usage(base).free
    except OSError:
        liberi = None
    if pdf_ok and aud_ok:
        pezzi = []
        pezzi.append(f"{pagine} slide" if pagine else "presentazione ok")
        pezzi.append(f"{_minuti(durata)} di audio" if durata else "audio ok")
        if liberi is not None:
            pezzi.append(f"{_gb(liberi)} liberi")
        riga = " • ".join(pezzi)
    else:
        mancanti = []
        if not pdf_ok:
            mancanti.append("la presentazione")
        if not aud_ok:
            mancanti.append("l'audio")
        riga = "Manca " + " e ".join(mancanti)
    return {
        "pdf_ok": pdf_ok,
        "audio_ok": aud_ok,
        "pagine": pagine,
        "durata_audio": durata,
        "byte_liberi": liberi,
        "pronto": pdf_ok and aud_ok,
        "riga": riga,
    }


def stato_ultima_run(base: Path = BASE_DIR) -> dict[str, object] | None:
    """L'ultimo video in numeri (None se mai fatta una run)."""
    import json

    try:
        report = json.loads((base / ".cache" / "sync_report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = None
    video = base / "video_finale.mp4"
    try:
        video_byte = video.stat().st_size if video.is_file() else None
    except OSError:
        video_byte = None
    if report is None and video_byte is None:
        return None
    segmenti = report.get("segments", []) if isinstance(report, dict) else []
    ancore = report.get("anchors", {}) if isinstance(report, dict) else {}
    frame = report.get("frame_check", {}) if isinstance(report, dict) else {}
    verificato = bool(frame) and not frame.get("mismatches") and frame.get("checked", 0) > 0
    return {
        "slide": len(segmenti),
        "durata_audio": report.get("audio_duration") if isinstance(report, dict) else None,
        "ancore": (
            f"{ancore.get('anchored', '?')}/{ancore.get('transitions', '?')} ancorati"
            if isinstance(ancore, dict)
            else "n/d"
        ),
        "verificato": verificato,
        "video_byte": video_byte,
    }


def storico_run(base: Path = BASE_DIR) -> list[dict[str, object]]:
    """Ultime 5 run da timing_history.jsonl (le piu' recenti prima)."""
    import json

    try:
        righe = (base / ".cache" / "timing_history.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    voci: list[dict[str, object]] = []
    for riga in righe[-5:]:
        try:
            dati = json.loads(riga)
            if isinstance(dati, dict):
                voci.append({"quando": dati.get("ts", "?"), "totale": dati.get("total")})
        except ValueError:
            continue
    return voci[::-1]


def _percorso_impostazioni() -> Path:
    """Dove ricordare ultime scelte (fuori dal progetto e dalla cache)."""
    casa = Path(os.environ.get("APPDATA", str(Path.home())))
    return casa / "sync-video" / "impostazioni.json"


def impostazioni_carica() -> dict[str, object]:
    """Ultime scelte dell'utente, o {} (mai un errore)."""
    import json

    try:
        dati = json.loads(_percorso_impostazioni().read_text(encoding="utf-8"))
        return dati if isinstance(dati, dict) else {}
    except (OSError, ValueError):
        return {}


def impostazioni_salva(dati: dict[str, object]) -> None:
    """Ricorda le scelte (ignora gli errori: e' solo comodita')."""
    import json

    try:
        percorso = _percorso_impostazioni()
        percorso.parent.mkdir(parents=True, exist_ok=True)
        percorso.write_text(json.dumps(dati, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def righe_verifica(base: Path = BASE_DIR) -> list[tuple[str, str, str, str]]:
    """Le 11 prove di verifica_pc come righe (stato, nome, dettaglio, rimedio)."""
    rep = verifica_pc.Relazione()
    verifica_pc._esegui_prove(rep, verifica_pc._prove(base))
    return [(e.stato, e.nome, e.dettaglio, e.rimedio) for e in rep.esiti]


class Finestra:
    """La finestra vera e propria (vive solo se c'è uno schermo)."""

    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk = tk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.radice = tk.Tk()
        self.radice.title("Sync Video: crea il video")
        self.radice.geometry("640x560")
        self.processo: subprocess.Popen[str] | None = None
        self.in_corso = False
        self.coda: queue.Queue[str] = queue.Queue()

        try:
            from config import versione_programma

            versione = versione_programma()
        except Exception:  # noqa: BLE001 - la versione non deve mai bloccare la finestra
            versione = "sconosciuta"

        taccuino = ttk.Notebook(self.radice)
        taccuino.pack(fill=tk.BOTH, expand=True)
        scheda_crea = ttk.Frame(taccuino, padding=12)
        scheda_pc = ttk.Frame(taccuino, padding=12)
        scheda_video = ttk.Frame(taccuino, padding=12)
        taccuino.add(scheda_crea, text="Crea video")
        taccuino.add(scheda_pc, text="Stato PC")
        taccuino.add(scheda_video, text="Ultimo video")
        self._scheda_crea(scheda_crea, versione)
        self._scheda_pc(scheda_pc)
        self._scheda_video(scheda_video)

        self.radice.protocol("WM_DELETE_WINDOW", self.chiudi)

    def _scheda_crea(self, cornice, versione: str) -> None:  # type: ignore[no-untyped-def]
        import tkinter as tk
        from tkinter import scrolledtext, ttk

        scelte = impostazioni_carica()
        ttk.Label(cornice, text="1. Scegli i file", font=("TkDefaultFont", 10, "bold")).pack(anchor=tk.W)
        self.presentazione = self._riga_file(
            cornice, "Presentazione:", str(scelte.get("presentazione", PRESENTAZIONE_PREDEFINITA))
        )
        self.audio = self._riga_file(
            cornice, "Audio:", str(scelte.get("audio", AUDIO_PREDEFINITO))
        )
        self.scheda_file = ttk.Label(cornice, text="")
        self.scheda_file.pack(anchor=tk.W)
        for campo in (self.presentazione, self.audio):
            campo.bind("<FocusOut>", lambda _e: self._aggiorna_scheda())

        ttk.Label(cornice, text="2. Opzioni", font=("TkDefaultFont", 10, "bold")).pack(anchor=tk.W, pady=(8, 0))
        self.solo_cpu = tk.BooleanVar(value=bool(scelte.get("solo_cpu", False)))
        self.riverifica = tk.BooleanVar(value=bool(scelte.get("riverifica", True)))
        self.bozza = tk.BooleanVar(value=bool(scelte.get("bozza", False)))
        ttk.Checkbutton(
            cornice, text="Solo CPU (se la trascrizione si blocca)", variable=self.solo_cpu
        ).pack(anchor=tk.W)
        ttk.Checkbutton(cornice, text="Ricontrolla il video finito", variable=self.riverifica).pack(anchor=tk.W)
        ttk.Checkbutton(cornice, text="Bozza veloce (meno precisa)", variable=self.bozza).pack(anchor=tk.W)

        ttk.Label(cornice, text="3. Via", font=("TkDefaultFont", 10, "bold")).pack(anchor=tk.W, pady=(8, 0))
        bottoni = ttk.Frame(cornice)
        bottoni.pack(fill=tk.X)
        self.pulsante_via = ttk.Button(bottoni, text="Crea il video", command=self.avvia)
        self.pulsante_via.pack(side=tk.LEFT)
        self.pulsante_stop = ttk.Button(bottoni, text="Ferma", command=self.ferma, state=tk.DISABLED)
        self.pulsante_stop.pack(side=tk.LEFT, padx=(6, 0))
        self.pulsante_cartella = ttk.Button(bottoni, text="Apri cartella", command=self.apri_cartella)
        self.pulsante_cartella.pack(side=tk.LEFT, padx=(6, 0))

        self.stato = ttk.Label(cornice, text=f"Pronto. Versione {versione}.")
        self.stato.pack(anchor=tk.W, pady=(6, 0))
        self.barra = ttk.Progressbar(cornice, mode="indeterminate")
        self.barra.pack(fill=tk.X, pady=(2, 6))
        self.diario = scrolledtext.ScrolledText(cornice, height=12, state=tk.DISABLED)
        self.diario.pack(fill=tk.BOTH, expand=True)
        self._aggiorna_scheda()

    def _scheda_pc(self, cornice) -> None:  # type: ignore[no-untyped-def]
        import tkinter as tk
        from tkinter import scrolledtext, ttk

        ttk.Label(
            cornice,
            text="Controlla se questo computer può fare un video, prima di aspettare.",
            wraplength=560,
        ).pack(anchor=tk.W)
        self.pulsante_pc = ttk.Button(cornice, text="Controlla il computer", command=self.controlla_pc)
        self.pulsante_pc.pack(anchor=tk.W, pady=(6, 6))
        self.esito_pc = scrolledtext.ScrolledText(cornice, height=16, state=tk.DISABLED)
        self.esito_pc.pack(fill=tk.BOTH, expand=True)
        for nome, colore in (("ok", "dark green"), ("ko", "red"), ("avv", "dark orange")):
            self.esito_pc.tag_config(nome, foreground=colore)

    def _scheda_video(self, cornice) -> None:  # type: ignore[no-untyped-def]
        import tkinter as tk
        from tkinter import ttk

        self.info_video = ttk.Label(cornice, text="", wraplength=560)
        self.info_video.pack(anchor=tk.W)
        self.storico = ttk.Label(cornice, text="", wraplength=560)
        self.storico.pack(anchor=tk.W, pady=(4, 0))
        bottoni = ttk.Frame(cornice)
        bottoni.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(bottoni, text="Aggiorna", command=self._aggiorna_video).pack(side=tk.LEFT)
        ttk.Button(bottoni, text="Guarda il video", command=self.guarda_video).pack(side=tk.LEFT, padx=(6, 0))
        self.pulsante_diag = ttk.Button(bottoni, text="Prepara diagnostica", command=self.prepara_diagnostica)
        self.pulsante_diag.pack(side=tk.LEFT, padx=(6, 0))
        self._aggiorna_video()

    def _aggiorna_scheda(self) -> None:
        """Scheda dei file scelti (conteggi o cosa manca)."""
        scheda = riepilogo_materiali(
            self.presentazione.get().strip(), self.audio.get().strip(), BASE_DIR
        )
        self.scheda_file.configure(text=str(scheda["riga"]))

    def _lancia_lavoro(self, lavoro, fatto) -> None:  # type: ignore[no-untyped-def]
        """Esegue lavoro() nel thread e chiama fatto(esito) nella finestra."""
        contenitore: dict[str, object] = {}

        def esegui() -> None:
            try:
                contenitore["ok"] = lavoro()
            except Exception as e:  # noqa: BLE001 - l'errore va mostrato, non lanciato
                contenitore["ko"] = e

        threading.Thread(target=esegui, daemon=True).start()
        self._attendi(contenitore, fatto)

    def _attendi(self, contenitore: dict[str, object], fatto) -> None:  # type: ignore[no-untyped-def]
        if "ok" in contenitore or "ko" in contenitore:
            fatto(contenitore)
        else:
            self.radice.after(200, lambda: self._attendi(contenitore, fatto))

    def controlla_pc(self) -> None:
        import tkinter as tk

        self.pulsante_pc.configure(state=tk.DISABLED)
        self._scrivi_pc("Controllo in corso…\n")
        self._lancia_lavoro(lambda: righe_verifica(BASE_DIR), self._mostra_pc)

    def _scrivi_pc(self, testo: str) -> None:
        self.esito_pc.configure(state="normal")
        self.esito_pc.delete("1.0", "end")
        self.esito_pc.insert("end", testo)
        self.esito_pc.configure(state="disabled")

    def _mostra_pc(self, contenitore: dict[str, object]) -> None:
        import tkinter as tk

        self.pulsante_pc.configure(state=tk.NORMAL)
        if "ko" in contenitore:
            self._scrivi_pc(f"Controllo fallito: {contenitore['ko']}\n")
            return
        righe = ""
        dati = cast("list[tuple[str, str, str, str]]", contenitore["ok"])
        for stato, nome, dettaglio, rimedio in dati:
            marca = {"OK": "ok", "KO": "ko"}.get(stato, "avv")
            righe += f"[{marca}] {nome}: {dettaglio}\n"
            if rimedio and stato != "OK":
                righe += f"      → {rimedio}\n"
        ko = sum(1 for r in dati if r[0] == "KO")
        righe += "\nPuò fare un video.\n" if ko == 0 else "\nManca qualcosa: leggi le righe KO.\n"
        self.esito_pc.configure(state="normal")
        self.esito_pc.delete("1.0", "end")
        for riga in righe.splitlines(keepends=True):
            tinta = "ok" if riga.startswith("[ok]") else ("ko" if riga.startswith("[ko]") else None)
            if tinta is None:
                self.esito_pc.insert("end", riga)
            else:
                self.esito_pc.insert("end", riga, tinta)
        self.esito_pc.configure(state="disabled")

    def _aggiorna_video(self) -> None:
        info = stato_ultima_run(BASE_DIR)
        if info is None:
            self.info_video.configure(text="Nessun video ancora: crea il primo dalla scheda Crea video.")
        else:
            grezza = info.get("durata_audio")
            durata = _minuti(float(grezza)) if isinstance(grezza, (int, float)) else "?"
            verificato = "sì" if info.get("verificato") else "da controllare"
            self.info_video.configure(
                text=f"Ultimo video: {info.get('slide', '?')} slide, {durata} di parlato, "
                f"ancore {info.get('ancore', '?')}, verifica finale: {verificato}."
            )
        righe = storico_run(BASE_DIR)
        if righe:
            testo = "Run precedenti:\n" + "\n".join(
                f"  • {v.get('quando', '?')}: {v.get('totale', '?')}s" for v in righe
            )
        else:
            testo = "Nessuna run registrata."
        self.storico.configure(text=testo)

    def guarda_video(self) -> None:
        video = BASE_DIR / "video_finale.mp4"
        if not video.is_file():
            self.messagebox.showinfo("Video", "Ancora nessun video: crealo prima.")
            return
        try:
            os.startfile(str(video))  # type: ignore[attr-defined]
        except OSError as e:
            self.messagebox.showerror("Video", str(e))

    def prepara_diagnostica(self) -> None:
        self.pulsante_diag.configure(state="disabled")
        self.info_video.configure(text="Preparo la diagnostica…")

        def lavoro() -> str:
            import diagnostica

            return str(diagnostica.scrivi_bundle(BASE_DIR))

        def fatto(contenitore: dict[str, object]) -> None:
            self.pulsante_diag.configure(state="normal")
            self._aggiorna_video()
            if "ko" in contenitore:
                self.messagebox.showerror("Diagnostica", f"Non riesco: {contenitore['ko']}")
            else:
                self.messagebox.showinfo("Diagnostica", f"Invia questo file:\n{contenitore['ok']}")

        self._lancia_lavoro(lavoro, fatto)

    def _riga_file(self, genitore, etichetta: str, predefinito: str):  # type: ignore[no-untyped-def]
        from tkinter import filedialog as _fd
        from tkinter import ttk

        riga = ttk.Frame(genitore)
        riga.pack(fill="x", pady=2)
        ttk.Label(riga, text=etichetta, width=14).pack(side="left")
        campo = ttk.Entry(riga)
        campo.pack(side="left", fill="x", expand=True)
        campo.insert(0, predefinito)

        def sfoglia() -> None:
            scelto = _fd.askopenfilename(initialdir=str(BASE_DIR))
            if scelto:
                campo.delete(0, "end")
                campo.insert(0, scelto)

        ttk.Button(riga, text="Sfoglia…", command=sfoglia).pack(side="left", padx=(6, 0))
        return campo

    def _scrivi(self, testo: str) -> None:
        self.diario.configure(state="normal")
        self.diario.insert("end", testo)
        self.diario.see("end")
        self.diario.configure(state="disabled")

    def avvia(self) -> None:
        if self.in_corso:
            return
        impostazioni_salva(
            {
                "presentazione": self.presentazione.get().strip(),
                "audio": self.audio.get().strip(),
                "solo_cpu": self.solo_cpu.get(),
                "riverifica": self.riverifica.get(),
                "bozza": self.bozza.get(),
            }
        )
        self._aggiorna_scheda()
        try:
            comando = costruisci_comando(
                self.presentazione.get().strip(),
                self.audio.get().strip(),
                solo_cpu=self.solo_cpu.get(),
                riverifica=self.riverifica.get(),
                bozza=self.bozza.get(),
            )
        except ValueError as e:
            self.messagebox.showerror("Manca qualcosa", str(e))
            return
        self._scrivi(f"$ {' '.join(comando)}\n")
        try:
            self.processo = subprocess.Popen(
                comando,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(BASE_DIR),
            )
        except OSError as e:
            self.messagebox.showerror("Avvio fallito", str(e))
            return
        self.in_corso = True
        self.pulsante_via.configure(state="disabled")
        self.pulsante_stop.configure(state="normal")
        self.barra.start(12)
        threading.Thread(target=self._leggi, daemon=True).start()
        self.radice.after(150, self._svuota_coda)

    def _leggi(self) -> None:
        """Nel thread secondario: legge il diario e lo accoda (mai Tk da qui)."""
        assert self.processo is not None and self.processo.stdout is not None
        for riga in self.processo.stdout:
            self.coda.put(riga)
        self.processo.wait()
        self.coda.put(f"\n[uscita con codice {self.processo.returncode}]\n")
        self.coda.put(None)  # type: ignore[arg-type]

    def _svuota_coda(self) -> None:
        """Nel thread della finestra: svuota la coda in sicurezza."""
        while True:
            try:
                riga = self.coda.get_nowait()
            except queue.Empty:
                break
            if riga is None:
                self._finito()
                return
            self._scrivi(riga)
            fase = _fase_da_riga(riga)
            if fase is not None:
                self.stato.configure(text=fase)
        if self.in_corso:
            self.radice.after(150, self._svuota_coda)

    def _finito(self) -> None:
        codice = self.processo.returncode if self.processo else -1
        self.in_corso = False
        self.barra.stop()
        self.pulsante_via.configure(state="normal")
        self.pulsante_stop.configure(state="disabled")
        if codice == 0:
            self.stato.configure(text="Finito: premi Apri cartella per vedere il video.")
        else:
            self.stato.configure(text=f"Si è fermato (codice {codice}): leggi in fondo al diario.")

    def ferma(self) -> None:
        if self.processo is not None and self.in_corso:
            self.processo.terminate()
            self._scrivi("\n[fermato da te]\n")

    def apri_cartella(self) -> None:
        try:
            os.startfile(str(BASE_DIR))  # type: ignore[attr-defined]
        except OSError as e:
            self.messagebox.showerror("Cartella", str(e))

    def apri(self) -> None:
        self.radice.mainloop()

    def chiudi(self) -> None:
        try:
            if self.processo is not None and self.in_corso:
                self.processo.terminate()
        except OSError:
            pass
        self.radice.destroy()


def main() -> int:
    try:
        import tkinter
    except ImportError:
        print("Tkinter non disponibile in questo Python.", file=sys.stderr)
        return 2
    try:
        finestra = Finestra()
    except tkinter.TclError as e:
        print(f"Nessuno schermo disponibile ({e}). Usa genera_video.bat.", file=sys.stderr)
        return 2
    finestra.apri()
    return 0


if __name__ == "__main__":
    sys.exit(main())
