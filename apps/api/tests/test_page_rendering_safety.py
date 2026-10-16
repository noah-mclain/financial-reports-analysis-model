"""PDFium's native lifetime stays serialized, including failed and concurrent requests."""

from __future__ import annotations

import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Self

import pytest
from PIL import Image

from fra_api import pages


@pytest.mark.parametrize("first_render,second_render", [(False, True), (True, False), (True, True)])
def test_pdfium_objects_never_overlap_between_api_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_render: bool, second_render: bool
) -> None:
    first_entered = threading.Event()
    second_started = threading.Event()
    overlapping = threading.Event()
    release = threading.Event()
    native_guard = threading.Lock()
    operations: list[str] = []

    class Bitmap:
        def __init__(self) -> None:
            self.image = Image.new("RGB", (2, 2), "white")

        def close(self) -> None:
            assert native_guard.locked()
            # Simulate a PIL image backed by native memory that becomes invalid on close.
            self.image.paste("black", (0, 0, 2, 2))
            operations.append("bitmap closed")

        def to_pil(self) -> Image.Image:
            return self.image

    class Page:
        def close(self) -> None:
            assert native_guard.locked()
            operations.append("page closed")

        def get_size(self) -> tuple[float, float]:
            return 600.0, 800.0

        def render(self, *, scale: float) -> Bitmap:
            return Bitmap()

    class Document:
        def __init__(self, path: Path) -> None:
            # Check at construction, before __enter__: opening a document is native too.
            if not native_guard.acquire(blocking=False):
                overlapping.set()
                raise RuntimeError("overlapping PDFium calls")
            if not first_entered.is_set():
                first_entered.set()
                assert release.wait(3), "test did not release the first native operation"

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            operations.append("document closed")
            native_guard.release()

        def __len__(self) -> int:
            return 1

        def __getitem__(self, index: int) -> Page:
            return Page()

    monkeypatch.setattr("fra_api.pages.pdfium.PdfDocument", Document)

    def call(render: bool, second: bool = False) -> None:
        if second:
            second_started.set()
        if render:
            pages.render_page(tmp_path / "synthetic.pdf", 1, tmp_path / "page.png")
        else:
            assert pages.page_size(tmp_path / "synthetic.pdf", 1) == (600.0, 800.0)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(call, first_render)
        assert first_entered.wait(3)
        second = executor.submit(call, second_render, True)
        try:
            assert second_started.wait(3)
            assert not overlapping.wait(0.1), "another API call entered PDFium concurrently"
        finally:
            release.set()
        first.result(timeout=3)
        second.result(timeout=3)
    assert operations.count("page closed") == operations.count("document closed")
    assert operations.count("bitmap closed") == int(first_render) + int(second_render)
    # Every bitmap and page is explicitly closed before its owning document.
    assert operations[-1] == "document closed"
    with Image.open(tmp_path / "page.png") as image:
        assert image.getpixel((0, 0)) == (255, 255, 255)


def test_real_blank_pdf_thread_stress_is_isolated_from_pytest(tmp_path: Path) -> None:
    code = r"""
import io
import sys
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pypdfium2 as pdfium
from PIL import Image
from fra_api.pages import page_size, render_page

root = Path(sys.argv[1])
pdf = root / "blank.pdf"
with pdfium.PdfDocument.new() as document:
    with closing(document.new_page(60, 80)):
        document.save(pdf)
target = root / "pages" / "1.png"
def request(_):
    for _ in range(10):
        assert page_size(pdf, 1) == (60.0, 80.0)
        render_page(pdf, 1, target)
        with Image.open(io.BytesIO(target.read_bytes())) as image:
            image.load()
            assert image.size == (120, 160)
with ThreadPoolExecutor(max_workers=8) as executor:
    list(executor.map(request, range(8)))
assert [p.name for p in target.parent.iterdir()] == ["1.png"]
print("8 threads x 10 renders: valid PNGs, sizes and atomic cache; no temporary files")
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"exit {result.returncode}\n{result.stdout}\n{result.stderr}"
    assert "8 threads x 10 renders" in result.stdout


def test_failed_atomic_replacement_preserves_the_cache_and_removes_the_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pypdfium2 as pdfium

    pdf = tmp_path / "blank.pdf"
    with pdfium.PdfDocument.new() as document:
        page = document.new_page(60, 80)
        document.save(pdf)
        page.close()
    target = tmp_path / "pages" / "1.png"
    pages.render_page(pdf, 1, target)
    original = target.read_bytes()

    def fail_replace(path: Path, destination: Path) -> Path:
        raise OSError("synthetic atomic replacement failure")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_replace)
        with pytest.raises(OSError, match="synthetic atomic replacement failure"):
            pages.render_page(pdf, 1, target)
    assert target.read_bytes() == original
    assert [p.name for p in target.parent.iterdir()] == ["1.png"]
    # Native resources and the process lock were released on the failed request too.
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(pages.page_size, pdf, 1).result(timeout=3) == (60.0, 80.0)
