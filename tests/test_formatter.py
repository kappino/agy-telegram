"""
Tests for text formatting and splitting functions.
"""

import unittest
from agy_telegram.core.formatter import split_text, markdown_to_telegram_html


class TestFormatter(unittest.TestCase):
    def test_split_text_short(self):
        sample = "This is a short text."
        self.assertEqual(split_text(sample, 100), [sample])

    def test_split_text_long(self):
        lines = [f"Line {i} of test content" for i in range(100)]
        full_text = "\n".join(lines)
        chunks = split_text(full_text, max_chunk=200)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 250)

    def test_markdown_to_telegram_html(self):
        raw = "# Heading 1\nThis is **bold** and `code`."
        html_out = markdown_to_telegram_html(raw)
        self.assertIn("<b>", html_out)
        self.assertIn("<code>code</code>", html_out)
        self.assertIn("🚀 <b>Heading 1</b>", html_out)

    def test_markdown_code_block(self):
        raw = "```python\nprint('hello world')\n```"
        html_out = markdown_to_telegram_html(raw)
        self.assertIn('<pre><code class="language-python">', html_out)
        self.assertIn("print(&#x27;hello world&#x27;)", html_out)


if __name__ == "__main__":
    unittest.main()
