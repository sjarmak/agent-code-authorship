"""CLI for the terminal Sourcegraph AI-ban catalog."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from authorship.sourcegraph_ai_ban_catalog import (
    AiBanCatalogError,
    catalog_input_paths,
    materialize_ai_ban_catalog,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument(
        "--language-inventory",
        type=Path,
        default=Path("study/sourcegraph-ai-ban-languages.v3.json"),
    )
    parser.add_argument("--pin", action="append", default=[], metavar="ID=SHA256")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("study/sourcegraph-ai-ban-catalog.v3.json"),
    )
    return parser


def _pins(values: Sequence[str]) -> dict[str, str]:
    pins = {}
    for value in values:
        input_id, separator, digest = value.partition("=")
        if not separator or not input_id or input_id in pins:
            raise AiBanCatalogError("pins must be unique ID=SHA256 pairs")
        pins[input_id] = digest
    return pins


def _write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    language_path = args.language_inventory
    if not language_path.is_absolute():
        language_path = args.repo_root / language_path
    paths = catalog_input_paths(
        args.repo_root,
        language_inventory_path=language_path,
    )
    catalog = materialize_ai_ban_catalog(paths, _pins(args.pin))
    _write(args.output, catalog)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
