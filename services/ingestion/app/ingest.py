"""Single-PDF processing: Docling conversion and page image extraction."""

import logging
from pathlib import Path

import fitz  # PyMuPDF

from .config import settings

logger = logging.getLogger(__name__)

_converter = None


def _get_converter():
    global _converter
    if _converter is None:
        from docling.document_converter import DocumentConverter

        _converter = DocumentConverter()
        logger.info("Docling DocumentConverter initialized")
    return _converter


def convert_pdf(pdf_path: str):
    """Run Docling on a PDF file. Returns the structured DoclingDocument."""
    converter = _get_converter()
    result = converter.convert(pdf_path)
    return result.document


def extract_page_images(pdf_path: str, output_dir: str) -> list[str]:
    """Render every page of a PDF as a PNG at the configured DPI."""
    doc = fitz.open(pdf_path)
    paths: list[str] = []
    for page in doc:
        img_path = str(Path(output_dir) / f"page_{page.number + 1:04d}.png")
        pix = page.get_pixmap(dpi=settings.PAGE_IMAGE_DPI)
        pix.save(img_path)
        paths.append(img_path)
    doc.close()
    return paths
