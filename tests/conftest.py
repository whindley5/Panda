import pytest

from panda import formats


@pytest.fixture(autouse=True)
def _isolated_format_registry():
    """Undo handler registration done by any test, including via ``cli.main``."""
    handlers = dict(formats.HANDLERS)
    extractors = dict(formats.TEXT_EXTRACTORS)
    yield
    formats.HANDLERS.clear()
    formats.HANDLERS.update(handlers)
    formats.TEXT_EXTRACTORS.clear()
    formats.TEXT_EXTRACTORS.update(extractors)
