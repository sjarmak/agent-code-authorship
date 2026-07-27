"""Extension to language, and which paths are worth reading at all.

Two judgements live here. The extension map decides what counts as a language,
and the skip list decides what is not really this project's code: vendored
dependencies, generated output, minified bundles, test fixtures. Both affect
every downstream number, so they are in one file rather than scattered.

TypeScript and JavaScript stay separate here and are pooled later, in the
estimator, where the reason for pooling is a data one.
"""
from __future__ import annotations

EXT_LANG = {
    ".go": "Go", ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript", ".java": "Java", ".c": "C",
    ".cc": "C++", ".cpp": "C++", ".h": "C", ".hpp": "C++", ".rs": "Rust",
    ".rb": "Ruby", ".cs": "C#", ".kt": "Kotlin", ".swift": "Swift",
    ".scala": "Scala", ".php": "PHP", ".m": "Objective-C", ".mm": "Objective-C",
}

SKIP_FRAGMENTS = (
    "vendor/",
    "node_modules/",
    "third_party/",
    "testdata/",
    "fixture/",
    "fixtures/",
    "generated",
    ".pb.go",
    ".min.js",
    "dist/",
    "build/",
)

HASH_COMMENT = {"Python", "Ruby", "Shell", "YAML"}
CURLY_COMMENT = {"TypeScript", "JavaScript", "Go", "Java", "C", "C++", "C#",
                 "Rust", "Kotlin", "Swift", "Scala", "PHP", "Objective-C"}


def ext_of(path: str) -> str:
    dot = path.rfind(".")
    return path[dot:].lower() if dot != -1 else ""


def lang_of(path: str) -> str | None:
    return EXT_LANG.get(ext_of(path))


def is_code_path(path: str) -> bool:
    return bool(path) and ext_of(path) in EXT_LANG


def SKIP_PATH(path: str) -> bool:
    return any(fragment in path for fragment in SKIP_FRAGMENTS)
