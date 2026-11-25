"""Extract an arena.ai Direct-chat HTML save into Markdown.

Keeps both replies when Arena injects a one-off two-model comparison, and can
include or drop each assistant reply's reasoning (thought process).

The chat lives in Next.js flight payloads (``self.__next_f.push`` script
calls). The chunks are concatenated into one stream and parsed row by row:
Next.js may split a single row across several push chunks, and text rows
(``id:T<len>,``) carry raw text of an exact UTF-16 length that can itself
contain newlines, so rows cannot be recovered reliably from one chunk alone.
"""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup

PUSH_RE = re.compile(r"""self\.__next_f\.push\(\[1,"((?:\\.|[^"\\])*)"\]\)""")
REF_RE = re.compile(r"^\$([0-9a-f]+)$")
ROW_ID_RE = re.compile(r"[0-9a-f]+")
TEXT_ROW_RE = re.compile(r"T(\d+),")
LEGACY_TEXT_ROW_RE = re.compile(r"([0-9a-f]+):T[0-9a-f]+,")
VIEW_SOURCE_MARKERS = ("line-content", "line-number", "html-tag")
SAVED_FROM_RE = re.compile(
    r"<!--\s*saved from url=\([^)]*\)(https?://[^>\s]+?)\s*-->",
    re.I,
)

FlightRow = tuple[str, str, str]  # (row id, kind, value); kind: "json" | "text"


def looks_like_view_source(html: str) -> bool:
    lowered = html[:8000].lower()
    return all(marker in html for marker in VIEW_SOURCE_MARKERS) or (
        "line-gutter-backdrop" in lowered and "line-content" in html
    )


def extract_saved_from_url(html: str) -> str | None:
    match = SAVED_FROM_RE.search(html[:2000])
    return match.group(1) if match else None


def unwrap_chrome_view_source(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    cells = soup.select("td.line-content")
    if not cells:
        raise ValueError(
            "This looks like a Chrome view-source save, but the original page "
            "could not be recovered from it."
        )
    reconstructed = "".join(cell.get_text() for cell in cells).strip()
    if not reconstructed:
        raise ValueError("View-source unwrapping produced an empty document.")
    return reconstructed


def decode_js_string(escaped: str) -> str:
    return json.loads('"' + escaped.replace("\n", "\\n").replace("\r", "\\r") + '"')


def flight_payloads(html: str) -> list[str]:
    return [decode_js_string(chunk) for chunk in PUSH_RE.findall(html)]


def flight_stream(payloads: list[str]) -> str:
    """Concatenate flight push chunks, in document order, into one stream."""
    return "".join(payloads)


def _take_utf16(stream: str, start: int, units: int) -> tuple[str, int]:
    """Consume ``units`` UTF-16 code units (JS ``string.length``) from start."""
    end = start
    while end < len(stream) and units > 0:
        units -= 2 if ord(stream[end]) > 0xFFFF else 1
        end += 1
    return stream[start:end], end


def parse_flight_stream(stream: str) -> list[FlightRow]:
    """Split the flight stream into ``(id, kind, value)`` rows.

    JSON rows end at their next newline (their text never contains a raw
    one). Text rows carry their length after ``T``, in UTF-16 code units, so
    their value may contain anything, including newlines that would otherwise
    look like row boundaries. Lines that do not start with a ``<hex-id>:``
    row id are skipped.
    """
    rows: list[FlightRow] = []
    i = 0
    size = len(stream)
    while i < size:
        if stream[i] == "\n":
            i += 1
            continue
        line_end = stream.find("\n", i)
        if line_end == -1:
            line_end = size
        colon = stream.find(":", i)
        if colon == -1 or colon > line_end:
            i = line_end + 1 if line_end < size else size
            continue
        row_id = stream[i:colon]
        if not ROW_ID_RE.fullmatch(row_id):
            i = line_end + 1 if line_end < size else size
            continue
        text_match = TEXT_ROW_RE.match(stream, colon + 1)
        if text_match:
            value, end = _take_utf16(
                stream, text_match.end(), int(text_match.group(1))
            )
            rows.append((row_id, "text", value))
            i = end
        else:
            value_end = stream.find("\n", colon + 1)
            if value_end == -1:
                value_end = size
            rows.append((row_id, "json", stream[colon + 1 : value_end]))
            i = value_end + 1 if value_end < size else size
    return rows


def flight_rows(payloads: list[str]) -> list[FlightRow]:
    """Parse the flight stream into rows, falling back to legacy saves.

    Current arena.ai saves pack several newline-separated rows into each push
    chunk and may split one row across chunks, so rows are parsed from the
    chunks concatenated in order. Older saves push exactly one row per chunk
    with no separators between chunks; if the stream reading yields no
    conversation state, fall back to that per-chunk reading.
    """
    stream_rows = parse_flight_stream(flight_stream(payloads))
    try:
        conversation_state(stream_rows)
    except ValueError:
        legacy_rows = parse_flight_legacy(payloads)
        try:
            conversation_state(legacy_rows)
        except ValueError:
            pass
        else:
            return legacy_rows
    return stream_rows


def parse_flight_legacy(payloads: list[str]) -> list[FlightRow]:
    """Read one-row-per-chunk (older) saves: marker chunk + value chunk."""
    rows: list[FlightRow] = []
    for index, payload in enumerate(payloads):
        stripped = payload.strip()
        if not stripped:
            continue
        match = LEGACY_TEXT_ROW_RE.fullmatch(stripped)
        if match:
            if index + 1 < len(payloads):
                rows.append((match.group(1), "text", payloads[index + 1]))
            continue
        for line in payload.splitlines():
            line = line.strip()
            if not line:
                continue
            colon = line.find(":")
            if colon <= 0:
                continue
            row_id = line[:colon]
            if not ROW_ID_RE.fullmatch(row_id):
                continue
            rows.append((row_id, "json", line[colon + 1 :]))
    return rows


def text_chunk_table(rows: list[FlightRow]) -> dict[str, str]:
    """Map ``$id`` text-chunk references to their text row values."""
    return {row_id: value for row_id, kind, value in rows if kind == "text"}


def resolve_content(content: Any, chunks: dict[str, str]) -> str:
    if not isinstance(content, str):
        return "" if content is None else str(content)
    match = REF_RE.fullmatch(content.strip())
    if match:
        return chunks.get(match.group(1), content)
    return content


def load_json(value: str) -> Any | None:
    """Parse a flight JSON row value (``[...]`` or ``{...}``)."""
    if not value or value[0] not in "[{":
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def walk_find(obj: Any, predicate) -> Any | None:
    if predicate(obj):
        return obj
    if isinstance(obj, dict):
        for value in obj.values():
            found = walk_find(value, predicate)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = walk_find(value, predicate)
            if found is not None:
                return found
    return None


def is_conversation_state(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("messages"), list)
        and obj["messages"]
        and isinstance(obj["messages"][0], dict)
        and "role" in obj["messages"][0]
        and "content" in obj["messages"][0]
    )


def is_model_catalog(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("initialModels"), list)
        and obj["initialModels"]
        and isinstance(obj["initialModels"][0], dict)
        and "id" in obj["initialModels"][0]
        and "displayName" in obj["initialModels"][0]
    )


def conversation_state(rows: list[FlightRow]) -> dict[str, Any]:
    for _row_id, kind, value in rows:
        if kind != "json" or '"messages"' not in value or '"role"' not in value:
            continue
        parsed = load_json(value)
        if parsed is None:
            continue
        found = walk_find(parsed, is_conversation_state)
        if found is not None:
            return found
        found = walk_find(
            parsed,
            lambda obj: isinstance(obj, dict)
            and is_conversation_state(obj.get("initialState")),
        )
        if found is not None:
            return found["initialState"]
    raise ValueError(
        "Could not find an arena.ai chat in this file. Save the Direct chat "
        "page (or its view-source HTML) and try again."
    )


def model_names(rows: list[FlightRow]) -> dict[str, str]:
    names: dict[str, str] = {}
    for _row_id, kind, value in rows:
        if kind != "json" or '"initialModels"' not in value:
            continue
        parsed = load_json(value)
        if parsed is None:
            continue
        catalog = walk_find(parsed, is_model_catalog)
        if catalog is None:
            continue
        for model in catalog["initialModels"]:
            if not isinstance(model, dict):
                continue
            model_id = model.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            names[model_id] = (
                model.get("displayName")
                or model.get("publicName")
                or model.get("name")
                or model_id
            )
        if names:
            break
    return names


def chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        message
        for message in messages
        if message.get("role") in {"user", "assistant"}
    ]


def text_blocks(value: Any) -> list[str]:
    """Text from a content value: a string or a list of text blocks."""
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    texts: list[str] = []
    for block in value:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            texts.append(block["text"])
    return texts


def message_content(message: dict[str, Any], chunks: dict[str, str]) -> str:
    """Best-effort text of a message's content: string, ``$id`` ref, or blocks."""
    content = message.get("content")
    if isinstance(content, str):
        return resolve_content(content, chunks)
    return "\n\n".join(text_blocks(content))


def reasoning_text(message: dict[str, Any], chunks: dict[str, str]) -> str:
    """Return the message's reasoning (thought process) text, if any."""
    value = message.get("reasoning")
    if isinstance(value, str):
        return resolve_content(value, chunks).strip()
    return "\n\n".join(text_blocks(value)).strip()


def reasoning_block(thought: str) -> str:
    """Render reasoning as a labelled blockquote, shown above the final reply."""
    quoted = "\n".join(
        ">" if not line.strip() else f"> {line}" for line in thought.splitlines()
    )
    return f"**Thought process**\n\n{quoted}"


def heading_for(message: dict[str, Any], names: dict[str, str]) -> str:
    if message.get("role") == "user":
        return "You"
    model_id = message.get("modelId")
    if not isinstance(model_id, str) or not model_id:
        return "Assistant"
    return names.get(model_id, "Assistant")


def attachments_markdown(message: dict[str, Any]) -> str:
    attachments = message.get("experimental_attachments") or []
    if isinstance(attachments, dict):
        attachments = list(attachments.values())
    if not isinstance(attachments, list):
        return ""
    lines = ["**Attachments:**"]
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        name = attachment.get("name") or "file"
        url = attachment.get("url")
        if url:
            lines.append(f"- [{name}]({url})")
        else:
            lines.append(f"- {name}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines) + "\n"


def source_url(html: str, saved_from: str | None, state: dict[str, Any]) -> str | None:
    if saved_from:
        return saved_from
    chat_id = state.get("id")
    if chat_id:
        return f"https://arena.ai/c/{chat_id}"
    return None


def comparison_turn_count(messages: list[dict[str, Any]]) -> int:
    count = 0
    index = 0
    while index < len(messages):
        if messages[index].get("role") != "assistant":
            index += 1
            continue
        run = 0
        while index < len(messages) and messages[index].get("role") == "assistant":
            run += 1
            index += 1
        if run >= 2:
            count += 1
    return count


def to_markdown(
    state: dict[str, Any],
    chunks: dict[str, str],
    names: dict[str, str],
    url: str | None,
    keep_reasoning: bool = False,
) -> str:
    header: list[str] = []
    title = state.get("title") or "Arena chat"
    if not isinstance(title, str):
        title = str(title)
    title = title.strip().splitlines()[0] if title.strip() else "Arena chat"
    if len(title) > 80:
        title = title[:77].rstrip() + "..."
    header.append(f"# {title}")
    if url:
        header.append("")
        header.append(f"Source: {url}")

    messages = chat_messages(state["messages"])
    turns: list[str] = []
    for index, message in enumerate(messages):
        role = message.get("role")
        heading = heading_for(message, names)
        prev_role = messages[index - 1].get("role") if index else None
        next_role = (
            messages[index + 1].get("role") if index + 1 < len(messages) else None
        )
        position = message.get("participantPosition")
        if (
            role == "assistant"
            and isinstance(position, str)
            and position in {"a", "b"}
            and (prev_role == "assistant" or next_role == "assistant")
        ):
            heading = f"{heading} ({position.upper()})"

        body = message_content(message, chunks).strip()
        files = attachments_markdown(message)
        block = f"## {heading}\n\n"
        if files:
            block += files + "\n"
        if keep_reasoning and role == "assistant":
            thought = reasoning_text(message, chunks)
            if thought:
                block += reasoning_block(thought) + "\n\n"
        if body:
            block += body + "\n"
        turns.append(block.rstrip())

    parts = ["\n".join(header)] + turns
    return "\n\n---\n\n".join(parts).rstrip() + "\n"


def html_to_markdown(
    raw: str, keep_reasoning: bool = False
) -> tuple[str, dict[str, int]]:
    saved_from = extract_saved_from_url(raw)
    html = unwrap_chrome_view_source(raw) if looks_like_view_source(raw) else raw
    if saved_from is None:
        saved_from = extract_saved_from_url(html)

    payloads = flight_payloads(html)
    if not payloads:
        raise ValueError(
            "No arena.ai chat payload found. Upload a saved Direct chat HTML file."
        )

    try:
        rows = flight_rows(payloads)
        chunks = text_chunk_table(rows)
        state = conversation_state(rows)
        names = model_names(rows)
        url = source_url(html, saved_from, state)
        messages = chat_messages(state.get("messages") or [])
        markdown = to_markdown(state, chunks, names, url, keep_reasoning)
    except ValueError:
        raise
    except Exception as exc:  # keep the app's error box clean and diagnosable
        raise ValueError(
            f"Could not extract this chat ({type(exc).__name__}: {exc}). "
            "Save the Direct chat page (or its view-source HTML) and try again."
        ) from exc
    stats = {
        "user_messages": sum(1 for message in messages if message.get("role") == "user"),
        "assistant_messages": sum(
            1 for message in messages if message.get("role") == "assistant"
        ),
        "comparison_turns": comparison_turn_count(messages),
        "reasoning_messages": sum(
            1
            for message in messages
            if message.get("role") == "assistant" and reasoning_text(message, chunks)
        ),
    }
    return markdown, stats