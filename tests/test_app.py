"""Streamlit AppTest flows for app.py: convert, toggle reasoning, clear, errors.

Run from the repo root:

    venv-arena-to-md/bin/python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, ".")

from streamlit.testing.v1 import AppTest  # noqa: E402

APP = str(Path(__file__).resolve().parent.parent / "app.py")


def chat_html(messages: list[dict], catalog: dict | None = None) -> str:
    """A minimal arena.ai-style page with inline content and reasoning."""
    state = {"initialState": {"id": "sid", "title": "T", "messages": messages}}
    payloads = ["a1:" + json.dumps(state)]
    if catalog:
        payloads.append("a2:" + json.dumps(catalog))
    chunks = "".join(
        "<script>self.__next_f.push([1,%s])</script>" % json.dumps(p)
        for p in payloads
    )
    return "<html><head></head><body>" + chunks + "</body></html>"


def chat_with_reasoning() -> str:
    return chat_html(
        [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "The answer.",
                "reasoning": "Thinking.",
                "modelId": "m1",
            },
        ],
        catalog={"initialModels": [{"id": "m1", "displayName": "Model One"}]},
    )


class AppFlowTests(unittest.TestCase):
    def setUp(self):
        self.at = AppTest.from_file(APP)

    # ---------- helpers ----------
    def preview(self) -> str:
        mds = [el.value for el in self.at.markdown if "## You" in (el.value or "")]
        self.assertEqual(len(mds), 1)
        return mds[0]

    # ---------- paste mode ----------
    def test_paste_convert_and_toggle(self):
        at = self.at
        at.run()
        self.assertFalse(at.checkbox[0].value)  # default drops reasoning
        at.radio[0].set_value("Paste source code").run()
        at.text_area[0].set_value(chat_with_reasoning()).run()
        at.button[0].click().run()
        self.assertEqual(len(self.at.error), 0)
        self.assertNotIn("Thought process", self.preview())

        # Toggling the checkbox re-converts without another Convert click.
        at.checkbox[0].check().run()
        self.assertEqual(len(self.at.error), 0)
        self.assertIn("**Thought process**\n\n> Thinking.", self.preview())

        at.checkbox[0].uncheck().run()
        self.assertNotIn("Thought process", self.preview())

    def test_paste_empty_and_garbage(self):
        at = self.at
        at.run()
        at.radio[0].set_value("Paste source code").run()
        at.text_area[0].set_value("").run()
        at.button[0].click().run()
        self.assertEqual(
            [e.value for e in at.error], ["Paste the source code first."]
        )

        at.text_area[0].set_value("<html><body>nothing</body></html>").run()
        at.button[0].click().run()
        self.assertTrue(
            any("arena.ai chat" in e.value for e in at.error), [e.value for e in at.error]
        )

        # A valid paste recovers.
        at.text_area[0].set_value(chat_with_reasoning()).run()
        at.button[0].click().run()
        self.assertEqual(len(at.error), 0)
        self.assertIn("The answer.", self.preview())

    # ---------- upload mode ----------
    def test_upload_convert_and_toggle(self):
        at = self.at
        at.run()
        at.file_uploader[0].set_value(
            ("chat.html", chat_with_reasoning().encode("utf-8"), "text/html")
        ).run()
        self.assertEqual(len(at.error), 0)
        self.assertNotIn("Thought process", self.preview())

        at.checkbox[0].check().run()
        self.assertIn("> Thinking.", self.preview())

        at.checkbox[0].uncheck().run()
        self.assertNotIn("Thought process", self.preview())

    def test_upload_clear_hides_results(self):
        at = self.at
        at.run()
        at.file_uploader[0].set_value(
            ("chat.html", chat_with_reasoning().encode("utf-8"), "text/html")
        ).run()
        self.assertIn("The answer.", self.preview())
        at.file_uploader[0].set_value(None).run()
        self.assertEqual(
            [el for el in at.markdown if "## You" in (el.value or "")], []
        )

    def test_upload_non_utf8(self):
        at = self.at
        at.run()
        at.file_uploader[0].set_value(("bad.html", b"\xff\xfe\xfa", "text/html")).run()
        self.assertEqual(
            [e.value for e in at.error], ["Could not read that file as UTF-8 text."]
        )


if __name__ == "__main__":
    unittest.main()