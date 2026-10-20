from email.message import Message
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


def load_script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = spec_from_file_location(name, path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("legacy", "expression", "expected"), [
    ("MIT License", None, "MIT"),
    ("BSD", None, "NOASSERTION"),
    ("arbitrary prose", None, "NOASSERTION"),
    ("MIT", "Apache-2.0 OR MIT", "Apache-2.0 OR MIT"),
    ("MIT", "invalid expression", "NOASSERTION"),
    ("", None, "NOASSERTION"),
])
def test_declared_licenses_are_valid_and_explicit(legacy, expression, expected):
    metadata = Message()
    metadata["License"] = legacy
    if expression:
        metadata["License-Expression"] = expression
    assert load_script("generate_sbom").declared_license(metadata) == expected
