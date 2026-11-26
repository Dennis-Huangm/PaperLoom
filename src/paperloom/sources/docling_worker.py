"""Run Docling in its own process for cancellable PDF jobs."""
from dataclasses import asdict
from pathlib import Path
import sys

from ..config import PDFConfig
from ..utils import write_json
from .pdf_pipeline import PDFParser


def main(pdf: str, staging: str, max_pages: str, do_ocr: str) -> int:
    directory = Path(staging)
    parser = PDFParser(PDFConfig(max_pages=int(max_pages)))
    try:
        parsed = parser._parse_docling(Path(pdf), directory, do_ocr=do_ocr == "1")
    except ImportError:
        return 2
    data = asdict(parsed)
    for figure in data["figures"]:
        figure["path"] = figure["path"].name
    write_json(directory / "parsed.json", data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
