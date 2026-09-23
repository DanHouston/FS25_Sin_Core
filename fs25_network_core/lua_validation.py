"""Small, dependency-free validation for the FS25 Lua 5.1 runtime dialect.

The repository does not ship a GIANTS Lua compiler.  FS25 nevertheless has a
strictly older Lua parser than current desktop Lua installations, so the
release builder performs a source/archive gate for syntax constructs that are
known not to load in FS25.  A real ``luac`` can still be run separately when
one is available; this module keeps the release gate deterministic everywhere.
"""

from __future__ import annotations

import re


class LuaValidationError(ValueError):
    """The source is not compatible with the FS25 Lua runtime dialect."""


_UNSUPPORTED = (
    (re.compile(r"\b(?:goto|continue)\b"), "goto/continue statements are not supported by the FS25 Lua parser"),
    (re.compile(r"::[A-Za-z_][A-Za-z0-9_]*::"), "Lua labels are not supported by the FS25 Lua parser"),
)


def _long_bracket_end(source: str, opening: int):
    """Return ``(content_start, closing_marker)`` for a Lua long bracket."""
    if opening >= len(source) or source[opening] != "[":
        return None
    cursor = opening + 1
    while cursor < len(source) and source[cursor] == "=":
        cursor += 1
    if cursor >= len(source) or source[cursor] != "[":
        return None
    return cursor + 1, "]" + source[opening + 1:cursor] + "]"


def _mask_non_code(source: str) -> str:
    """Blank comments and string literals while preserving line positions."""
    masked = list(source)
    cursor = 0
    length = len(source)

    def blank(start: int, end: int) -> None:
        for index in range(start, end):
            if source[index] not in "\r\n":
                masked[index] = " "

    while cursor < length:
        if source.startswith("--", cursor):
            long_comment = _long_bracket_end(source, cursor + 2)
            if long_comment is not None:
                content_start, closing = long_comment
                end = source.find(closing, content_start)
                if end < 0:
                    raise LuaValidationError("unterminated long comment")
                end += len(closing)
                blank(cursor, end)
                cursor = end
                continue
            end = cursor + 2
            while end < length and source[end] not in "\r\n":
                end += 1
            blank(cursor, end)
            cursor = end
            continue

        if source[cursor] in "'\"":
            quote = source[cursor]
            end = cursor + 1
            while end < length:
                if source[end] == "\\":
                    end += 2
                    continue
                if source[end] == quote:
                    end += 1
                    break
                if source[end] in "\r\n":
                    raise LuaValidationError("unterminated short string")
                end += 1
            else:
                raise LuaValidationError("unterminated short string")
            blank(cursor, end)
            cursor = end
            continue

        long_string = _long_bracket_end(source, cursor)
        if long_string is not None:
            content_start, closing = long_string
            end = source.find(closing, content_start)
            if end < 0:
                raise LuaValidationError("unterminated long string")
            end += len(closing)
            blank(cursor, end)
            cursor = end
            continue

        cursor += 1

    return "".join(masked)


def validate_fs25_lua_source(source: str | bytes, name: str = "<source>") -> None:
    """Validate source against the Lua dialect accepted by FS25.

    This deliberately focuses on deterministic FS25 compatibility checks.  It
    rejects the Lua 5.2+ control-flow syntax that caused the live load failure
    while ignoring occurrences inside comments and strings.
    """
    if isinstance(source, bytes):
        try:
            source = source.decode("utf-8")
        except UnicodeDecodeError as error:
            raise LuaValidationError(f"{name}: source is not UTF-8") from error
    if not isinstance(source, str) or not source.strip():
        raise LuaValidationError(f"{name}: source is empty")
    try:
        masked = _mask_non_code(source)
    except LuaValidationError as error:
        raise LuaValidationError(f"{name}: {error}") from error
    for pattern, reason in _UNSUPPORTED:
        match = pattern.search(masked)
        if match is not None:
            line = masked.count("\n", 0, match.start()) + 1
            raise LuaValidationError(f"{name}:{line}: {reason}")

