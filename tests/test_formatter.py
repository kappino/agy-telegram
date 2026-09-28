"""
Tests for text formatting and splitting functions.
"""

from agy_telegram.core.formatter import split_text, markdown_to_telegram_html

def test_split_text_short():
    sample = "This is a short text."
    assert split_text(sample, 100) == [sample]

def test_split_text_long():
    lines = [f"Line {i} of test content" for i in range(100)]
    full_text = "\n".join(lines)
    chunks = split_text(full_text, max_chunk=200)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 250

def test_markdown_to_telegram_html():
    raw = "# Heading 1\nThis is **bold** and `code`."
    html_out = markdown_to_telegram_html(raw)
    assert "<b>" in html_out
    assert "<code>code</code>" in html_out
    assert "🚀 <b>Heading 1</b>" in html_out

def test_markdown_code_block():
    raw = "```python\nprint('hello world')\n```"
    html_out = markdown_to_telegram_html(raw)
    assert '<pre><code class="language-python">' in html_out
    assert "print(&#x27;hello world&#x27;)" in html_out
