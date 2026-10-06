import pytest

from publication_state import RequestJournal


def test_crash_after_start_blocks_retry_and_conflicting_arguments(tmp_path):
    path = tmp_path / "private" / "requests.sqlite"
    request = RequestJournal("one", "same", path)
    assert request.lookup() is None
    request.start()
    assert RequestJournal("one", "same", path).lookup() == ("uncertain", "")
    with pytest.raises(RuntimeError, match="already started"):
        RequestJournal("one", "same", path).start()
    with pytest.raises(ValueError, match="different media"):
        RequestJournal("one", "changed", path).lookup()
    request.remember_video("wZqnYESB3Us")
    assert RequestJournal("one", "same", path).lookup() == ("uncertain", "wZqnYESB3Us")
    request.published("wZqnYESB3Us")
    assert RequestJournal("one", "same", path).lookup() == ("published", "wZqnYESB3Us")
