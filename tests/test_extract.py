"""Regression tests for extract.py.

Run from the repo root:

    venv-arena-to-md/bin/python -m unittest discover -s tests

Covers both flight-payload save formats (old: one row per push chunk; new:
several rows per chunk), ``$id`` text-chunk refs, view-source unwrapping, and
the keep/drop reasoning (thought process) flag.
"""

from __future__ import annotations

import json
import sys
import unittest

sys.path.insert(0, ".")

import extract  # noqa: E402


def make_html(payloads: list[str]) -> str:
    """Build a page whose script tags push the given flight payloads."""
    chunks = "".join(
        "<script>self.__next_f.push([1,%s])</script>" % json.dumps(p)
        for p in payloads
    )
    return "<html><head></head><body>" + chunks + "</body></html>"


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


class OldFormatTests(unittest.TestCase):
    """One flight row per push chunk; ``$id`` refs point at T-chunk texts."""

    def payloads(self) -> list[str]:
        return [
            'aa:"ModelStoreProvider"',
            "c01:T123,",
            "The final answer.",
            "e01:T456,",
            "Step 1: think.\n\nStep 2: answer.",  # multi-line chunk text
            "ad:" + json.dumps(STATE_REFS),
            "ae:" + json.dumps(CATALOG),
        ]

    def test_refs_and_reasoning_resolve(self):
        markdown, stats = extract.html_to_markdown(make_html(self.payloads()), True)
        self.assertIn("## Model One", markdown)
        self.assertIn("The final answer.", markdown)
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
        self.assertIn("The final answer.", markdown_off)
        self.assertEqual(markdown_off, markdown_default)
        self.assertNotEqual(markdown_on, markdown_off)


class NewFormatTests(unittest.TestCase):
    """Several ``id:json`` rows packed into a single push chunk."""

    def html(self) -> str:
        rows = "\n".join(
            [
                '99:"EvaluationStoreProvider"',
                '9a:I[792735,["$39"],"$99"]',
                "c01:T123,",          # content ref marker
                "The final answer.",   # content ref text (next row)
                "e01:T456,",           # reasoning ref marker
                "Thinking step.",      # reasoning ref text (next row)
                "38:" + json.dumps(STATE_REFS),
                "39:" + json.dumps(CATALOG),
            ]
        )
        return make_html([rows])

    def test_state_found_in_multi_row_chunk(self):
        markdown, stats = extract.html_to_markdown(self.html())
        self.assertIn("# T", markdown)
        self.assertIn("## You", markdown)
        self.assertIn("## Model One", markdown)
        self.assertIn("The final answer.", markdown)
        self.assertEqual(stats["user_messages"], 1)
        self.assertEqual(stats["assistant_messages"], 1)
        self.assertEqual(stats["reasoning_messages"], 1)

    def test_reasoning_toggle(self):
        on, _ = extract.html_to_markdown(self.html(), True)
        off, _ = extract.html_to_markdown(self.html(), False)
        self.assertIn("**Thought process**\n\n> Thinking step.", on)
        self.assertNotIn("Thought process", off)
        self.assertNotIn("Thinking step", off)


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


if __name__ == "__main__":
    unittest.main()