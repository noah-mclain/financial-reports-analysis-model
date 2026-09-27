"""Label the statement pages of the scanned golden documents, blind first.

    uv run python scripts/label_statement_pages.py serve       # OCR, then a sheet per document
    uv run python scripts/label_statement_pages.py reconcile   # compare labels with the cues
    uv run python scripts/label_statement_pages.py apply       # write the labels into manifest.yaml

The sheet shows every page with no suggestions. Only after the owner saves the labels does
``reconcile`` list where the title and structure cues disagree, so the answer key is not
anchored to the thing it scores (spec 09, R23).
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import html
import io
import json
import re
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import yaml

from fra_core.schemas import StatementType
from fra_ingest.config import REPO_ROOT, load_config
from fra_ingest.locate import load_title_book, score_page
from fra_ingest.ocr import VisionOcr
from fra_ingest.pages import read_pages, sha256_file

MANIFEST = REPO_ROOT / "eval" / "golden" / "manifest.yaml"
LABELS_DIR = REPO_ROOT / "var" / "labels"
KEYS = {
    "financial_position": StatementType.BALANCE,
    "profit_or_loss": StatementType.INCOME,
    "comprehensive_income": StatementType.COMPREHENSIVE_INCOME,
    "changes_in_equity": StatementType.EQUITY,
    "cash_flows": StatementType.CASH_FLOW,
}
THUMB_DPI = 40
_RANGE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$")


def parse_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for part in filter(None, (p.strip() for p in text.split(","))):
        match = _RANGE.match(part)
        if not match:
            msg = f"not a page range: {part!r}"
            raise ValueError(msg)
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if first < 1 or last < first:
            msg = f"not a page range: {part!r}"
            raise ValueError(msg)
        ranges.append((first, last))
    return ranges


def manifest_block(labels: dict[str, list[tuple[int, int]]]) -> list[str]:
    lines = ["    statement_pages:  # labelled blind from the OCR sheet, then reconciled"]
    for key in KEYS:
        spans = labels.get(key) or []
        if len(spans) == 1:
            lines.append(f"      {key}: [{spans[0][0]}, {spans[0][1]}]")
        elif spans:
            joined = ", ".join(f"[{a}, {b}]" for a, b in spans)
            lines.append(f"      {key}: [{joined}]")
    return lines


def apply_to_manifest(text: str, doc_id: str, labels: dict[str, list[tuple[int, int]]]) -> str:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == f"- id: {doc_id}"), None)
    if start is None:
        msg = f"{doc_id} is not in the manifest"
        raise ValueError(msg)
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("- id:")),
        len(lines),
    )
    for i in range(start, end):
        if lines[i].strip() == "statement_pages: null":
            return "\n".join(lines[:i] + manifest_block(labels) + lines[i + 1 :]) + "\n"
    msg = f"{doc_id} already has statement pages"
    raise ValueError(msg)


def disagreements(marked: set[int], suggested: set[int]) -> tuple[list[int], list[int]]:
    """(pages suggested but not marked, pages marked but not suggested)."""
    return sorted(suggested - marked), sorted(marked - suggested)


def _unlabelled() -> list[dict[str, Any]]:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    return [doc for doc in manifest["documents"] if doc.get("statement_pages") is None]


def _pdf_path(doc: dict[str, Any]) -> Path:
    return MANIFEST.parent / doc["file"]


def _pages(doc: dict[str, Any]) -> list[Any]:
    config = load_config()
    path = _pdf_path(doc)
    return read_pages(path, config, VisionOcr(), cache_dir=config.artifact_root / sha256_file(path))


def _thumbnails(path: Path) -> list[str]:
    pdf = pdfium.PdfDocument(path)
    try:
        encoded = []
        for index in range(len(pdf)):
            image = pdf[index].render(scale=THUMB_DPI / 72).to_pil().convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=70)
            encoded.append(base64.b64encode(buffer.getvalue()).decode("ascii"))
        return encoded
    finally:
        pdf.close()


def _sheet(doc: dict[str, Any], thumbs: list[str]) -> str:
    saved = LABELS_DIR / f"{doc['id']}.json"
    current = json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else {}
    inputs = "".join(
        f'<label>{key}<input name="{key}" placeholder="e.g. 5 or 5-6" value="'
        + html.escape(", ".join(f"{a}-{b}" if a != b else str(a) for a, b in current.get(key, [])))
        + '"></label>'
        for key in KEYS
    )
    tiles = "".join(
        f'<figure><a href="/page/{doc["id"]}/{n}" target="_blank">'
        f'<img src="data:image/jpeg;base64,{data}"></a><figcaption>{n}</figcaption></figure>'
        for n, data in enumerate(thumbs, start=1)
    )
    return f"""<!doctype html><meta charset="utf-8"><title>{doc["id"]}</title>
<style>
body{{font:14px system-ui;margin:0}} form{{position:sticky;top:0;background:#fff;padding:12px;
border-bottom:1px solid #ccc;display:flex;gap:12px;flex-wrap:wrap;align-items:end}}
label{{display:flex;flex-direction:column;font-size:12px}} input{{width:120px}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px;padding:12px}}
figure{{margin:0;text-align:center}} img{{width:100%;border:1px solid #ddd}}
</style>
<form id="f"><strong>{doc["id"]}</strong>{inputs}<button>Save</button><span id="s"></span></form>
<main>{tiles}</main>
<script>
document.getElementById('f').onsubmit = async (e) => {{
  e.preventDefault();
  const body = Object.fromEntries(new FormData(e.target));
  const r = await fetch(location.pathname, {{method: 'POST', body: JSON.stringify(body)}});
  document.getElementById('s').textContent = r.ok ? 'saved' : await r.text();
}};
</script>"""


def cmd_serve(args: argparse.Namespace) -> int:
    docs = {doc["id"]: doc for doc in _unlabelled()}
    thumbs: dict[str, list[str]] = {}
    for doc_id, doc in docs.items():
        print(f"reading {doc_id} ...", flush=True)
        _pages(doc)  # fills the page cache the locator will reuse
        thumbs[doc_id] = _thumbnails(_pdf_path(doc))
    LABELS_DIR.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parts = self.path.strip("/").split("/")
            if parts == [""]:
                links = "".join(
                    f'<li><a href="/doc/{d}">{d}</a>'
                    + (" (saved)" if (LABELS_DIR / f"{d}.json").exists() else "")
                    for d in docs
                )
                self._send(f"<!doctype html><meta charset='utf-8'><ul>{links}</ul>")
            elif len(parts) == 2 and parts[0] == "doc" and parts[1] in thumbs:
                self._send(_sheet(docs[parts[1]], thumbs[parts[1]]))
            elif len(parts) == 3 and parts[0] == "page" and parts[1] in docs:
                pdf = pdfium.PdfDocument(_pdf_path(docs[parts[1]]))
                image = pdf[int(parts[2]) - 1].render(scale=100 / 72).to_pil().convert("RGB")
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=85)
                pdf.close()
                self._send(buffer.getvalue(), "image/jpeg")
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            parts = self.path.strip("/").split("/")
            if len(parts) != 2 or parts[1] not in docs:
                self.send_error(404)
                return
            raw = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                labels = {key: parse_ranges(raw.get(key, "")) for key in KEYS}
            except ValueError as exc:
                self._send(str(exc), "text/plain", status=400)
                return
            out = LABELS_DIR / f"{parts[1]}.json"
            out.write_text(json.dumps({k: v for k, v in labels.items() if v}), encoding="utf-8")
            self._send("saved", "text/plain")

        def _send(self, body: str | bytes, kind: str = "text/html", status: int = 200) -> None:
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", f"{kind}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"labelling sheets at {url} (Ctrl-C to stop)")
    webbrowser.open(url)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    return 0


def cmd_reconcile(_: argparse.Namespace) -> int:
    book = load_title_book()
    for doc in _unlabelled():
        saved = LABELS_DIR / f"{doc['id']}.json"
        if not saved.exists():
            print(f"{doc['id']}: not labelled yet")
            continue
        labels = json.loads(saved.read_text(encoding="utf-8"))
        scores = [score_page(page, book) for page in _pages(doc)]
        report = [f"# {doc['id']}"]
        for key, statement_type in KEYS.items():
            marked = {p for a, b in labels.get(key, []) for p in range(a, b + 1)}
            suggested = {s.page_no for s in scores if s.is_candidate(statement_type)}
            only_suggested, only_marked = disagreements(marked, suggested)
            if only_suggested or only_marked:
                report.append(
                    f"{key}: suggested, not marked {only_suggested}; "
                    f"marked, not suggested {only_marked}"
                )
        text = "\n".join(report if len(report) > 1 else [*report, "no disagreements"])
        (LABELS_DIR / f"{doc['id']}.reconcile.txt").write_text(text + "\n", encoding="utf-8")
        print(text)
    return 0


def cmd_apply(_: argparse.Namespace) -> int:
    text = MANIFEST.read_text(encoding="utf-8")
    for doc in _unlabelled():
        saved = LABELS_DIR / f"{doc['id']}.json"
        if saved.exists():
            labels = {
                k: [(int(a), int(b)) for a, b in v]
                for k, v in json.loads(saved.read_text(encoding="utf-8")).items()
            }
            text = apply_to_manifest(text, doc["id"], labels)
            print(f"applied {doc['id']}")
    MANIFEST.write_text(text, encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(run=cmd_serve)
    commands.add_parser("reconcile").set_defaults(run=cmd_reconcile)
    commands.add_parser("apply").set_defaults(run=cmd_apply)
    args = parser.parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    raise SystemExit(main())
