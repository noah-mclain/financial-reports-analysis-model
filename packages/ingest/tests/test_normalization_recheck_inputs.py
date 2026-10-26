from support import page, span, structured_source
from test_eps_checks import SCOPE, note_rows, statement_rows

from fra_core.schemas import StatementType
from fra_ingest.config import IngestConfig
from fra_ingest.results import StructureResult
from fra_ingest.structure import structure_document


def test_structure_retains_eps_inputs_across_cache_roundtrip() -> None:
    inputs = structured_source(
        [(1, statement_rows()), (2, note_rows())],
        [span(StatementType.INCOME, 1)],
        [page(1, StatementType.INCOME)],
        [(n, y, text) for n in (1, 2) for y, text in ((10, SCOPE[0]), (25, SCOPE[1]))]
        + [(2, 40, "32. EARNINGS PER SHARE")],
    )
    result, _ = structure_document(inputs, IngestConfig())
    cached = StructureResult.model_validate_json(result.model_dump_json())
    assert len(cached.eps_notes) == 1
    assert all(s.id in cached.statement_headers for s in cached.statements)
