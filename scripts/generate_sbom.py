"""Generate an SPDX 2.3 JSON inventory (sbom.spdx.json) from the pinned runtime environment.

Run inside an environment installed from requirements.txt. The output is not committed.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from importlib.metadata import distribution
from pathlib import Path
from uuid import uuid4

from packaging.licenses import InvalidLicenseExpression, canonicalize_license_expression

ROOT = Path(__file__).resolve().parents[1]


def _pins(path: Path) -> list[tuple[str, str]]:
    pins = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip()
        if " @ " in value:
            # A direct wheel reference: the version is the wheel name's second field.
            name, url = value.split(" @ ", 1)
            pins.append((name, url.rsplit("/", 1)[1].split("-")[1]))
        elif value:
            name, version = value.split("==", 1)
            pins.append((name, version))
    return pins


def declared_license(metadata) -> str:
    """Preserve explicit expressions; never guess a variant from ambiguous labels."""
    value = metadata.get("License-Expression") or metadata.get("License", "")
    aliases = {"MIT License": "MIT", "Apache 2.0": "Apache-2.0", "ISC License": "ISC"}
    value = aliases.get(value.strip(), value.strip())
    if not value or value == "NOASSERTION":
        return "NOASSERTION"
    try:
        return canonicalize_license_expression(value)
    except InvalidLicenseExpression:
        return "NOASSERTION"


def build(requirements: Path) -> dict:
    packages = []
    for index, (name, expected) in enumerate(_pins(requirements), start=1):
        metadata = distribution(name)
        if metadata.version != expected:
            raise ValueError(f"{name}: installed {metadata.version}, expected {expected}")
        license_name = declared_license(metadata.metadata)
        packages.append(
            {
                "SPDXID": f"SPDXRef-Package-{index}",
                "name": name,
                "versionInfo": expected,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": license_name or "NOASSERTION",
            }
        )
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "panda-runtime",
        "documentNamespace": f"https://local.invalid/panda/sbom/0.1.0/{uuid4()}",
        "creationInfo": {
            "creators": ["Tool: panda-generate-sbom"],
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),  # noqa: UP017
        },
        "packages": packages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "sbom.spdx.json")
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(build(ROOT / "requirements.txt"), indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
