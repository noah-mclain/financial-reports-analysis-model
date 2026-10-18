"""The routed pass over synthetic archives: nothing unrouted is decoded, parsed or kept."""

import gzip
import hashlib
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dataset_contracts import DatasetError
from harness.holdout_records import LOG_HEADER
from sec_source_access import (
    AccessContext,
    SecIndex,
    SourceAccessError,
    SourceManifest,
    iter_payloads,
)
from sec_source_pass import (
    PayloadRow,
    directory_opener,
    run_pass,
    stream_routed_rows,
)

from fra_core.pools import Identity, PoolRegistry
from fra_core.split import Part, cik_key, hashed_part
from pool_store import save_registry

SUB_HEADER = "\t".join(
    ["adsh", "cik", "name", "sic", "form", "period", "fy", "fp", "nciks", "aciks"]
)
PRE_HEADER = "\t".join(
    ["adsh", "report", "line", "stmt", "inpth", "rfile", "tag", "version", "plabel", "negating"]
)
CANARY = "CANARY-UNROUTED-LABEL-7731"


def cik_in(part: Part, skip: int = 0) -> int:
    return [c for c in range(1, 3000) if hashed_part(cik_key(c)) == part][skip]


def adsh(n: int) -> str:
    return f"{n:010d}-24-{n:06d}"


def sub_line(n: int, cik: int, name: str, form: str = "10-K") -> str:
    return "\t".join([adsh(n), str(cik), name, "3714", form, "20241231", "2024", "FY", "1", ""])


def pre_line(
    n: int, line: int, label: str, *, stmt: str = "BS", tag: str = "Assets", inpth: str = "0"
) -> bytes:
    fields = [adsh(n), "2", str(line), stmt, inpth, "H", tag, "us-gaap/2024", label, "0"]
    return ("\t".join(fields) + "\n").encode("utf-8")


def write_archive(path: Path, subs: list[str], pre: list[bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("sub.txt", "\n".join([SUB_HEADER, *subs]) + "\n")
        archive.writestr("pre.txt", (PRE_HEADER + "\n").encode() + b"".join(pre))
        archive.writestr("num.txt", b"MUST NEVER BE READ\n")
    return path


class World:
    """Registry, moves and looks files plus one archive with routed and unrouted filings."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.fit = cik_in(Part.FIT)
        self.valid = cik_in(Part.VALIDATION)
        self.hold = cik_in(Part.HOLDOUT)
        self.test = cik_in(Part.FIT, 1)
        registry = PoolRegistry()
        for cik, name, pool in (
            (self.fit, "Fit Co", "train"),
            (self.valid, "Valid Co", "train"),
            (self.hold, "Hold Co", "train"),
            (self.test, "Test Co", "model_test"),
        ):
            registry.register(Identity(name, cik), pool)
        self.registry_path = root / "registry.json"
        save_registry(registry, self.registry_path)
        self.moves_path = root / "moves.yaml"
        self.moves_path.write_text("moves: []\n", encoding="utf-8")
        self.looks_path = root / "looks.tsv"
        self.looks_path.write_text(LOG_HEADER, encoding="utf-8")
        subs = [
            sub_line(1, self.fit, "Fit Co"),
            sub_line(2, self.valid, "Valid Co", "10-Q"),
            sub_line(3, self.hold, "Hold Co"),
            sub_line(4, self.test, "Test Co"),
        ]
        pre = [
            pre_line(1, 1, "Total assets"),
            pre_line(1, 2, "Net sales", stmt="IS", tag="Revenues"),
            pre_line(2, 1, "Total assets"),
            # Unrouted filings: a canary label, invalid UTF-8 and a malformed row.
            pre_line(3, 1, CANARY),
            b"0000000003-24-000003\t\xff\xfe\xfd\n",
            b"0000000004-24-000004\tonly\tthree\n",
            pre_line(4, 1, CANARY),
        ]
        self.archive = write_archive(root / "2024q3.zip", subs, pre)

    def run(self, **changes: object):  # type: ignore[no-untyped-def]
        arguments = {
            "archives": [self.archive],
            "registry_path": self.registry_path,
            "moves_path": self.moves_path,
            "looks_path": self.looks_path,
            "out_dir": self.root / "prepared",
            "authorization": "owner message, test",
            "clock": lambda: datetime(2026, 10, 18, tzinfo=UTC),
            **changes,
        }
        return run_pass(**arguments)  # type: ignore[arg-type]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_stream_keeps_routed_rows_and_counts_the_rest_without_reading_them(
    world: World,
) -> None:
    rows, stats = stream_routed_rows(world.archive, {adsh(1), adsh(2)})
    assert {k: [r.line for r in v] for k, v in rows.items()} == {adsh(1): [1, 2], adsh(2): [1]}
    assert (stats.lines, stats.kept, stats.discarded) == (7, 3, 4)
    assert rows[adsh(1)][1] == PayloadRow(
        adsh=adsh(1),
        stmt="IS",
        report=2,
        line=2,
        tag="Revenues",
        version="us-gaap/2024",
        plabel="Net sales",
        negating=False,
        inpth=False,
    )


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        (b"0000000001-24-000001\tshort\n", "columns"),
        (b"0000000001-24-000001\t2\t9\tBS\t5\tH\tAssets\tus-gaap/2024\tx\t0\n", "inpth"),
        (b"0000000001-24-000001\t2\t9\tBS\t0\tH\tAssets\tus-gaap/2024\t\xff\t0\n", "UTF-8"),
        (b"0000000001-24-000001\tx\t9\tBS\t0\tH\tAssets\tus-gaap/2024\tx\t0\n", "report"),
    ],
)
def test_a_malformed_routed_line_aborts_with_archive_filing_and_line(
    tmp_path: Path, bad: bytes, reason: str
) -> None:
    archive = write_archive(
        tmp_path / "2024q3.zip", [sub_line(1, 5, "A Co")], [pre_line(1, 1, "ok"), bad]
    )
    with pytest.raises(DatasetError) as caught:
        stream_routed_rows(archive, {adsh(1)})
    message = str(caught.value)
    assert "2024q3.zip" in message
    assert adsh(1) in message
    assert "line 3" in message
    assert reason in message
    assert "\\xff" not in message


@pytest.mark.parametrize("stmt", ["", "XX", "CP"])
def test_an_empty_or_undocumented_statement_code_is_kept_for_the_decoder_to_drop(
    tmp_path: Path, stmt: str
) -> None:
    line = f"0000000001-24-000001\t2\t9\t{stmt}\t0\tH\tAssets\tus-gaap/2024\tx\t0\n".encode()
    archive = write_archive(tmp_path / "2024q3.zip", [sub_line(1, 5, "A Co")], [line])
    rows, _ = stream_routed_rows(archive, {adsh(1)})
    assert [r.stmt for r in rows[adsh(1)]] == [stmt]


def test_the_pass_writes_payloads_index_manifest_context_and_a_log(world: World) -> None:
    log = world.run()
    out = world.root / "prepared"
    assert sorted(p.name for p in out.iterdir()) == ["2024q3", "pass-log.json"]
    assert json.loads((out / "pass-log.json").read_text(encoding="utf-8")) == log
    assert log["routing"]["routed"] == {"fit": 1, "validation": 1}
    assert log["routing"]["dropped"] == {"holdout": 1, "pool_model_test": 1}
    archive_log = log["archives"]["2024q3"]
    assert archive_log["pre_lines"] == 7
    assert archive_log["kept_rows"] == 3
    assert archive_log["discarded_rows"] == 4
    assert archive_log["source_sha256"] == hashlib.sha256(world.archive.read_bytes()).hexdigest()
    assert (
        log["registry"]["file_sha256"]
        == hashlib.sha256(world.registry_path.read_bytes()).hexdigest()
    )
    assert log["moves"]["file_sha256"] == hashlib.sha256(world.moves_path.read_bytes()).hexdigest()
    assert log["looks"]["file_sha256"] == hashlib.sha256(world.looks_path.read_bytes()).hexdigest()
    assert log["authorization"] == "owner message, test"
    assert log["started"] == log["finished"] == "2026-10-18T00:00:00+00:00"
    payloads = sorted((out / "2024q3" / "payloads").iterdir())
    assert [p.name for p in payloads] == [f"{adsh(1)}.jsonl.gz", f"{adsh(2)}.jsonl.gz"]


def test_no_unrouted_label_or_content_reaches_any_output(world: World) -> None:
    world.run()
    out = world.root / "prepared"
    everything = b""
    for path in sorted(out.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            everything += gzip.decompress(data) if path.suffix == ".gz" else data
    assert CANARY.encode() not in everything
    assert b"\xff\xfe" not in everything
    for unrouted in (adsh(3), adsh(4)):
        assert unrouted.encode() not in everything
    assert b"Hold Co" not in everything and b"Test Co" not in everything


def test_the_output_authorizes_and_opens_through_the_access_contract(world: World) -> None:
    from pool_store import load_registry

    world.run()
    base = world.root / "prepared" / "2024q3"
    index = SecIndex.model_validate_json((base / "index.json").read_bytes())
    manifest = SourceManifest.model_validate_json((base / "manifest.json").read_bytes())
    context = AccessContext.model_validate_json((base / "context.json").read_bytes())
    ids = [a.artifact_id for a in manifest.artifacts]
    verified = list(
        iter_payloads(
            ids,
            index=index,
            manifest=manifest,
            registry=load_registry(world.registry_path),
            context=context,
            moves=[],
            looks=[],
            opener=directory_opener(world.root / "prepared"),
        )
    )
    assert len(verified) == 2
    parts = {v.descriptor.filing.source_record.source_id: v.descriptor.part for v in verified}
    assert parts == {adsh(1): Part.FIT, adsh(2): Part.VALIDATION}
    assert {a.row_count for a in manifest.artifacts} == {2, 1}


def test_the_pass_refuses_an_existing_output_and_writes_nothing_over_it(world: World) -> None:
    world.run()
    before = (world.root / "prepared" / "pass-log.json").read_bytes()
    with pytest.raises(DatasetError, match="already exists"):
        world.run()
    assert (world.root / "prepared" / "pass-log.json").read_bytes() == before


def test_registry_changing_under_the_pass_aborts_and_is_logged(world: World) -> None:
    def tamper(_archive: str) -> None:
        world.registry_path.write_text(world.registry_path.read_text() + " ", encoding="utf-8")

    with pytest.raises(DatasetError, match="registry"):
        world.run(on_archive=tamper)
    assert not (world.root / "prepared" / "pass-log.json").exists()
    assert (world.root / "prepared" / "pass-abort.json").exists()


def test_a_routed_malformed_line_aborts_the_pass_and_is_logged_without_content(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    write_archive(
        world.archive,
        [sub_line(1, world.fit, "Fit Co")],
        [pre_line(1, 1, "fine"), b"0000000001-24-000001\tbroken\n"],
    )
    with pytest.raises(DatasetError, match="line 3"):
        world.run()
    abort = json.loads((tmp_path / "prepared" / "pass-abort.json").read_text(encoding="utf-8"))
    assert "line 3" in abort["error"]
    assert "broken" not in json.dumps(abort)


def test_the_opener_stays_inside_its_root_and_refuses_links(world: World, tmp_path: Path) -> None:
    world.run()
    out = world.root / "prepared"
    base = out / "2024q3"
    index = SecIndex.model_validate_json((base / "index.json").read_bytes())
    manifest = SourceManifest.model_validate_json((base / "manifest.json").read_bytes())
    context = AccessContext.model_validate_json((base / "context.json").read_bytes())
    from pool_store import load_registry

    first = out / manifest.artifacts[0].relative_path
    saved = first.read_bytes()
    first.unlink()
    first.symlink_to(world.archive)
    with pytest.raises(SourceAccessError, match=r"symlink|link"):
        list(
            iter_payloads(
                [manifest.artifacts[0].artifact_id],
                index=index,
                manifest=manifest,
                registry=load_registry(world.registry_path),
                context=context,
                moves=[],
                looks=[],
                opener=directory_opener(out),
            )
        )
    first.unlink()
    first.write_bytes(saved)


def test_a_presentation_header_that_does_not_start_with_adsh_is_refused(tmp_path: Path) -> None:
    with zipfile.ZipFile(tmp_path / "2024q3.zip", "w") as archive:
        archive.writestr("sub.txt", "\n".join([SUB_HEADER, sub_line(1, 5, "A Co")]) + "\n")
        archive.writestr("pre.txt", PRE_HEADER.replace("adsh\treport", "report\tadsh", 1) + "\n")
    with pytest.raises(DatasetError, match=r"2024q3\.zip.*pre\.txt.*adsh.*first"):
        stream_routed_rows(tmp_path / "2024q3.zip", {adsh(1)})


def test_a_routed_filing_without_rows_is_listed_and_counted(tmp_path: Path) -> None:
    world = World(tmp_path)
    write_archive(
        world.archive,
        [sub_line(1, world.fit, "Fit Co"), sub_line(2, world.valid, "Valid Co", "10-Q")],
        [pre_line(1, 1, "Total assets")],
    )
    log = world.run()
    assert log["archives"]["2024q3"]["empty_payloads"] == [adsh(2)]
    assert log["totals"]["empty_payloads"] == 1
    assert log["archives"]["2024q3"]["output_bytes"] > 0
    assert log["totals"]["output_bytes"] == log["archives"]["2024q3"]["output_bytes"]
