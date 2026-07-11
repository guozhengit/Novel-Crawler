"""Shared deterministic URL path normalization."""

from __future__ import annotations

import re
from urllib.parse import quote, unquote_to_bytes, urlsplit

_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")


def canonical_path(url_or_path: str) -> str:
    value = urlsplit(url_or_path).path if "://" in url_or_path else url_or_path
    if _BAD_PERCENT.search(value):
        raise ValueError("URL path contains a malformed percent escape")
    raw_parts = (value or "/").split("/")
    normalized_parts: list[str] = []
    for part in raw_parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if normalized_parts:
                normalized_parts.pop()
            continue
        canonical = _canonical_segment(part)
        if "%" in part and canonical in {".", ".."}:
            canonical = canonical.replace(".", "%2E")
        normalized_parts.append(canonical)
    normalized = "/" + "/".join(normalized_parts)
    if value.endswith("/") and normalized != "/":
        normalized += "/"
    return normalized


def _canonical_segment(segment: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(segment):
        if segment[index] != "%":
            output.append(quote(segment[index], safe="-._~"))
            index += 1
            continue
        start = index
        while index < len(segment) and segment[index] == "%":
            index += 3
        raw = unquote_to_bytes(segment[start:index])
        try:
            decoded = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("URL path is not valid UTF-8") from exc
        for character in decoded:
            if character.isascii() and (character.isalnum() or character in "-._~"):
                output.append(character)
            else:
                output.append("".join(f"%{byte:02X}" for byte in character.encode("utf-8")))
    return "".join(output)


def path_template(path: str) -> str:
    canonical = canonical_path(path)
    parts = canonical.split("/")[1:]
    output: list[str] = []
    for index, part in enumerate(parts):
        decoded = unquote_to_bytes(part).decode("utf-8")
        if re.fullmatch(r"[0-9]+", decoded):
            output.append("{int}")
        elif index > 0 and re.fullmatch(r"[A-Za-z][A-Za-z0-9._~-]*-[A-Za-z0-9._~-]+", decoded):
            output.append("{slug}")
        else:
            output.append(part)
    return "/" + "/".join(output)


def sibling_template(first: str, second: str) -> str | None:
    first_parts = canonical_path(first).split("/")[1:]
    second_parts = canonical_path(second).split("/")[1:]
    if len(first_parts) != len(second_parts):
        return None
    differences = [index for index, pair in enumerate(zip(first_parts, second_parts, strict=True)) if pair[0] != pair[1]]
    if len(differences) != 1:
        return None
    index = differences[0]
    left, right = first_parts[index], second_parts[index]
    left_tokens = re.findall(r"%[0-9A-F]{2}|[0-9]+|[^0-9%]+", left)
    right_tokens = re.findall(r"%[0-9A-F]{2}|[0-9]+|[^0-9%]+", right)
    if len(left_tokens) != len(right_tokens):
        return None
    differing_runs: list[int] = []
    for token_index, (left_token, right_token) in enumerate(zip(left_tokens, right_tokens, strict=True)):
        is_digit_run = re.fullmatch(r"[0-9]+", left_token) is not None and re.fullmatch(r"[0-9]+", right_token) is not None
        if is_digit_run:
            if left_token != right_token:
                differing_runs.append(token_index)
        elif left_token != right_token:
            return None
    if len(differing_runs) != 1:
        return None
    left_tokens[differing_runs[0]] = "{int}"
    merged = [*first_parts]
    merged[index] = "".join(left_tokens)
    return "/" + "/".join(merged)
