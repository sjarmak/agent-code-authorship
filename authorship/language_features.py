"""Language-aware feature extraction for the preregistered primary model."""
from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import PurePosixPath
from typing import Sequence

from authorship.features import (
    CURLY_LANGS,
    DIVIDER,
    ELISION,
    NAMES,
    _code_features,
    _comment_features,
    _shape_features,
    features as baseline_features,
)


PRIMARY_SCHEMA_VERSION = 1
GO_DECLARATION = re.compile(r"^\s*(?:func|type|var|const)\b")


def code_kind(path: str) -> str:
    normalized = path.replace("\\", "/").lower()
    name = PurePosixPath(normalized).name
    if (
        "/test/" in f"/{normalized}"
        or "/tests/" in f"/{normalized}"
        or name.startswith("test_")
        or name.endswith("_test.go")
        or name.endswith("_test.py")
    ):
        return "test"
    return "production"


def _python_parts(lines: Sequence[str]) -> dict:
    text = "\n".join(lines)
    comments: list[str] = []
    comment_by_line: dict[int, tuple[int, str]] = {}
    tokens: list[tokenize.TokenInfo] = []
    try:
        generator = tokenize.generate_tokens(io.StringIO(text).readline)
        while True:
            try:
                tokens.append(next(generator))
            except StopIteration:
                break
            except (tokenize.TokenError, IndentationError):
                break
    except (tokenize.TokenError, IndentationError, SyntaxError):
        tokens = []
    for token in tokens:
        if token.type == tokenize.COMMENT:
            content = token.string[1:].strip()
            comments.append(content)
            comment_by_line[token.start[0] - 1] = (token.start[1], content)

    code_lines = []
    full_indices = set()
    inline = 0
    for index, line in enumerate(lines):
        found = comment_by_line.get(index)
        if found is None:
            code_lines.append(line)
            continue
        column, _ = found
        code = line[:column]
        code_lines.append(code)
        if code.strip():
            inline += 1
        else:
            full_indices.add(index)

    has_docstring = False
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, TypeError):
        tree = None
    if tree is not None:
        nodes = (
            ast.Module,
            ast.ClassDef,
            ast.FunctionDef,
            ast.AsyncFunctionDef,
        )
        has_docstring = any(
            isinstance(node, nodes) and ast.get_docstring(node, clean=False) is not None
            for node in ast.walk(tree)
        )
    return {
        "comments": comments,
        "code_lines": [line for line in code_lines if line.strip()],
        "full_indices": full_indices,
        "inline": inline,
        "doc": has_docstring,
    }


def _go_parts(lines: Sequence[str]) -> dict:
    comments: list[str] = []
    code_lines: list[str] = []
    full_indices = set()
    inline = 0
    in_block = False
    in_raw = False

    for line_index, line in enumerate(lines):
        code: list[str] = []
        comment_parts: list[str] = []
        i = 0
        in_double = False
        in_rune = False
        escaped = False
        while i < len(line):
            if in_block:
                end = line.find("*/", i)
                if end == -1:
                    comment_parts.append(line[i:].strip())
                    i = len(line)
                    continue
                comment_parts.append(line[i:end].strip())
                in_block = False
                i = end + 2
                continue
            char = line[i]
            if in_raw:
                code.append(char)
                if char == "`":
                    in_raw = False
                i += 1
                continue
            if in_double or in_rune:
                code.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif in_double and char == '"':
                    in_double = False
                elif in_rune and char == "'":
                    in_rune = False
                i += 1
                continue
            if line.startswith("//", i):
                comment_parts.append(line[i + 2 :].strip())
                i = len(line)
                continue
            if line.startswith("/*", i):
                in_block = True
                i += 2
                continue
            code.append(char)
            if char == "`":
                in_raw = True
            elif char == '"':
                in_double = True
            elif char == "'":
                in_rune = True
            i += 1
        code_text = "".join(code)
        if code_text.strip():
            code_lines.append(code_text)
        if comment_parts:
            comments.extend(part for part in comment_parts if part)
            if code_text.strip():
                inline += 1
            else:
                full_indices.add(line_index)

    has_doc = False
    for index in sorted(full_indices):
        next_index = index + 1
        while next_index < len(lines) and not lines[next_index].strip():
            next_index += 1
        if next_index < len(lines) and GO_DECLARATION.match(lines[next_index]):
            has_doc = True
            break
    return {
        "comments": comments,
        "code_lines": code_lines,
        "full_indices": full_indices,
        "inline": inline,
        "doc": has_doc,
    }


def primary_features(lines: Sequence[str], lang: str) -> dict[str, float]:
    if not lines:
        return {name: 0.0 for name in NAMES}
    if lang == "Python":
        parts = _python_parts(lines)
    elif lang == "Go":
        parts = _go_parts(lines)
    else:
        return baseline_features(lines, lang)

    comments = parts["comments"]
    code_lines = parts["code_lines"]
    n_code = len(code_lines) or 1
    comment_body = "\n".join(f"// {comment}" for comment in comments)
    out = {
        **_shape_features(lines),
        **_comment_features(comments, n_code),
        **_code_features(code_lines, lang),
        "comment_line_rate": len(parts["full_indices"]) / len(lines),
        "inline_comment_rate": parts["inline"] / n_code,
        "docstring_present": 1.0 if parts["doc"] else 0.0,
        "divider_rate": (
            sum(1 for comment in comments if DIVIDER.search(f"// {comment}"))
            / len(lines)
        ),
        "elision_present": 1.0 if ELISION.search(comment_body) else 0.0,
        "is_python": 1.0 if lang == "Python" else 0.0,
        "is_ts_js": 0.0,
        "is_go": 1.0 if lang == "Go" else 0.0,
        "is_curly_other": 1.0 if lang in CURLY_LANGS - {"Go"} else 0.0,
    }
    return {name: float(out.get(name, 0.0)) for name in NAMES}


def primary_vector(lines: Sequence[str], lang: str) -> list[float]:
    result = primary_features(lines, lang)
    return [result[name] for name in NAMES]
