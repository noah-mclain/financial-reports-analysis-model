"""The convert stage inside the child (spec 10, Data flow and Failure handling)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from support import FakeRunner, located

from fra_core.schemas import PageMode
from fra_ingest.config import IngestConfig
from fra_ingest.convert import CONVERT_VERSION, convert_pdf, settings_hash
from fra_ingest.converter import RangeRunner
from fra_ingest.ocr_policy import plan_ranges
from fra_ingest.results import ConvertResult, LocateResult

TXT, IMG = PageMode.TEXT, PageMode.IMAGE
SHA = "c" * 64
PDF = Path("doc.pdf")


def config(tmp_path: Path, **changes: object) -> IngestConfig:
    return IngestConfig(artifact_root=tmp_path).model_copy(update=changes)


def factory(runner: FakeRunner) -> Callable[[IngestConfig], RangeRunner]:
    return lambda _config: runner


def never(_config: IngestConfig) -> RangeRunner:
    raise AssertionError("docling must not load")


def doc(ranges: list[tuple[int, int]], modes: list[PageMode] | None = None) -> LocateResult:
    return located(modes or [TXT] * 6, ranges, sha256=SHA)


def run(
    tmp_path: Path, runner: FakeRunner, located_result: LocateResult, **changes: object
) -> ConvertResult:
    return convert_pdf(
        PDF,
        located_result,
        {},
        config(tmp_path, **changes),
        runner_factory=factory(runner),
        docling="2.126.0",
    )


def test_each_range_is_converted_and_written(tmp_path: Path) -> None:
    runner = FakeRunner()
    result = run(tmp_path, runner, doc([(2, 3), (5, 5)]))
    out = tmp_path / SHA

    assert [p.label for p in runner.calls] == ["2-3", "5-5"]
    assert [r.status for r in result.ranges] == ["ok", "ok"]
    assert [r.docling_path for r in result.ranges] == ["docling/p2-3.json", "docling/p5-5.json"]
    assert result.page_images == {2: "pages/2.png", 3: "pages/3.png", 5: "pages/5.png"}
    assert all((out / path).is_file() for path in result.page_images.values())
    assert (out / "docling" / "p2-3.json").is_file()
    assert ConvertResult.model_validate_json((out / "convert.json").read_text()) == result
    assert result.version == CONVERT_VERSION
    assert result.docling_version == "2.126.0"
    assert result.peak_footprint_gb is not None and result.peak_footprint_gb > 0
    assert {"models", "convert", "write"} <= set(result.timings)


def test_no_ranges_writes_a_result_without_loading_docling(tmp_path: Path) -> None:
    result = convert_pdf(
        PDF, doc([]), {}, config(tmp_path), runner_factory=never, docling="2.126.0"
    )
    assert result.flags == ["no_statements_found"]
    assert result.ranges == []
    assert (tmp_path / SHA / "convert.json").is_file()


def test_a_second_run_with_the_same_settings_is_served_from_convert_json(tmp_path: Path) -> None:
    first = run(tmp_path, FakeRunner(), doc([(2, 3)]))
    again = FakeRunner()
    assert run(tmp_path, again, doc([(2, 3)])) == first
    assert again.calls == []


def test_a_changed_setting_converts_again(tmp_path: Path) -> None:
    run(tmp_path, FakeRunner(), doc([(2, 3)]))
    again = FakeRunner()
    run(tmp_path, again, doc([(2, 3)]), images_scale=1.0)
    assert len(again.calls) == 1


def test_use_cache_false_converts_again(tmp_path: Path) -> None:
    run(tmp_path, FakeRunner(), doc([(2, 3)]))
    again = FakeRunner()
    convert_pdf(
        PDF,
        doc([(2, 3)]),
        {},
        config(tmp_path),
        runner_factory=factory(again),
        docling="2.126.0",
        use_cache=False,
    )
    assert len(again.calls) == 1


def test_a_missing_artifact_file_converts_again(tmp_path: Path) -> None:
    run(tmp_path, FakeRunner(), doc([(2, 3)]))
    (tmp_path / SHA / "pages" / "3.png").unlink()
    again = FakeRunner()
    run(tmp_path, again, doc([(2, 3)]))
    assert len(again.calls) == 1


def test_leftovers_without_convert_json_are_not_trusted(tmp_path: Path) -> None:
    run(tmp_path, FakeRunner(), doc([(2, 3)]))
    (tmp_path / SHA / "convert.json").unlink()
    again = FakeRunner()
    run(tmp_path, again, doc([(2, 3)]))
    assert len(again.calls) == 1


def test_a_cached_result_with_a_failed_range_is_retried(tmp_path: Path) -> None:
    run(tmp_path, FakeRunner({"2-3": "raise"}), doc([(2, 3), (5, 5)]))
    again = FakeRunner()
    run(tmp_path, again, doc([(2, 3), (5, 5)]))
    assert [p.label for p in again.calls] == ["2-3", "5-5"]


def test_files_from_an_earlier_run_are_removed(tmp_path: Path) -> None:
    run(tmp_path, FakeRunner(), doc([(1, 1)]))
    run(tmp_path, FakeRunner(), doc([(2, 3)]))
    out = tmp_path / SHA
    assert sorted(p.name for p in (out / "docling").iterdir()) == ["p2-3.json"]
    assert sorted(p.name for p in (out / "pages").iterdir()) == ["2.png", "3.png"]


def test_a_range_that_raises_fails_alone(tmp_path: Path) -> None:
    result = run(tmp_path, FakeRunner({"2-3": "raise"}), doc([(2, 3), (5, 5)]))
    failed, ok = result.ranges
    assert failed.status == "failed"
    assert failed.docling_path is None
    assert "convert_failed:2-3" in failed.flags
    assert any(f.startswith("error:RuntimeError") for f in failed.flags)
    assert ok.status == "ok"
    assert not result.all_failed


def test_a_failed_write_fails_only_that_range_and_leaves_nothing_behind(tmp_path: Path) -> None:
    result = run(tmp_path, FakeRunner({"2-3": "write_fails"}), doc([(2, 3), (5, 5)]))
    failed, ok = result.ranges
    out = tmp_path / SHA

    assert failed.status == "failed"
    assert failed.docling_path is None
    assert "convert_failed:2-3" in failed.flags
    assert any(f.startswith("error:OSError") for f in failed.flags)
    assert not (out / "docling" / "p2-3.json").is_file()
    assert not (out / "pages" / "2.png").is_file()
    assert not (out / "pages" / "3.png").is_file()
    assert ok.status == "ok"
    assert result.page_images == {5: "pages/5.png"}
    assert not result.all_failed


def test_a_killed_run_does_not_leave_a_stale_convert_json_to_trust(tmp_path: Path) -> None:
    run(tmp_path, FakeRunner(), doc([(2, 3)]))
    interrupted = FakeRunner({"2-3": "interrupt"})
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, interrupted, doc([(2, 3)]), images_scale=1.0)

    again = FakeRunner()
    run(tmp_path, again, doc([(2, 3)]))
    assert len(again.calls) == 1


def test_a_partial_range_keeps_its_output_and_says_why(tmp_path: Path) -> None:
    result = run(tmp_path, FakeRunner({"2-3": "partial"}), doc([(2, 3)]))
    (partial,) = result.ranges
    assert partial.status == "partial"
    assert partial.docling_path == "docling/p2-3.json"
    assert "convert_partial:2-3" in partial.flags
    assert "error:page 3: document timeout exceeded" in partial.flags


def test_a_docling_failure_status_fails_the_range(tmp_path: Path) -> None:
    result = run(tmp_path, FakeRunner({"2-3": "failed"}), doc([(2, 3)]))
    assert result.ranges[0].status == "failed"
    assert "convert_failed:2-3" in result.ranges[0].flags
    assert result.all_failed
    assert "all_ranges_failed" in result.flags


def test_pages_outside_the_range_fail_it(tmp_path: Path) -> None:
    result = run(tmp_path, FakeRunner({"2-3": "outside"}), doc([(2, 3)]))
    (bad,) = result.ranges
    assert bad.status == "failed"
    assert "page_outside_range:2-3" in bad.flags
    assert bad.docling_path is None
    assert result.page_images == {}


def test_a_page_without_an_image_is_flagged(tmp_path: Path) -> None:
    result = run(tmp_path, FakeRunner({"2-3": "no_image"}), doc([(2, 3)]))
    assert {"page_image_missing:2", "page_image_missing:3"} <= set(result.ranges[0].flags)
    assert result.page_images == {}


def test_a_page_docling_did_not_return_is_flagged(tmp_path: Path) -> None:
    result = run(tmp_path, FakeRunner({"2-3": "short"}), doc([(2, 3)]))
    assert "page_not_converted:3" in result.ranges[0].flags
    assert result.page_images == {2: "pages/2.png"}


def test_without_an_ocr_engine_image_ranges_are_skipped_and_docling_never_loads(
    tmp_path: Path,
) -> None:
    result = convert_pdf(
        PDF,
        doc([(2, 3)], [TXT, IMG, TXT, TXT]),
        {},
        config(tmp_path, convert_ocr="none"),
        runner_factory=never,
        docling="2.126.0",
    )
    (skipped,) = result.ranges
    assert skipped.status == "skipped"
    assert skipped.flags == ["ocr_unavailable:2-3"]
    assert not result.all_failed


def test_a_peak_above_the_budget_is_flagged(tmp_path: Path) -> None:
    result = run(tmp_path, FakeRunner(), doc([(2, 3)]), memory_budget_gb=0.001)
    assert "memory_over_budget" in result.flags


def test_timings_passed_in_are_kept(tmp_path: Path) -> None:
    result = convert_pdf(
        PDF,
        doc([(2, 3)]),
        {},
        config(tmp_path),
        runner_factory=factory(FakeRunner()),
        docling="2.126.0",
        timings={"locate": 1.5},
    )
    assert result.timings["locate"] == 1.5


@pytest.mark.parametrize("change", [{"device": "cpu"}, {"do_cell_matching": False}])
def test_the_settings_hash_follows_settings_and_plans(
    tmp_path: Path, change: dict[str, object]
) -> None:
    base = config(tmp_path)
    plans = plan_ranges(doc([(2, 3)]), {}, base)
    digest = settings_hash(base, plans, "2.126.0", "2")
    assert settings_hash(base.model_copy(update=change), plans, "2.126.0", "2") != digest
    other = plan_ranges(doc([(2, 4)]), {}, base)
    assert settings_hash(base, other, "2.126.0", "2") != digest
    assert settings_hash(base, plans, "2.127.0", "2") != digest
    assert (
        settings_hash(base.model_copy(update={"child_timeout_s": 5.0}), plans, "2.126.0", "2")
        == digest
    )
