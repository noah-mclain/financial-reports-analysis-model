from fra_ingest.review import CRITICAL_CELL_FLAGS
from fra_model.payload import _LOCAL_COUNTED_REASONS


def test_critical_cell_flags_are_accepted_or_documented_as_nonlocal() -> None:
    nonlocal_flags: dict[str, str] = {}
    assert all(nonlocal_flags.values())
    assert set(CRITICAL_CELL_FLAGS) <= _LOCAL_COUNTED_REASONS | nonlocal_flags.keys()
