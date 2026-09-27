"""Split rules for the corpus: nothing from a golden issuer or a shared issuer leaks across pools."""

from typing import Any

import pytest

from corpus import check, issuer_key, text_layer

GOLDEN = {issuer_key(n) for n in ["Almarai Company", "Juhayna Food Industries"]}


def doc(doc_id: str, issuer: str, pool: str, url: str | None = None) -> dict[str, Any]:
    return {
        "id": doc_id,
        "issuer": issuer,
        "pool": pool,
        "role": "corporate",
        "url": url or f"https://example.com/{doc_id}.pdf",
    }


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Almarai Company", "ALMARAI CO."),
        ("Savola Group", "The Savola Group Company"),
        ("Juhayna Food Industries", "Juhayna Food Industries S.A.E"),
    ],
)
def test_issuer_key_ignores_legal_suffixes(a: str, b: str) -> None:
    assert issuer_key(a) == issuer_key(b)


def test_distinct_issuers_stay_distinct() -> None:
    assert issuer_key("Orascom Construction") != issuer_key("Orascom Investment Holding")


def test_clean_split_passes() -> None:
    report = check(
        [doc("a", "Savola Group", "blind"), doc("b", "Jarir Marketing", "train")], GOLDEN
    )
    assert report.errors == []


def test_golden_issuer_outside_dev_is_an_error() -> None:
    report = check([doc("x", "Almarai Company", "blind")], GOLDEN)
    assert report.existing == ["x"]
    assert any("belongs in dev" in e for e in report.errors)


def test_golden_issuer_in_dev_is_marked_existing() -> None:
    report = check([doc("x", "ALMARAI CO.", "dev")], GOLDEN)
    assert report.existing == ["x"]
    assert report.errors == []


def test_issuer_spanning_pools_is_an_error() -> None:
    report = check(
        [doc("en", "Jarir Marketing", "train"), doc("ar", "Jarir Marketing", "model_test")], GOLDEN
    )
    assert any("spans pools" in e for e in report.errors)


def test_duplicate_id_and_url_are_errors() -> None:
    url = "https://example.com/same.pdf"
    report = check(
        [doc("a", "Savola Group", "blind", url), doc("a", "Savola Group", "blind", url)], GOLDEN
    )
    assert any("duplicate id" in e for e in report.errors)
    assert any("same url" in e for e in report.errors)


@pytest.mark.parametrize(
    ("chars", "layer"),
    [
        ([900, 1200, 800], "digital"),
        ([0, 3, 12], "scanned"),
        ([0, 1100, 950], "mixed"),  # image cover, text body
    ],
)
def test_text_layer_is_decided_per_page(chars: list[int], layer: str) -> None:
    assert text_layer(chars) == layer


def test_candidates_file_obeys_the_rules() -> None:
    from corpus import CANDIDATES, golden_index, load_yaml

    golden_issuers, _ = golden_index()
    report = check(load_yaml(CANDIDATES)["documents"], golden_issuers)
    assert report.errors == []


def test_assign_pool_is_stable_and_spreads() -> None:
    from corpus import assign_pool

    assert assign_pool("Savola Group") == assign_pool("The Savola Group Company")
    pools = {assign_pool(f"Issuer {i}") for i in range(300)}
    assert pools == {"train", "model_test", "blind"}


def test_missing_robots_txt_allows_and_server_error_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    import corpus

    def fake_urlopen(request: Any, timeout: float = 0) -> Any:
        code = 404 if "allow.example" in request.full_url else 503
        raise urllib.error.HTTPError(request.full_url, code, "x", None, None)  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    polite = corpus.Politeness(delay_s=0)
    assert polite.allowed("https://allow.example/a.pdf")
    assert not polite.allowed("https://down.example/a.pdf")


def test_unreachable_robots_reports_the_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    import ssl
    import urllib.error

    import corpus

    def fake_urlopen(request: Any, timeout: float = 0) -> Any:
        raise urllib.error.URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    polite = corpus.Politeness(delay_s=0, backoff_s=0)
    url = "https://tls.example/a.pdf"
    assert not polite.allowed(url)
    assert "robots.txt unreachable" in polite.refusal(url)
    assert "CERTIFICATE_VERIFY_FAILED" in polite.refusal(url)


def test_failed_download_keeps_earlier_measurement() -> None:
    from corpus import after_failure

    prior = {"pool": "train", "sha256": "ab", "pages": 40, "status": "new"}
    kept = after_failure(prior, {"pool": "train"}, "URLError: timed out")
    assert kept["sha256"] == "ab" and kept["status"] == "new"
    assert kept["last_error"] == "URLError: timed out"

    fresh = after_failure(None, {"pool": "train"}, "URLError: timed out")
    assert fresh == {"pool": "train", "status": "failed", "error": "URLError: timed out"}


def test_dropped_connection_is_retried_but_http_status_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import http.client
    import urllib.error

    import corpus

    calls: list[str] = []

    class Body:
        def __enter__(self) -> "Body":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b"%PDF-1.7"

    def fake_urlopen(request: Any, timeout: float = 0) -> Any:
        calls.append(request.full_url)
        if "forbidden" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 403, "x", None, None)  # type: ignore[arg-type]
        if len(calls) < 3:
            raise http.client.IncompleteRead(b"", 100)
        return Body()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    polite = corpus.Politeness(delay_s=0, backoff_s=0)
    assert polite.get("https://flaky.example/a.pdf") == b"%PDF-1.7"
    assert len(calls) == 3

    calls.clear()
    with pytest.raises(urllib.error.HTTPError):
        polite.get("https://forbidden.example/a.pdf")
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("error", "by_hand"),
    [
        ("PermissionError: robots.txt unreachable: Remote end closed connection", True),
        ("HTTPError: HTTP Error 403: Forbidden", True),
        ("HTTPError: HTTP Error 404: Not Found", False),
        ("IncompleteRead: IncompleteRead(0 bytes read, 10 more expected)", False),
    ],
)
def test_refused_downloads_are_listed_for_a_browser(error: str, by_hand: bool) -> None:
    from corpus import refused

    assert refused(error) is by_hand


def control(doc_id: str, **fields: str) -> dict[str, Any]:
    return {**doc(doc_id, f"Issuer {doc_id}", "train"), "role": "negative_control", **fields}


def test_negative_controls_need_a_sector() -> None:
    report = check([control("a")], GOLDEN)
    assert report.errors == ["a: negative_control needs a sector (bank, insurer, other_financial)"]


def test_other_financial_needs_a_subsector() -> None:
    report = check([control("a", sector="other_financial")], GOLDEN)
    assert report.errors == [
        "a: other_financial needs a subsector (investment_holding, brokerage, "
        "exchange_operator, consumer_finance, asset_manager, other)"
    ]


def test_banks_take_no_subsector() -> None:
    report = check([control("a", sector="bank", subsector="brokerage")], GOLDEN)
    assert report.errors == ["a: subsector is only for other_financial"]


def test_corporates_take_no_sector() -> None:
    report = check([{**doc("a", "Savola Group", "train"), "sector": "bank"}], GOLDEN)
    assert report.errors == ["a: sector is only for negative_control"]


def test_labelled_controls_pass() -> None:
    report = check(
        [
            control("a", sector="bank"),
            control("b", sector="other_financial", subsector="brokerage"),
        ],
        GOLDEN,
    )
    assert report.errors == []
