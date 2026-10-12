#!/usr/bin/env python3
"""
FASE 1 — Estrazione slide da PDF + OCR parallelo.
Il rendering PDF usa multiprocessing (PyMuPDF non è thread-safe).
"""

import contextlib
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import pymupdf as fitz  # PyMuPDF (alias deprecato importato come compat)
from PIL import Image

from config import log, tqdm

# Candidati percorsi LibreOffice (Windows + Unix)
_SOFFICE_CANDIDATES = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/usr/local/bin/soffice",
    "/opt/libreoffice/program/soffice",
]

# LibreOffice installato SENZA diritti di amministratore va in
# %LOCALAPPDATA%\Programs\LibreOffice, non in Program Files. Prima non era
# fra i candidati: un collega che lo installa senza privilegi riceverebbe
# "nessun converter trovato" mentre LibreOffice ce l'ha, a due passi da lui.
# Non basta `shutil.which`: l'installer non mette soffice nel PATH.
_SOFFICE_PER_UTENTE = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "LibreOffice" / "program" / "soffice.exe"
)

# Candidati percorsi ONLYOFFICE (x2t converter)
_ONLYOFFICE_CANDIDATES = [
    r"C:\Program Files\ONLYOFFICE\DesktopEditors\converter\x2t.exe",
    r"C:\Program Files (x86)\ONLYOFFICE\DesktopEditors\converter\x2t.exe",
    "/usr/bin/x2t",
    "/opt/onlyoffice/desktopeditors/converter/x2t",
]

# Candidati GenOffice. NON ha un converter in stile x2t: dentro l'app c'e' una
# CLI headless (resources\cli\genoffice.cjs) eseguita dal runtime Electron
# dell'app stessa. Serve il .exe OLTRE allo script .cjs: senza GenOffice.exe la
# CLI non parte, e per questo i due file sono verificati insieme.
_GENOFFICE_CANDIDATES = [
    r"C:\Program Files\GenOffice\GenOffice.exe",
    r"C:\Program Files (x86)\GenOffice\GenOffice.exe",
    "/opt/genoffice/GenOffice",
    "/usr/bin/genoffice",
]

# Lo script CLI, relativo alla radice di installazione trovata sopra.
_GENOFFICE_CLI_REL = Path("resources") / "cli" / "genoffice.cjs"


def _find_soffice() -> str | None:
    """Cerca un eseguibile LibreOffice soffice utilizzabile per la conversione."""
    env = os.environ.get("SOFFICE_PATH")
    if env and Path(env).exists():
        return env
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for cand in _SOFFICE_CANDIDATES:
        if Path(cand).exists():
            return cand
    if _SOFFICE_PER_UTENTE.exists():
        return str(_SOFFICE_PER_UTENTE)
    return None


def _find_onlyoffice() -> str | None:
    """Cerca il converter ONLYOFFICE x2t utilizzabile per la conversione."""
    env = os.environ.get("ONLYOFFICE_X2T_PATH")
    if env and Path(env).exists():
        return env
    found = shutil.which("x2t")
    if found:
        return found
    for cand in _ONLYOFFICE_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


PRESENTATION_SUFFIXES = {".ppt", ".pptx"}


def _find_genoffice() -> tuple[str, str] | None:
    """(GenOffice.exe, percorso CLI) se la CLI headless e' installata.

    Ritorna entrambi perche' servono insieme: il .exe da' il runtime Node,
    lo .cjs contiene il codice della CLI. Se uno dei due manca non c'e'
    conversione possibile, quindi si restituisce None invece di un percorso
    che fallirebbe solo a runtime.
    """
    env_app = os.environ.get("GENOFFICE_PATH")
    if env_app and Path(env_app).exists():
        cli_env = Path(env_app).parent / _GENOFFICE_CLI_REL
        return (env_app, str(cli_env)) if cli_env.exists() else None
    found = shutil.which("genoffice")
    if found:
        # Il launcher sul PATH puo' essere lo script .cjs o il .cmd wrapper.
        cand = Path(found)
        cli_path = cand if cand.suffix == ".cjs" else cand.parent / "genoffice.cjs"
        app_path = cand.parent.parent.parent / "GenOffice.exe"
        if cli_path.exists() and app_path.exists():
            return (str(app_path), str(cli_path))
    for cand_str in _GENOFFICE_CANDIDATES:
        p = Path(cand_str)
        if p.exists():
            cli_file = p.parent / _GENOFFICE_CLI_REL
            if cli_file.exists():
                return (cand_str, str(cli_file))
    return None


def _find_powerpoint() -> bool:
    """True se Microsoft PowerPoint e' installato (COM utilizzabile).

    Si interroga il REGISTRO, non l'applicazione: aprire COM avvia PowerPoint,
    il che costerebbe qualche secondo e potrebbe lasciare una finestra aperta
    solo per scoprire che Office non c'e'. Basta il ProgID: se la chiave
    esiste, la sua sottochiave CLSID esiste per forza.
    """
    if os.environ.get("SYNC_VIDEO_NO_OFFICE") == "1":
        return False
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "PowerPoint.Application"):
            return True
    except OSError:
        return False


def _office_converter_script() -> Path:
    """Percorso dello script PowerShell che pilota PowerPoint via COM."""
    return Path(__file__).resolve().parent / "pptx_to_pdf_office.ps1"


def _file_md5(path: Path, chunk: int = 1024 * 1024) -> str:
    """MD5 del contenuto di un file (streaming, non carica in memoria)."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _pptx_fallback_to_pdf(ppt_path: Path, pdf_path: Path) -> bool:
    """Fallback puro Python: PPTX -> PDF via python-pptx + PyMuPDF.

    Estrae testo e immagini dalle slide e genera un PDF 16:9 (1280x720).
    Non è fedele al 100% come LibreOffice/ONLYOFFICE, ma sblocca la pipeline
    quando il converter nativo fallisce (es. x2t TypeError su file complessi).
    Ritorna True se il PDF è stato creato con successo.
    """
    try:
        from pptx import Presentation  # type: ignore
    except ImportError:
        return False
    try:
        prs = Presentation(str(ppt_path))
    except Exception as e:
        log.debug("   Fallback PPTX->PDF: apertura fallita: %s", e)
        return False
    try:
        doc = fitz.open()
        for idx, slide in enumerate(prs.slides):
            # Pagina 16:9 come il video finale (1280x720)
            page = doc.new_page(width=1280, height=720)
            # Raccogli testo
            texts: list[str] = []
            images_to_draw: list[tuple[bytes, str]] = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    t = shape.text.strip()
                    if t:
                        texts.append(t)
                if shape.has_table:
                    for row in shape.table.rows:
                        for cell in row.cells:
                            if cell.text.strip():
                                texts.append(cell.text.strip())
                # Immagini
                if shape.shape_type == 13:  # picture
                    try:
                        img_bytes = shape.image.blob
                        ext = shape.image.ext or "png"
                        images_to_draw.append((img_bytes, ext))
                    except Exception:
                        pass
            # Se ci sono immagini, prova a disegnarne la prima a tutta pagina
            if images_to_draw:
                try:
                    img_bytes, ext = images_to_draw[0]

                    # Fix RGBA -> RGB (fitz/x2t non gestisce alpha correttamente: rendeva bianco)
                    try:
                        pil_img = Image.open(io.BytesIO(img_bytes))
                        if pil_img.mode in ("RGBA", "LA", "PA"):
                            bg = Image.new("RGB", pil_img.size, (255, 255, 255))
                            if pil_img.mode == "RGBA":
                                bg.paste(pil_img, mask=pil_img.split()[3])
                            else:
                                bg.paste(pil_img)
                            buf = io.BytesIO()
                            bg.save(buf, format="PNG")
                            img_bytes = buf.getvalue()
                            ext = "png"
                    except Exception:
                        pass

                    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tf:
                        tf.write(img_bytes)
                        tmp_img = tf.name
                    try:
                        page.insert_image(page.rect, filename=tmp_img, keep_proportion=True, overlay=False)
                    finally:
                        Path(tmp_img).unlink(missing_ok=True)
                    # Se c'era testo oltre l'immagine, aggiungilo in basso
                    if texts:
                        text = " | ".join(texts)[:300]
                        page.insert_textbox(
                            fitz.Rect(20, 680, 1260, 710),
                            text,
                            fontsize=8,
                            color=(0.3, 0.3, 0.3),
                            align=1,
                        )
                except Exception as e:
                    log.debug("   Fallback immagine slide %d fallita: %s", idx + 1, e)
            # Nessuna immagine: renderizza il testo centrato
            if not images_to_draw:
                full_text = "\n".join(texts).strip() or f"[Slide {idx+1} - contenuto visivo]"
                # Titolo slide
                rect = fitz.Rect(40, 40, 1240, 680)
                page.insert_textbox(
                    rect,
                    full_text[:2000],
                    fontsize=18,
                    color=(0.1, 0.1, 0.1),
                    align=0,
                    fontname="helv",
                )
            # Numero slide in basso a destra
            page.insert_text(fitz.Point(1220, 710), f"{idx+1}/{len(prs.slides)}", fontsize=8, color=(0.5, 0.5, 0.5))
        doc.save(str(pdf_path))
        doc.close()
        return pdf_path.exists() and pdf_path.stat().st_size > 0
    except Exception as e:
        log.debug("   Fallback PPTX->PDF eccezione: %s", e)
        with contextlib.suppress(Exception):
            doc.close()
        return False


def convert_presentation_to_pdf(ppt_path: Path, out_dir: Path, *, fallback_senza_grafica: bool = False) -> Path:
    """Converte una presentazione PPT/PPTX in PDF usando un converter nativo.

    Ordine di preferenza:
      1. LibreOffice ``soffice`` (se installato)
      2. GenOffice CLI headless (se installata) — conserva grafica e layout
      3. Microsoft PowerPoint via COM (se installato) — per i PC con solo Office
      4. ONLYOFFICE ``x2t`` (se installato) — alternativa senza LibreOffice
      5. Fallback Python puro (python-pptx + PyMuPDF), SOLO con
         ``fallback_senza_grafica=True``

    GenOffice sta prima di ONLYOFFICE perche' e' piu' probabile trovarsi
    gia' installato come editor predefinito, e il suo convertitore mantiene
    il layout (il fallback Python no: perdo' forme, colori e sfondi).
    PowerPoint sta per terzo perche' e' l'unico dei quattro che richiede una
    licenza: e' l'ultima risorsa per i PC che hanno Office e nient'altro.

    `fallback_senza_grafica` NON e' il default: senza converter nativi la
    funzione solleva RuntimeError invece di ricostruire le slide a mano dal
    solo testo. Il video cosi' prodotto sembra normale ma e' peggiore, e
    niente lo segnala. Vedi la fine della funzione.
    """
    soffice = _find_soffice()
    genoffice = _find_genoffice()
    onlyoffice = _find_onlyoffice()
    powerpoint = _find_powerpoint()
    has_converter = soffice or genoffice or onlyoffice or powerpoint
    # Senza converter il fallback non parte da solo: vedi la fine della funzione.
    if not has_converter and not fallback_senza_grafica:
        log.warning(
            "   Nessun converter nativo (LibreOffice/GenOffice/PowerPoint/ONLYOFFICE) "
            "trovato. Il programma si ferma invece di produrre slide ricostruite "
            "senza grafica, che peggiorerebbero la sincronizzazione."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / (ppt_path.stem + ".pdf")

    # Riusa il PDF convertito solo se il file sorgente è INVARIATO (confronto per
    # contenuto, non per data di modifica): una copia/caricamento del .pptx può
    # avere un mtime più vecchio del PDF in cache, cosa che faceva riusare un PDF
    # stantio. Il marker a lato memorizza l'MD5 del sorgente convertito.
    marker_path = out_dir / (ppt_path.stem + ".src_md5")
    if pdf_path.exists() and marker_path.exists():
        try:
            if marker_path.read_text(encoding="ascii").strip() == _file_md5(ppt_path):
                log.info("   -> PDF già presente (riusato): %s", pdf_path)
                return pdf_path
        except OSError:
            pass
        log.info("   -> PDF in cache scaduto (sorgente cambiato), riconverto...")

    ext_upper = ppt_path.suffix.upper().lstrip(".")
    # Rimuovi il PDF (e il marker) del tentativo precedente PRIMA di convertire.
    # Il successo qui si valuta dalla presenza del file, quindi lasciare il
    # vecchio in posto significa che un converter fallito "riusa" il PDF
    # stantio, aggiorna il marker con l'MD5 del sorgente NUOVO e da quel momento
    # ogni run successiva riusa quel PDF per sempre: slide vecchie nel video,
    # con la cache OCR riusata perche' l'hash del PDF non e' cambiato, e un log
    # che dice "PDF convertito".
    # Nota il fallback Python usa lo stesso pdf_path di output: rimuovere qui
    # vale anche per lui, che altrimenti ha lo stesso problema.
    for stale in (pdf_path, marker_path):
        with contextlib.suppress(OSError):
            stale.unlink()

    # Prova converter nativo se disponibile
    if has_converter:
        converter: str
        cmd: list[str]
        if soffice:
            converter = "LibreOffice"
            cmd = [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(ppt_path)]
            env_extra: dict[str, str] = {}
        elif genoffice:
            converter = "GenOffice"
            cmd = [
                genoffice[0],
                genoffice[1],
                "convert",
                str(ppt_path),
                "--to",
                "pdf",
                "--out",
                str(pdf_path),
                "--force",
            ]
            # La CLI non e' un eseguibile autonomo: e' uno script Node che gira
            # sul runtime Electron dell'app. Senza questa variabile Electron
            # aprirebbe la GUI e il processo non restituirebbe l'output.
            env_extra = {"ELECTRON_RUN_AS_NODE": "1"}
        elif powerpoint:
            converter = "PowerPoint"
            # Non c'e' un equivalente di --headless per PowerPoint: l'unica
            # via e' pilotarlo via COM, e lo fa uno script PowerShell. Non
            # richiede pywin32 perche' PowerShell parla COM nativamente.
            ps = shutil.which("powershell") or shutil.which("pwsh")
            cmd = [
                ps or "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(_office_converter_script()),
                str(ppt_path),
                str(pdf_path),
            ]
            env_extra = {}
        else:
            converter = "ONLYOFFICE"
            cmd = [onlyoffice, str(ppt_path), str(pdf_path)]  # type: ignore[list-item]
            env_extra = {}
        log.info("   Conversione %s -> PDF (%s)...", ext_upper, converter)
        # PowerPoint via COM avvia l'applicazione: su un deck grosso, o su un
        # PC lento, 180s non bastano e il timeout ucciderebbe la conversione
        # lasciando PowerPoint aperto. Gli altri sono headless e veloci.
        timeout = 300 if converter == "PowerPoint" else 180
        proc = None
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env={**os.environ, **env_extra} if env_extra else None,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
        except subprocess.TimeoutExpired:
            log.warning("   Conversione %s->PDF timeout (%ds)", ext_upper, timeout)
        except OSError as e:
            # soffice/x2t presenti nel PATH ma non eseguibili (ACL, voce
            # spazzatura, architettura sbagliata, permessi). Senza questo
            # l'utente prende un traceback invece del messaggio costruito in
            # fondo, che spiega cosa provare.
            log.warning("   %s non eseguibile (%s)", converter, e)
        if proc is not None:
            # x2t ritorna exit !=0 anche quando produce il PDF con warning:
            # il successo si valuta dalla presenza del file, non dall'exit code.
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                if proc.returncode != 0:
                    log.debug(
                        "   %s warning (exit=%d): %s",
                        converter,
                        proc.returncode,
                        (proc.stderr or proc.stdout)[:500],
                    )
                marker_path.write_text(_file_md5(ppt_path), encoding="ascii")
                log.info("   -> PDF convertito: %s", pdf_path)
                return pdf_path
            log.warning(
                "   %s fallito (exit=%d): %s",
                converter,
                proc.returncode,
                (proc.stderr or proc.stdout or "")[:600],
            )
            log.warning("   Provo il prossimo converter...")

    # Fallback puro Python (non richiede LibreOffice/ONLYOFFICE).
    #
    # NON viene usato in automatico: ricostruisce le slide mettendo solo testo
    # e la prima immagine, quindi perde forme, colori, sfondi e layout. L'OCR
    # ne ricava molto meno testo, la sincronizzazione peggiora, e il video
    # esce lo stesso identico a prima: un green che nasconde un risultato
    # peggiore. Il progetto per questo non mette python-pptx tra le dipendenze,
    # e qui porta la scelta fino in fondo invece di fare la metà.
    #
    # Resta dietro --fallback-senza-grafica, per chi sa che il proprio file
    # è solo testo e accetta il risultato.
    if fallback_senza_grafica:
        log.info("   Conversione %s -> PDF (fallback Python python-pptx+PyMuPDF)...", ext_upper)
        if _pptx_fallback_to_pdf(ppt_path, pdf_path):
            marker_path.write_text(_file_md5(ppt_path), encoding="ascii")
            log.info("   -> PDF convertito (fallback): %s", pdf_path)
            log.warning(
                "   ATTENZIONE: slide ricostruite SENZA layout, colori e sfondi. "
                "L'OCR ne ricava meno testo e la sincronizzazione e' meno affidabile."
            )
            return pdf_path
        raise RuntimeError(f"Conversione {ext_upper}->PDF fallita anche con il fallback Python.")

    raise RuntimeError(
        f"Conversione {ext_upper}->PDF non riuscita: nessun convertitore puo' "
        f"ricostruire le slide senza perdere la grafica.\n"
        f" - LibreOffice: {'non trovato' if not soffice else 'errore'}\n"
        f" - GenOffice CLI: {'non trovato' if not genoffice else 'errore'}\n"
        f" - PowerPoint COM: {'non trovato' if not powerpoint else 'errore'}\n"
        f" - ONLYOFFICE x2t: {'non trovato' if not onlyoffice else 'errore'}\n"
        "\n"
        "Il programma si ferma qui invece di produrre un video peggiore senza\n"
        "avvisarti: con le slide ricostruite a mano dal solo testo l'OCR vede\n"
        "molto meno, e la sincronizzazione peggiora senza che se ne accorga\n"
        "nessuno.\n"
        "\n"
        "Soluzioni, dalla piu' rapida:\n"
        " 1) Apri il .pptx e salva come PDF (PowerPoint, LibreOffice, Google\n"
        "    Slides, ONLYOFFICE: tutti lo fanno). Salvalo come presentazione.pdf\n"
        "    nella cartella e rilancia: e' il percorso che dà il risultato migliore.\n"
        " 2) Installa LibreOffice e rilancia: winget install TheDocumentFoundation.LibreOffice\n"
        " 3) Se il tuo file e' davvero solo testo e ti va bene cosi', rilancia con\n"
        "    --fallback-senza-grafica per accettare le slide ricostruite.\n"
        "\n"
        f"    py -3.11 {config_module_path()} --pdf \"{ppt_path.parent / (ppt_path.stem + '.pdf')}\""
    )


def config_module_path() -> str:
    """Percorso di src/main.py, per i comandi da copiare nel messaggio d'errore."""
    return str(Path(__file__).with_name("main.py"))


def _render_page(args: tuple[str, int, str, int]) -> str:
    """Renderizza una singola pagina PDF in PNG (eseguita in processo separato)."""
    pdf_path, page_num, output_path, dpi = args
    doc = fitz.open(str(pdf_path))
    try:
        page = doc.load_page(page_num)
        pix = page.get_pixmap(dpi=dpi)
        pix.save(str(output_path))
    finally:
        doc.close()
    return output_path


def _tesseract() -> Any:
    """pytesseract, importato solo al primo uso effettivo dell'OCR.

    pytesseract importa pandas a livello di modulo, e pandas dipende da numpy:
    un import in cima al file rende l'INTERO programma ostaggio di quella
    catena. Misurato il 16/09/2026: con numpy 1.24.3 l'import di pandas falliva
    ("C extension: None not built") e main.py non partiva più, nemmeno per le
    fasi che non usano affatto l'OCR. L'import pigro sposta il guasto dove
    serve davvero, cioè nel punto d'uso.
    """
    import pytesseract

    return pytesseract


_TESTO_NESSUN_TESTO = "[Nessun testo rilevato. Immagine visiva.]"

_MESSAGGIO_OCR_VUOTO = (
    "OCR: nessun testo estratto da nessuna delle {n} pagine. Tesseract è "
    "installato e ha la lingua 'ita'?\n"
    "  - Tesseract OCR: `winget install UB-Mannheim.TesseractOCR`\n"
    "    (apt: `apt-get install tesseract-ocr tesseract-ocr-ita`)\n"
    "  - Dati della lingua: `tessdata/ita.traineddata` (non sono nel repo)\n"
    "  - Verifica con: `tesseract --list-langs`\n"
    "  Senza OCR le slide restano vuote e la sincronizzazione perde il segnale "
    "del contenuto: le ancore 'slide N' funzionano comunque, ma il motore non "
    "ha piu' nulla su cui appoggiarsi."
)


def _ocr_tutto_vuoto(slide_texts: list[str]) -> bool:
    """True se NESSUNA slide ha testo: il caso in cui Tesseract non c'e'.

    Una slide senza testo e' legittima (un'immagine, una foto) e li' il
    segnaposto e' la risposta giusta. Tutte vuote invece significa quasi
    certamente che l'OCR non ha girato, e senza avviso la run produce un video
    con le slide vuote: sembra a posto e non lo e'.
    """
    if not slide_texts:
        return False
    return all(t.strip() in ("", _TESTO_NESSUN_TESTO) for t in slide_texts)


def _ocr_single_slide(image_path: Path, lang: str, max_retries: int = 3) -> str:
    """Esegue OCR su una singola immagine con retry e backoff."""
    pt = _tesseract()
    raw = ""
    for attempt in range(max_retries):
        try:
            # Passiamo il PERCORSO, non l'oggetto Image: Pillow 12.x ha una
            # regressione quando pytesseract ri-salva il PNG (AttributeError:
            # 'PngImageFile' object has no attribute '_im'). Col path pytesseract
            # usa il file direttamente senza duplicarlo.
            raw = pt.image_to_string(str(image_path), lang=lang)
            break
        except (pt.TesseractError, OSError) as e:
            if attempt < max_retries - 1:
                wait = (attempt + 1) * 1.0
                log.debug(
                    "   OCR retry %d/%d per %s (errore: %s), attesa %.0fs...",
                    attempt + 1,
                    max_retries,
                    image_path.name,
                    e,
                    wait,
                )
                time.sleep(wait)
            else:
                log.debug("   OCR fallito con lang=%s, riprovo senza lingua.", lang)
                try:
                    raw = pt.image_to_string(str(image_path))
                except (pt.TesseractError, OSError):
                    raw = ""
    clean = re.sub(r"\s+", " ", raw).strip()
    return clean if clean else _TESTO_NESSUN_TESTO


def extract_slides_text_ocr(
    pdf_path: Path,
    output_dir: Path,
    lang: str = "ita",
    dpi: int = 300,
    workers: int = 4,
) -> tuple[list[str], list[str]]:
    """Converte ogni pagina del PDF in PNG ed estrae il testo via OCR."""
    log.info("1. Analisi del PDF: Estrazione OCR dei testi per l'IA...")
    output_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)
    doc.close()

    slide_files: list[str] = []
    pages_to_render: list[tuple[str, int, str, int]] = []

    for page_num in range(total_pages):
        output_path = output_dir / f"slide_{page_num:03d}.png"
        valid_cache = False
        if output_path.exists():
            try:
                with Image.open(str(output_path)) as test_img:
                    test_img.verify()
                valid_cache = True
            except (OSError, ValueError, SyntaxError):
                log.debug("   Cache corrotta per %s, ri-renderizzo.", output_path.name)
                output_path.unlink(missing_ok=True)
        if not valid_cache:
            pages_to_render.append((str(pdf_path), page_num, str(output_path), dpi))
        slide_files.append(str(output_path))

    if pages_to_render:
        n_render = len(pages_to_render)
        log.info("   Rendering %d slide (%d cached)...", n_render, total_pages - n_render)
        n_workers = min(workers, n_render)
        # `or n_render <= 1` era un secondo termine morto: n_workers è già il
        # minimo tra workers e n_render, quindi n_render <= 1 implica
        # n_workers <= 1. Resta solo il test sul worker effettivo.
        if n_workers <= 1:
            for args in tqdm(pages_to_render, desc="Rendering slide"):
                _render_page(args)
        else:
            with Pool(processes=n_workers) as pool:
                list(tqdm(pool.imap(_render_page, pages_to_render), total=n_render, desc="Rendering slide"))
    else:
        log.info("   Tutte le slide sono in cache (nessun rendering necessario).")

    slide_texts: list[str] = [""] * total_pages
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_ocr_single_slide, Path(sf), lang): idx for idx, sf in enumerate(slide_files)}
        for future in as_completed(futures):
            idx = futures[future]
            slide_texts[idx] = future.result()

    # Tesseract assente = ogni slide prende il segnaposto e la run va a buon
    # fine: il video esce con le slide vuote e nessun errore, che è la cosa più
    # difficile da diagnosticare per chi clona il progetto. pytesseract
    # eredita TesseractNotFoundError da OSError, quindi _ocr_single_slide lo
    # tratta come un errore di OCR qualsiasi e lo annulla.
    #
    # Una slide senza testo è legittima (un'immagine, una foto): il segnaposto
    # è la risposta giusta. TUTTE le slide vuote no: è quasi certamente
    # Tesseract non installato, e va detto adesso invece di produrre un video
    # che sembra a posto e non lo è.
    if _ocr_tutto_vuoto(slide_texts):
        raise RuntimeError(_MESSAGGIO_OCR_VUOTO.format(n=total_pages))

    for i, txt in enumerate(slide_texts):
        preview = txt[:80] + "..." if len(txt) > 80 else txt
        log.debug("   Slide %d: %s", i + 1, preview)

    log.info("   -> OCR completato su %d slide.", total_pages)
    return slide_files, slide_texts
