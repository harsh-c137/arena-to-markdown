#!/usr/bin/env python3
"""Streamlit app: upload an arena.ai Direct-chat HTML save, download Markdown."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from extract import html_to_markdown

st.set_page_config(page_title="Arena chat → Markdown", page_icon="💬", layout="centered")

st.markdown(
    """
    <style>
    [data-testid="stDownloadButton"] button {
        background-color: #16a34a !important;
        border-color: #16a34a !important;
        color: #fff !important;
    }
    [data-testid="stDownloadButton"] button:hover {
        background-color: #15803d !important;
        border-color: #15803d !important;
        color: #fff !important;
    }
    .arena-alert {
        background-color: rgba(28, 131, 225, 0.1);
        border: 1px solid rgba(28, 131, 225, 0.2);
        border-radius: 0.5rem;
        padding: 1rem 1.15rem;
        margin-bottom: 1rem;
    }
    .arena-alert ul {
        margin: 0.35rem 0;
        padding-left: 1.2rem;
    }
    .arena-alert li {
        margin: 0.2rem 0;
    }
    .arena-alert details {
        margin: 0.55rem 0 0.55rem 0.15rem;
    }
    .arena-alert summary {
        cursor: pointer;
        font-weight: 400;
    }
    .arena-alert ol {
        margin: 0.45rem 0 0.15rem 0;
        padding-left: 1.3rem;
    }
    .arena-alert p {
        margin: 0.5rem 0 0.1rem 0;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Arena chat → Markdown")
st.caption("Turn a saved arena.ai Direct chat into a downloadable Markdown file.")

st.markdown(
    """
<div class="arena-alert">
<ul>
  <li>Grab the source code of your Arena AI chat and upload or paste it down below</li>
</ul>
<details>
  <summary>How to get source code</summary>
  <ol>
    <li>Open your chat on arena.ai</li>
    <li>View source code (Ctrl + U) or (Command ⌘ + Option ⌥ + U)</li>
    <li>(Ctrl +S) or (Command ⌘ + S) to save as HTML file</li>
  </ol>
</details>
<ul>
  <li>Upload <strong>"Direct"</strong> chats only. No "Side by Side", "Agent Mode" or "Battle Mode" chats yet</li>
  <li>If Arena slipped a random two-model comparison into a Direct chat, both replies are kept and labeled A and B</li>
  <li>Keep or drop the model's thought process (reasoning) with the checkbox below</li>
</ul>
</div>
    """,
    unsafe_allow_html=True,
)

source = st.radio(
    "How do you want to provide the source code?",
    ("Upload HTML file", "Paste source code"),
    horizontal=True,
)

if st.session_state.get("input_mode") != source:
    st.session_state.input_mode = source
    st.session_state.input_raw = None

raw: str | None = None
download_name = "arena-chat.md"

if source == "Upload HTML file":
    uploaded = st.file_uploader(
        "Arena chat HTML",
        type=["html", "htm"],
        help="The saved Direct chat page, including Chrome view-source dumps.",
    )
    if uploaded is not None:
        try:
            raw = uploaded.getvalue().decode("utf-8")
        except UnicodeDecodeError:
            st.error("Could not read that file as UTF-8 text.")
            st.stop()
        download_name = f"{Path(uploaded.name).stem or 'arena-chat'}.md"
    else:
        # Upload removed: drop the last conversion for good.
        st.session_state.input_raw = None
else:
    pasted = st.text_area(
        "Paste the page source here",
        height=240,
        placeholder="Paste the full HTML from View Source…",
    )
    if st.button("Convert to Markdown", type="secondary"):
        raw = pasted.strip()
        if not raw:
            st.error("Paste the source code first.")
            st.stop()

keep_reasoning = st.checkbox(
    "Include the model's thought process (reasoning)",
    help="Model replies on Arena can come with their chain of thought. "
    "Keep it in the Markdown or export only the final answers.",
)

# Paste mode only sets raw on the Convert click, so fall back to the last
# committed source. This keeps the export in sync when the checkbox changes.
if raw is None:
    raw = st.session_state.get("input_raw")
if raw is None:
    st.stop()
st.session_state.input_raw = raw

try:
    markdown, stats = html_to_markdown(raw, keep_reasoning=keep_reasoning)
except ValueError as exc:
    st.session_state.input_raw = None
    st.error(str(exc))
    st.stop()

st.success(
    f"Extracted **{stats['user_messages']}** of your messages and "
    f"**{stats['assistant_messages']}** model replies."
)
if stats["comparison_turns"]:
    st.warning(
        f"Found **{stats['comparison_turns']}** two-model comparison "
        f"{'turn' if stats['comparison_turns'] == 1 else 'turns'}. "
        "Both replies were kept."
    )
if stats["reasoning_messages"]:
    if keep_reasoning:
        st.info(
            f"Kept the thought process of **{stats['reasoning_messages']}** model "
            "replies. Uncheck the box above to drop it."
        )
    else:
        st.info(
            f"**{stats['reasoning_messages']}** model replies had a thought "
            "process, which was left out. Check the box above to include it."
        )

st.download_button(
    label="Download Markdown",
    data=markdown.encode("utf-8"),
    file_name=download_name,
    mime="text/markdown",
    type="primary",
)

with st.expander("Preview", expanded=True):
    st.markdown(markdown)
