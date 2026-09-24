"""Section-aware chunking with classification and metadata extraction."""

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ProcessedChunk:
    id: str
    text: str
    chunk_type: str
    metadata: dict = field(default_factory=dict)


def classify_text(text: str) -> str:
    """Classify a chunk as procedure, table, warning, or reference."""
    lines = text.strip().split("\n")

    # Table: multiple pipe-delimited rows
    pipe_rows = sum(1 for line in lines if "|" in line and line.count("|") >= 3)
    if pipe_rows >= 2:
        return "table"

    # Warning / Caution / Danger
    upper = text.upper()
    if any(kw in upper for kw in ("WARNING:", "CAUTION:", "DANGER:", "WARNING\n", "CAUTION\n")):
        return "warning"

    # Procedure: 3+ numbered steps
    numbered = sum(1 for line in lines if re.match(r"^\s*\d+[\.\)]\s", line.strip()))
    if numbered >= 3:
        return "procedure"

    return "reference"


def _get_headings(chunk) -> list[str]:
    """Extract heading hierarchy from a Docling chunk, handling API variations."""
    meta = getattr(chunk, "meta", None)
    if meta is None:
        return []
    for attr in ("headings", "hierarchy"):
        val = getattr(meta, attr, None)
        if val:
            return list(val)
    return []


def _get_pages(chunk) -> list[int]:
    """Extract page numbers from a Docling chunk's provenance data."""
    meta = getattr(chunk, "meta", None)
    if meta is None:
        return []

    pages: set[int] = set()
    doc_items = getattr(meta, "doc_items", None) or []
    for item in doc_items:
        prov = getattr(item, "prov", None)
        if prov is None:
            continue
        prov_list = prov if isinstance(prov, list) else [prov]
        for p in prov_list:
            page_no = getattr(p, "page_no", None)
            if page_no is not None:
                pages.add(int(page_no))
    return sorted(pages)


def chunk_document(
    doc,
    file_stem: str,
    manual_title: str,
    equipment_id: str,
    equipment_name: str,
    page_image_urls: list[str],
) -> list[ProcessedChunk]:
    """Chunk a Docling document into classified pieces with metadata.

    Uses Docling's HierarchicalChunker for structure-aware splitting.
    Falls back to markdown-based splitting if the chunker is unavailable.
    """
    try:
        return _chunk_with_hierarchical(
            doc, file_stem, manual_title, equipment_id, equipment_name, page_image_urls
        )
    except Exception:
        logger.warning(
            "HierarchicalChunker failed, falling back to markdown splitting",
            exc_info=True,
        )
        return _chunk_from_markdown(
            doc, file_stem, manual_title, equipment_id, equipment_name, page_image_urls
        )


def _chunk_with_hierarchical(
    doc,
    file_stem: str,
    manual_title: str,
    equipment_id: str,
    equipment_name: str,
    page_image_urls: list[str],
) -> list[ProcessedChunk]:
    from docling.chunking import HierarchicalChunker

    chunker = HierarchicalChunker()
    raw_chunks = list(chunker.chunk(doc))

    processed: list[ProcessedChunk] = []
    for i, chunk in enumerate(raw_chunks):
        text = chunk.text.strip()
        if not text or len(text) < 20:
            continue

        headings = _get_headings(chunk)
        pages = _get_pages(chunk)

        page_range = ""
        chunk_images: list[str] = []
        if pages:
            if len(pages) == 1:
                page_range = str(pages[0])
            else:
                page_range = f"{pages[0]} to {pages[-1]}"
            chunk_images = [
                page_image_urls[p - 1]
                for p in pages
                if 0 < p <= len(page_image_urls)
            ]

        upper_text = text.upper()
        processed.append(
            ProcessedChunk(
                id=f"{file_stem}-{i:04d}",
                text=text,
                chunk_type=classify_text(text),
                metadata={
                    "equipment_id": equipment_id,
                    "equipment_name": equipment_name,
                    "manual_title": manual_title,
                    "section_path": " > ".join(headings) if headings else "",
                    "page_range": page_range,
                    "has_warnings": any(
                        kw in upper_text for kw in ("WARNING", "CAUTION", "DANGER")
                    ),
                    "has_tables": "|" in text and text.count("|") >= 6,
                    "original_page_images": chunk_images,
                },
            )
        )

    return processed


def _chunk_from_markdown(
    doc,
    file_stem: str,
    manual_title: str,
    equipment_id: str,
    equipment_name: str,
    page_image_urls: list[str],
) -> list[ProcessedChunk]:
    """Fallback: export document to markdown and split by headings."""
    markdown = doc.export_to_markdown()
    sections = re.split(r"\n(?=#{1,4}\s)", markdown)

    processed: list[ProcessedChunk] = []
    for i, section in enumerate(sections):
        text = section.strip()
        if not text or len(text) < 20:
            continue

        heading_match = re.match(r"#{1,4}\s+(.+)", text)
        heading = heading_match.group(1).strip() if heading_match else ""

        upper_text = text.upper()
        processed.append(
            ProcessedChunk(
                id=f"{file_stem}-md-{i:04d}",
                text=text,
                chunk_type=classify_text(text),
                metadata={
                    "equipment_id": equipment_id,
                    "equipment_name": equipment_name,
                    "manual_title": manual_title,
                    "section_path": heading,
                    "page_range": "",
                    "has_warnings": any(
                        kw in upper_text for kw in ("WARNING", "CAUTION", "DANGER")
                    ),
                    "has_tables": "|" in text and text.count("|") >= 6,
                    "original_page_images": [],
                },
            )
        )

    return processed
