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
import subprocess
import sys
import threading
from pathlib import Path

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


class Finestra:
    """La finestra vera e propria (vive solo se c'è uno schermo)."""

    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext, ttk

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

        cornice = ttk.Frame(self.radice, padding=12)
        cornice.pack(fill=tk.BOTH, expand=True)

        ttk.Label(cornice, text="1. Scegli i file", font=("TkDefaultFont", 10, "bold")).pack(anchor=tk.W)
        self.presentazione = self._riga_file(cornice, "Presentazione:", PRESENTAZIONE_PREDEFINITA)
        self.audio = self._riga_file(cornice, "Audio:", AUDIO_PREDEFINITO)

        ttk.Label(cornice, text="2. Opzioni", font=("TkDefaultFont", 10, "bold")).pack(anchor=tk.W, pady=(8, 0))
        self.solo_cpu = tk.BooleanVar(value=False)
        self.riverifica = tk.BooleanVar(value=True)
        self.bozza = tk.BooleanVar(value=False)
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
        self.diario = scrolledtext.ScrolledText(cornice, height=14, state=tk.DISABLED)
        self.diario.pack(fill=tk.BOTH, expand=True)

        self.radice.protocol("WM_DELETE_WINDOW", self.chiudi)

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
