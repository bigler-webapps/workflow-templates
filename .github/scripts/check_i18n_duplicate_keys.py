#!/usr/bin/env python3
"""Fail when an i18n translations JSON file contains duplicate object keys."""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


TRANSLATIONS_PATHS = (
    Path("i18n/translations.json"),
    Path("src/i18n/translations.json"),
)


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def find_translation_files(frontend_root: Path) -> list[Path]:
    return [
        frontend_root / relative_path
        for relative_path in TRANSLATIONS_PATHS
        if (frontend_root / relative_path).is_file()
    ]


def load_duplicates(path: Path) -> list[tuple[str, list[Any]]]:
    duplicates: list[tuple[str, list[Any]]] = []

    def object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        values_by_key: defaultdict[str, list[Any]] = defaultdict(list)
        for key, value in pairs:
            values_by_key[key].append(value)

        for key, values in values_by_key.items():
            if len(values) > 1:
                duplicates.append((key, values))

        return dict(pairs)

    try:
        with path.open("r", encoding="utf-8") as handle:
            json.load(handle, object_pairs_hook=object_pairs_hook)
    except json.JSONDecodeError as exc:
        fail(f"Could not parse translations file {path}: {exc}")

    return duplicates


def format_duplicates(path: Path, duplicates: list[tuple[str, list[Any]]]) -> str:
    lines = [f"Duplicate i18n keys in {path}:"]
    for key, values in duplicates:
        lines.append(f"- {key}: {len(values)} occurrences")
        lines.extend(
            f"  {index}. {json.dumps(value, ensure_ascii=False, sort_keys=True)}"
            for index, value in enumerate(values, start=1)
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check i18n translations JSON files for duplicate object keys."
    )
    parser.add_argument(
        "frontend_root",
        nargs="?",
        default=".",
        help="Frontend root containing i18n/translations.json or src/i18n/translations.json.",
    )
    args = parser.parse_args()

    frontend_root = Path(args.frontend_root)
    translation_files = find_translation_files(frontend_root)
    if not translation_files:
        print(f"No translations file found under {frontend_root}; skipping.")
        return

    failures = [
        format_duplicates(path, duplicates)
        for path in translation_files
        if (duplicates := load_duplicates(path))
    ]
    if failures:
        fail("\n\n".join(failures))

    print("No duplicate i18n keys found.")


if __name__ == "__main__":
    main()
