from pathlib import Path

import pytest

from panda import formats
from panda.formats import discover, handler_for, register


@pytest.fixture(autouse=True)
def clean_registry():
    """Keep registrations from leaking between tests."""
    handlers = dict(formats.HANDLERS)
    extractors = dict(formats.TEXT_EXTRACTORS)
    yield
    formats.HANDLERS.clear()
    formats.HANDLERS.update(handlers)
    formats.TEXT_EXTRACTORS.clear()
    formats.TEXT_EXTRACTORS.update(extractors)


def _noop(source: Path, destination: Path) -> None:
    pass


def test_no_handlers_registered_by_default():
    assert handler_for(Path("a.html")) is None


def test_register_then_lookup():
    register(".html", _noop)
    assert handler_for(Path("a.html")) is _noop


def test_lookup_is_case_insensitive():
    register(".html", _noop)
    assert handler_for(Path("A.HTML")) is _noop


def test_duplicate_registration_is_refused():
    register(".html", _noop)
    with pytest.raises(ValueError, match="already registered"):
        register(".html", _noop)


def test_discover_splits_supported_from_unsupported(tmp_path):
    register(".html", _noop)
    (tmp_path / "page.html").write_text("x")
    (tmp_path / "notes.txt").write_text("x")

    found = discover(tmp_path)

    assert [p.name for p in found.supported] == ["page.html"]
    assert [p.name for p in found.unsupported] == ["notes.txt"]
    assert found.total == 2


def test_discover_recurses_into_subdirectories(tmp_path):
    nested = tmp_path / "batch" / "monday"
    nested.mkdir(parents=True)
    (nested / "a.eml").write_text("x")

    assert [p.name for p in discover(tmp_path).unsupported] == ["a.eml"]


def test_discover_includes_hidden_files(tmp_path):
    """A hidden client file is still a client file, and skipping it is a silent leak."""
    (tmp_path / ".attachment").write_text("x")

    assert [p.name for p in discover(tmp_path).unsupported] == [".attachment"]


def test_discover_ignores_directories(tmp_path):
    (tmp_path / "empty").mkdir()

    assert discover(tmp_path).total == 0


def test_discovery_refuses_linked_root(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    linked = tmp_path / 'linked'
    linked.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match='linked'):
        discover(linked)
