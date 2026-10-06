"""`make corpus-split`: the report of the train split matches the counts in
docs/blueprint/04-execution-phases.md 2.4. When the corpus changes, recount, then update 2.4 and
this test together."""

import argparse
from typing import Any

import pytest

from corpus import cmd_split, split_report

COLUMNS = ("issuers", "documents", "arabic", "english", "both", "annual", "interim")


def parse(output: str) -> dict[str, Any]:
    tables: dict[str, dict[str, dict[str, int]]] = {}
    current = ""
    totals = {}
    for line in output.splitlines():
        words = line.split()
        if line.startswith("train pool:"):
            totals = {"issuers": int(words[2]), "documents": int(words[4])}
        elif words[:1] in (["as"], ["after"]):
            current = words[0]
            tables[current] = {}
        elif current and len(words) == len(COLUMNS) + 1:
            tables[current][words[0]] = dict(zip(COLUMNS, map(int, words[1:]), strict=True))
    return {"train": totals, **tables}


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    return parse(split_report())


def test_train_pool_totals(report: dict[str, Any]) -> None:
    assert report["train"] == {"issuers": 122, "documents": 213}


def test_as_hashed(report: dict[str, Any]) -> None:
    hashed = report["as"]
    assert {p: (hashed[p]["issuers"], hashed[p]["documents"]) for p in hashed} == {
        "fit": (81, 138),
        "validation": (15, 29),
        "holdout": (26, 46),
    }


def test_after_the_four_overrides(report: dict[str, Any]) -> None:
    after = report["after"]
    assert {p: (after[p]["issuers"], after[p]["documents"]) for p in after} == {
        "fit": (85, 149),
        "validation": (15, 29),
        "holdout": (22, 35),
    }
    assert {p: (after[p]["annual"], after[p]["interim"]) for p in after} == {
        "fit": (50, 99),
        "validation": (9, 20),
        "holdout": (5, 30),
    }
    assert (after["holdout"]["arabic"], after["holdout"]["english"]) == (16, 15)
    assert {p: after[p]["both"] for p in after} == {"fit": 31, "validation": 6, "holdout": 9}
    assert after["validation"]["arabic"] == 9


def test_command_prints_the_report(capsys: pytest.CaptureFixture[str]) -> None:
    assert cmd_split(argparse.Namespace()) == 0
    assert parse(capsys.readouterr().out) == parse(split_report())
