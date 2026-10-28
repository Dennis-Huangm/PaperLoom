"""Published artifact locations shared by persisted receipts and local readers.

These URLs are a saved-data contract; no web framework or catalog is needed.
"""
from pathlib import Path
from urllib.parse import quote


def artifact_url(path: Path, output_root: Path) -> str | None:
    """Return a browser URL only for files contained by the artifact root."""
    try:
        relative = path.resolve().relative_to(output_root.resolve()).as_posix()
    except ValueError:
        return None
    return f"/artifacts/{quote(relative, safe='/')}"

def result_artifact_url(path: Path, output_root: Path) -> str | None:
    """Prefer the rendered HTML sibling for Markdown task artifacts."""
    if path.suffix.casefold() in {".md", ".markdown"}:
        html_path = path.with_suffix(".html")
        if html_path.is_file():
            path = html_path
    return artifact_url(path, output_root)
