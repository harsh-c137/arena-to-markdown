"""Regression tests for extract.py.

Run from the repo root:

    venv-arena-to-md/bin/python -m unittest discover -s tests

Covers both flight-payload save formats (old: one row per push chunk; new:
several rows per chunk), rows split across chunks, ``$id`` text-chunk refs
with exact UTF-16 lengths, view-source unwrapping, message-shape robustness,
and the keep/drop reasoning (thought process) flag.
"""

from __future__ import annotations

import json
import sys
import unittest
from unittest import mock

sys.path.insert(0, ".")

import extract  # noqa: E402


def make_html(payloads: list[str]) -> str:
    """Build a page whose script tags push the given flight payloads."""
    chunks = "".join(
        "<script>self.__next_f.push([1,%s])</script>" % json.dumps(p)
        for p in payloads
    )
    return "<html><head></head><body>" + chunks + "</body></html>"


def utf16_len(text: str) -> int:
    """JS ``string.length``: UTF-16 code units (astral chars count as two)."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def t_row(row_id: str, text: str) -> str:
    """A flight text row: ``id:T<len>,<raw text of exactly that length>``."""
    return f"{row_id}:T{utf16_len(text)},{text}"


def split_everywhere(text: str, parts: int) -> list[str]:
    """Cut text into ``parts`` chunks at arbitrary byte-ish boundaries."""
    size = max(1, len(text) // parts)
    return [text[i : i + size] for i in range(0, len(text), size)]


def state_fixture(messages: list[dict]) -> dict:
    return {"initialState": {"id": "sid", "title": "T", "messages": messages}}


CATALOG = {
    "initialModels": [
        {"id": "m1", "displayName": "Model One", "publicName": "m-one"},
        {"id": "mA", "displayName": "Alpha"},
        {"id": "mB", "displayName": "Beta"},
    ]
}

STATE_REFS = state_fixture(
    [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "$c01", "reasoning": "$e01", "modelId": "m1"},
    ]
)

STATE_INLINE = state_fixture(
    [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "The answer.",
            "reasoning": "Thinking.",
            "modelId": "m1",
        },
    ]
)

CONTENT_TEXT = "The final answer."
REASONING_TEXT = "Step 1: think.\n\nStep 2: answer."  # multi-line


def stream_rows() -> str:
    """A flight stream with refs, a catalog, and the state row."""
    return "\n".join(
        [
            '99:"EvaluationStoreProvider"',
            '9a:I[792735,["$39"],"$99"]',
            t_row("c01", CONTENT_TEXT),
            t_row("e01", REASONING_TEXT),
            "38:" + json.dumps(STATE_REFS),
            "39:" + json.dumps(CATALOG),
        ]
    )


class OldFormatTests(unittest.TestCase):
    """One flight row per push chunk: markers and values are separate pushes."""

    def payloads(self) -> list[str]:
        return [
            'aa:"ModelStoreProvider"',
            f"c01:T{utf16_len(CONTENT_TEXT)},",
            CONTENT_TEXT,
            f"e01:T{utf16_len(REASONING_TEXT)},",
            REASONING_TEXT,
            "ad:" + json.dumps(STATE_REFS),
            "ae:" + json.dumps(CATALOG),
        ]

    def test_refs_and_reasoning_resolve(self):
        markdown, stats = extract.html_to_markdown(make_html(self.payloads()), True)
        self.assertIn("## Model One", markdown)
        self.assertIn(CONTENT_TEXT, markdown)
        # multi-line reasoning survives as a blockquote
        self.assertIn("> Step 1: think.\n>\n> Step 2: answer.", markdown)
        self.assertEqual(stats["reasoning_messages"], 1)

    def test_reasoning_dropped_by_default(self):
        html = make_html(self.payloads())
        markdown_on, _ = extract.html_to_markdown(html, True)
        markdown_off, _ = extract.html_to_markdown(html, False)
        markdown_default, _ = extract.html_to_markdown(html)
        self.assertNotIn("Thought process", markdown_off)
        self.assertNotIn("Step 1", markdown_off)
        self.assertIn(CONTENT_TEXT, markdown_off)
        self.assertEqual(markdown_off, markdown_default)
        self.assertNotEqual(markdown_on, markdown_off)


class NewFormatTests(unittest.TestCase):
    """Several flight rows packed into a single push chunk."""

    def html(self) -> str:
        return make_html([stream_rows()])

    def test_state_found_in_multi_row_chunk(self):
        markdown, stats = extract.html_to_markdown(self.html())
        self.assertIn("# T", markdown)
        self.assertIn("## You", markdown)
        self.assertIn("## Model One", markdown)
        self.assertIn(CONTENT_TEXT, markdown)
        self.assertEqual(stats["user_messages"], 1)
        self.assertEqual(stats["assistant_messages"], 1)
        self.assertEqual(stats["reasoning_messages"], 1)

    def test_reasoning_toggle(self):
        on, _ = extract.html_to_markdown(self.html(), True)
        off, _ = extract.html_to_markdown(self.html(), False)
        self.assertIn(
            "**Thought process**\n\n> Step 1: think.\n>\n> Step 2: answer.", on
        )
        self.assertNotIn("Thought process", off)
        self.assertNotIn("Step 1", off)
        self.assertIn(CONTENT_TEXT, off)


class SplitRowTests(unittest.TestCase):
    """Next.js may split a flight row across several push chunks."""

    def test_state_row_split_across_chunks(self):
        stream = stream_rows()
        payloads = split_everywhere(stream, 5)
        markdown, stats = extract.html_to_markdown(make_html(payloads))
        self.assertIn("## Model One", markdown)
        self.assertIn(CONTENT_TEXT, markdown)
        self.assertEqual(stats["user_messages"], 1)
        self.assertEqual(stats["assistant_messages"], 1)

    def test_reasoning_resolved_when_split(self):
        stream = stream_rows()
        payloads = split_everywhere(stream, 7)
        on, _ = extract.html_to_markdown(make_html(payloads), True)
        self.assertIn("> Step 2: answer.", on)

    def test_unicode_text_row(self):
        thought = "Thought 🎉 done."  # astral char: 2 UTF-16 units
        state = state_fixture(
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "answer", "reasoning": "$e01"},
            ]
        )
        stream = "\n".join(
            [
                t_row("e01", thought),
                "38:" + json.dumps(state),
            ]
        )
        markdown, _ = extract.html_to_markdown(make_html([stream]), True)
        self.assertIn(f"> {thought}", markdown)
        self.assertIn("## Assistant", markdown)


class ReasoningShapeTests(unittest.TestCase):
    def test_list_of_blocks(self):
        state = state_fixture(
            [
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": "answer",
                    "reasoning": [{"text": "Block one."}, {"text": "Block two."}],
                    "modelId": "m1",
                },
            ]
        )
        html = make_html(["a1:" + json.dumps(state)])
        markdown, stats = extract.html_to_markdown(html, True)
        self.assertIn("> Block one.\n>\n> Block two.", markdown)
        self.assertEqual(stats["reasoning_messages"], 1)

    def test_list_of_strings(self):
        state = state_fixture(
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "answer", "reasoning": ["one", "two"]},
            ]
        )
        html = make_html(["a1:" + json.dumps(state)])
        markdown, stats = extract.html_to_markdown(html, True)
        self.assertIn("> one\n>\n> two", markdown)
        self.assertEqual(stats["reasoning_messages"], 1)

    def test_non_string_reasoning_ignored(self):
        state = state_fixture(
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "answer", "reasoning": 7},
            ]
        )
        html = make_html(["a1:" + json.dumps(state)])
        markdown, stats = extract.html_to_markdown(html, True)
        self.assertNotIn("Thought process", markdown)
        self.assertEqual(stats["reasoning_messages"], 0)

    def test_no_reasoning_is_noop(self):
        state = state_fixture(
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "answer", "modelId": "m1"},
            ]
        )
        html = make_html(["a1:" + json.dumps(state), "a2:" + json.dumps(CATALOG)])
        self.assertEqual(
            extract.html_to_markdown(html, True)[0],
            extract.html_to_markdown(html, False)[0],
        )

    def test_comparison_turns_keep_reasoning(self):
        state = state_fixture(
            [
                {"role": "user", "content": "compare"},
                {
                    "role": "assistant",
                    "content": "answer A",
                    "reasoning": "thought A",
                    "modelId": "mA",
                    "participantPosition": "a",
                },
                {
                    "role": "assistant",
                    "content": "answer B",
                    "reasoning": "thought B",
                    "modelId": "mB",
                    "participantPosition": "b",
                },
            ]
        )
        html = make_html(["a1:" + json.dumps(state), "a2:" + json.dumps(CATALOG)])
        markdown, stats = extract.html_to_markdown(html, True)
        self.assertIn("## Alpha (A)", markdown)
        self.assertIn("## Beta (B)", markdown)
        self.assertIn("> thought A", markdown)
        self.assertIn("> thought B", markdown)
        self.assertEqual(stats["comparison_turns"], 1)
        self.assertEqual(stats["reasoning_messages"], 2)


class MessageShapeTests(unittest.TestCase):
    """Unusual field shapes must not crash, only degrade gracefully."""

    def state(self, **overrides) -> str:
        assistant = {
            "role": "assistant",
            "content": "answer",
            "modelId": "m1",
            "reasoning": "thought",
        }
        assistant.update(overrides)
        state = state_fixture(
            [
                {"role": "user", "content": "hello"},
                assistant,
            ]
        )
        return make_html(["a1:" + json.dumps(state), "a2:" + json.dumps(CATALOG)])

    def test_content_as_list_of_blocks(self):
        html = self.state(content=[{"text": "block one"}, {"text": "block two"}])
        markdown, _ = extract.html_to_markdown(html, True)
        self.assertIn("block one\n\nblock two", markdown)

    def test_unhashable_model_id(self):
        markdown, _ = extract.html_to_markdown(self.state(modelId=["m1"]), True)
        self.assertIn("## Assistant", markdown)

    def test_non_string_participant_position(self):
        markdown, _ = extract.html_to_markdown(
            self.state(participantPosition=["a"]), True
        )
        self.assertIn("## Model One", markdown)  # no (A) suffix, no crash

    def test_attachments_as_dict(self):
        markdown, _ = extract.html_to_markdown(
            self.state(
                experimental_attachments={
                    "one": {"name": "a.png", "url": "https://x/a.png"}
                }
            ),
            True,
        )
        self.assertIn("- [a.png](https://x/a.png)", markdown)

    def test_non_string_title(self):
        state = state_fixture(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "answer"},
            ]
        )
        state["initialState"]["title"] = 12345
        html = make_html(["a1:" + json.dumps(state)])
        markdown, _ = extract.html_to_markdown(html)
        self.assertIn("# 12345", markdown)

    def test_blank_title(self):
        state = state_fixture(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "answer"},
            ]
        )
        state["initialState"]["title"] = "   "
        html = make_html(["a1:" + json.dumps(state)])
        markdown, _ = extract.html_to_markdown(html)
        self.assertIn("# Arena chat", markdown)


class ViewSourceTests(unittest.TestCase):
    def test_unwrap_and_convert(self):
        inner = make_html(
            ["a1:" + json.dumps(STATE_INLINE), "a2:" + json.dumps(CATALOG)]
        )
        escaped = inner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        rows = "".join(
            f'<tr><td class="line-number" value="{i}"></td>'
            f'<td class="line-content">{line}</td></tr>'
            for i, line in enumerate(escaped.splitlines(), start=1)
        )
        page = (
            "<!-- saved from url=(0042)https://arena.ai/c/sid -->"
            "<html><head></head><body>"
            '<div class="line-gutter-backdrop"></div>'
            f'<table class="line-wrap"><tbody>{rows}</tbody></table>'
            "</body></html>"
        )
        markdown, stats = extract.html_to_markdown(page, True)
        self.assertIn("Source: https://arena.ai/c/sid", markdown)
        self.assertIn("## Model One", markdown)
        self.assertIn("> Thinking.", markdown)
        self.assertIn("The answer.", markdown)
        self.assertEqual(stats["assistant_messages"], 1)


class ErrorTests(unittest.TestCase):
    def test_no_payload_raises(self):
        with self.assertRaises(ValueError):
            extract.html_to_markdown("<html><body>nothing</body></html>")

    def test_no_state_raises(self):
        with self.assertRaises(ValueError):
            extract.html_to_markdown(make_html(['aa:"EvaluationStoreProvider"']))

    def test_unexpected_error_becomes_value_error(self):
        """Internal crashes surface as a clean, diagnosable ValueError."""
        with mock.patch.object(extract, "to_markdown", side_effect=TypeError("boom")):
            with self.assertRaises(ValueError) as ctx:
                extract.html_to_markdown(
                    make_html(["a1:" + json.dumps(STATE_INLINE)])
                )
            self.assertIn("TypeError: boom", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()