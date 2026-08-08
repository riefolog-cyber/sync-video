#!/usr/bin/env python3
"""
FASE 1 — Estrazione slide da PDF + OCR parallelo.
Il rendering PDF usa multiprocessing (PyMuPDF non è thread-safe).
"""

import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from multiprocessing import Pool
from pathlib import Path

import fitz  # PyMuPDF
import pytesseract
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
    return None


def convert_pptx_to_pdf(pptx_path: Path, out_dir: Path) -> Path:
    """Converte una presentazione PPTX in PDF usando LibreOffice headless.

    Args:
        pptx_path: percorso del file .pptx
        out_dir: directory dove salvare il PDF risultante

    Returns:
        Percorso del PDF convertito (stesso nome, estensione .pdf)

    Raises:
        RuntimeError: se LibreOffice non è disponibile o la conversione fallisce
    """
    soffice = _find_soffice()
    if not soffice:
        raise RuntimeError(
            "LibreOffice non trovato: necessario per convertire .pptx in PDF.\n"
            "Installa LibreOffice (https://www.libreoffice.org) oppure "
            "converte la presentazione in .pdf manualmente."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / (pptx_path.stem + ".pdf")

    # Riusa il PDF già convertito se più recente del PPTX: evita di riconvertire
    # a ogni run (LibreOffice cambia l'hash del PDF, invalidando la cache OCR).
    if pdf_path.exists() and pdf_path.stat().st_mtime >= pptx_path.stat().st_mtime:
        log.info("   -> PDF già presente (riusato): %s", pdf_path)
        return pdf_path

    cmd = [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(pptx_path)]
    log.info("   Conversione PPTX -> PDF (LibreOffice)...")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Conversione PPTX->PDF terminata per timeout (180s).") from None
    if proc.returncode != 0 or not pdf_path.exists():
        raise RuntimeError(f"Conversione PPTX->PDF fallita (exit={proc.returncode}):\n{proc.stderr}")
    log.info("   -> PDF convertito: %s", pdf_path)
    return pdf_path


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


def _ocr_single_slide(image_path: Path, lang: str, max_retries: int = 3) -> str:
    """Esegue OCR su una singola immagine con retry e backoff."""
    raw = ""
    for attempt in range(max_retries):
        try:
            raw = pytesseract.image_to_string(Image.open(str(image_path)), lang=lang)
            break
        except (pytesseract.TesseractError, OSError) as e:
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
                    raw = pytesseract.image_to_string(Image.open(str(image_path)))
                except (pytesseract.TesseractError, OSError):
                    raw = ""
    clean = re.sub(r"\s+", " ", raw).strip()
    return clean if clean else "[Nessun testo rilevato. Immagine visiva.]"


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
        if n_workers <= 1 or n_render <= 1:
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
        for future in tqdm(as_completed(futures), total=total_pages, desc="OCR slide"):
            idx = futures[future]
            slide_texts[idx] = future.result()

    for i, txt in enumerate(slide_texts):
        preview = txt[:80] + "..." if len(txt) > 80 else txt
        log.debug("   Slide %d: %s", i + 1, preview)

    log.info("   -> OCR completato su %d slide.", total_pages)
    return slide_files, slide_texts
