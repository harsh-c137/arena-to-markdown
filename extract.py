"""Extract an arena.ai Direct-chat HTML save into Markdown.

Keeps both replies when Arena injects a one-off two-model comparison.
"""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup

PUSH_RE = re.compile(r"""self\.__next_f\.push\(\[1,"((?:\\.|[^"\\])*)"\]\)""")
T_STRING_RE = re.compile(r"^([0-9a-f]+):T[0-9a-f]+,$")
REF_RE = re.compile(r"^\$([0-9a-f]+)$")
VIEW_SOURCE_MARKERS = ("line-content", "line-number", "html-tag")
SAVED_FROM_RE = re.compile(
    r"<!--\s*saved from url=\([^)]*\)(https?://[^>\s]+?)\s*-->",
    re.I,
)


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


def text_chunk_table(payloads: list[str]) -> dict[str, str]:
    table: dict[str, str] = {}
    for index, payload in enumerate(payloads):
        match = T_STRING_RE.fullmatch(payload.strip())
        if match and index + 1 < len(payloads):
            table[match.group(1)] = payloads[index + 1]
    return table


def resolve_content(content: Any, chunks: dict[str, str]) -> str:
    if not isinstance(content, str):
        return "" if content is None else str(content)
    match = REF_RE.fullmatch(content.strip())
    if match:
        return chunks.get(match.group(1), content)
    return content


def json_after_flight_id(payload: str) -> Any | None:
    stripped = payload.lstrip()
    colon = stripped.find(":")
    if colon == -1:
        return None
    body = stripped[colon + 1 :]
    if not body or body[0] not in "[{":
        return None
    try:
        return json.loads(body)
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


def conversation_state(payloads: list[str]) -> dict[str, Any]:
    for payload in payloads:
        if '"messages"' not in payload or '"role"' not in payload:
            continue
        parsed = json_after_flight_id(payload)
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


def model_names(payloads: list[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    for payload in payloads:
        if '"initialModels"' not in payload:
            continue
        parsed = json_after_flight_id(payload)
        if parsed is None:
            continue
        catalog = walk_find(parsed, is_model_catalog)
        if catalog is None:
            continue
        for model in catalog["initialModels"]:
            model_id = model.get("id")
            if not model_id:
                continue
            names[model_id] = (
                model.get("displayName")
                or model.get("publicName")
                or model.get("name")
                or model_id
            )
        break
    return names


def chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        message
        for message in messages
        if message.get("role") in {"user", "assistant"}
    ]


def heading_for(message: dict[str, Any], names: dict[str, str]) -> str:
    if message.get("role") == "user":
        return "You"
    model_id = message.get("modelId")
    return names.get(model_id, "Assistant") if model_id else "Assistant"


def attachments_markdown(message: dict[str, Any]) -> str:
    attachments = message.get("experimental_attachments") or []
    if not attachments:
        return ""
    lines = ["**Attachments:**"]
    for attachment in attachments:
        name = attachment.get("name") or "file"
        url = attachment.get("url")
        if url:
            lines.append(f"- [{name}]({url})")
        else:
            lines.append(f"- {name}")
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
) -> str:
    header: list[str] = []
    title = (state.get("title") or "Arena chat").strip().splitlines()[0]
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
        if (
            role == "assistant"
            and message.get("participantPosition") in {"a", "b"}
            and (prev_role == "assistant" or next_role == "assistant")
        ):
            heading = f"{heading} ({message['participantPosition'].upper()})"

        body = resolve_content(message.get("content"), chunks).strip()
        files = attachments_markdown(message)
        block = f"## {heading}\n\n"
        if files:
            block += files + "\n"
        if body:
            block += body + "\n"
        turns.append(block.rstrip())

    parts = ["\n".join(header)] + turns
    return "\n\n---\n\n".join(parts).rstrip() + "\n"


def html_to_markdown(raw: str) -> tuple[str, dict[str, int]]:
    saved_from = extract_saved_from_url(raw)
    html = unwrap_chrome_view_source(raw) if looks_like_view_source(raw) else raw
    if saved_from is None:
        saved_from = extract_saved_from_url(html)

    payloads = flight_payloads(html)
    if not payloads:
        raise ValueError(
            "No arena.ai chat payload found. Upload a saved Direct chat HTML file."
        )

    chunks = text_chunk_table(payloads)
    state = conversation_state(payloads)
    names = model_names(payloads)
    url = source_url(html, saved_from, state)
    messages = chat_messages(state.get("messages") or [])
    markdown = to_markdown(state, chunks, names, url)
    stats = {
        "user_messages": sum(1 for message in messages if message.get("role") == "user"),
        "assistant_messages": sum(
            1 for message in messages if message.get("role") == "assistant"
        ),
        "comparison_turns": comparison_turn_count(messages),
    }
    return markdown, stats
