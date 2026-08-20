"""Print the CHANGELOG.md section for one version, for use as release notes.

Stdlib only, exit non-zero if the section is missing — a release without a
changelog entry is a release that should not happen.

Usage: python3 .github/scripts/extract_changelog.py 0.1.0
"""

import re
import sys
from pathlib import Path


def extract(changelog: str, version: str) -> str:
    heading = re.compile(r"^## \[(?P<version>[^\]]+)\]", re.MULTILINE)
    matches = list(heading.finditer(changelog))
    for i, match in enumerate(matches):
        if match.group("version") == version:
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(changelog)
            body = changelog[start:end]
            # Drop the rest of the heading line (the " - YYYY-MM-DD" tail).
            body = body.split("\n", 1)[1] if "\n" in body else ""
            return body.strip()
    raise SystemExit(f"CHANGELOG.md has no section for version {version}")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: extract_changelog.py <version>")
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    print(extract(changelog, sys.argv[1]))


if __name__ == "__main__":
    main()
