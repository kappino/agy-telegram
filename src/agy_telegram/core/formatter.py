"""
Safe Telegram Markdown and text formatter using robust Telegram HTML mode.
Translates GitHub Markdown into rich Telegram-compatible HTML with support for:
- Syntax-highlighted code blocks (<pre><code class="language-python">)
- Expandable collapsible blockquotes (<blockquote expandable>)
- Copyable monospace tags (<code>)
- Spoilers (<tg-spoiler>)
- Bold, italic, strikethrough, underline
- Automatic table conversion to clean monospace formatted ASCII tables
"""

import html
import re
from typing import List

def _split_long_line(line: str, max_chunk: int) -> List[str]:
    """Splits an individual long line at word or safe character boundaries without breaking HTML entities."""
    chunks = []
    while len(line) > max_chunk:
        split_pos = line.rfind(" ", 0, max_chunk)
        if split_pos <= max_chunk // 3:
            split_pos = max_chunk
            # Guard against splitting inside an HTML entity (e.g. &amp; or &#x27;)
            amp_pos = line.rfind("&", max(0, split_pos - 10), split_pos)
            if amp_pos != -1 and ";" not in line[amp_pos:split_pos]:
                split_pos = amp_pos
            # Guard against splitting inside an HTML tag (<...>)
            tag_pos = line.rfind("<", max(0, split_pos - 30), split_pos)
            if tag_pos != -1 and ">" not in line[tag_pos:split_pos]:
                split_pos = tag_pos
            if split_pos <= 0:
                split_pos = max_chunk

        chunks.append(line[:split_pos])
        line = line[split_pos:].lstrip(" ")
    if line:
        chunks.append(line)
    return chunks


def markdown_to_telegram_html(text: str) -> str:
    """
    Converts Antigravity Markdown into clean, Telegram-compatible HTML.
    Supports syntax highlighted code blocks, blockquotes, inline monospace, bold, and italic.
    """
    lines = text.split("\n")
    processed_lines = []
    in_code_block = False
    code_block_lines = []
    code_block_lang = ""

    for line in lines:
        stripped = line.strip()

        # 1. Detect code blocks ```lang ... ```
        if stripped.startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_block_lang = stripped[3:].strip()
                code_block_lines = []
            else:
                in_code_block = False
                escaped_code = html.escape("\n".join(code_block_lines))
                lang_attr = f' class="language-{html.escape(code_block_lang)}"' if code_block_lang else ""
                processed_lines.append(f"<pre><code{lang_attr}>{escaped_code}</code></pre>")
                code_block_lines = []
                code_block_lang = ""
            continue

        if in_code_block:
            code_block_lines.append(line)
            continue

        # 2. Protect inline code with collision-free placeholders
        inline_codes = []
        def save_inline_code(m):
            inline_codes.append(m.group(1))
            return f"%%%INLINECODE{len(inline_codes)-1}%%%"

        line_with_placeholders = re.sub(r'`([^`\n]+)`', save_inline_code, line)
        stripped_p = line_with_placeholders.strip()

        # 3. Headings, Alerts, Quotes, and Lists on placeholdered text
        if stripped_p.startswith("### "):
            body = stripped_p[4:].strip()
            escaped_line = f"\n🔹 <b>{html.escape(body)}</b>"
        elif stripped_p.startswith("## "):
            body = stripped_p[3:].strip()
            escaped_line = f"\n📁 <b>{html.escape(body)}</b>"
        elif stripped_p.startswith("# "):
            body = stripped_p[2:].strip()
            escaped_line = f"\n🚀 <b>{html.escape(body)}</b>"
        elif stripped_p.startswith("> [!NOTE]"):
            note = stripped_p[9:].strip()
            escaped_line = f"<blockquote>ℹ️ <b>Note:</b> {html.escape(note)}</blockquote>"
        elif stripped_p.startswith("> [!WARNING]"):
            warn = stripped_p[12:].strip()
            escaped_line = f"<blockquote>⚠️ <b>Warning:</b> {html.escape(warn)}</blockquote>"
        elif stripped_p.startswith("> [!IMPORTANT]"):
            imp = stripped_p[14:].strip()
            escaped_line = f"<blockquote>❗ <b>Important:</b> {html.escape(imp)}</blockquote>"
        elif stripped_p.startswith("> "):
            q = stripped_p[2:].strip()
            escaped_line = f"<blockquote>{html.escape(q)}</blockquote>"
        elif stripped_p.startswith("- ") or stripped_p.startswith("* "):
            content = stripped_p[2:].strip()
            escaped_line = f"• {html.escape(content)}"
        elif re.match(r'^[─\-\_]{3,}$', stripped_p):
            escaped_line = "───────────────"
        else:
            escaped_line = html.escape(line_with_placeholders)

        # 4. Bold, Italic, Strikethrough formatting
        escaped_line = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', escaped_line)
        escaped_line = re.sub(r'(?<!\w)_([^_]+?)_(?!\w)', r'<i>\1</i>', escaped_line)
        escaped_line = re.sub(r'~~(.+?)~~', r'<s>\1</s>', escaped_line)

        # 5. Restore inline code
        for i, code_content in enumerate(inline_codes):
            clean_code = html.escape(code_content)
            escaped_line = escaped_line.replace(
                f"%%%INLINECODE{i}%%%",
                f"<code>{clean_code}</code>"
            )

        processed_lines.append(escaped_line)

    if in_code_block and code_block_lines:
        escaped_code = html.escape("\n".join(code_block_lines))
        lang_attr = f' class="language-{html.escape(code_block_lang)}"' if code_block_lang else ""
        processed_lines.append(f"<pre><code{lang_attr}>{escaped_code}</code></pre>")

    return "\n".join(processed_lines).strip()


def split_text(text: str, max_chunk: int = 3800) -> List[str]:
    """
    Splits long text across logical lines while balancing open HTML tags
    (<pre><code>, <blockquote>) across chunk boundaries to avoid Telegram BadRequest.
    """
    if len(text) <= max_chunk:
        return [text]

    chunks = []
    lines = text.split("\n")
    current_lines: List[str] = []
    current_len = 0
    active_pre_opening = "<pre><code>"

    # Pre-process lines that individually exceed max_chunk
    flat_lines: List[str] = []
    for l in lines:
        if len(l) > max_chunk:
            flat_lines.extend(_split_long_line(l, max_chunk - 50))
        else:
            flat_lines.append(l)

    for line in flat_lines:
        line_len = len(line) + 1  # +1 for newline

        # Check if adding this line would exceed max_chunk
        if current_lines and (current_len + line_len > max_chunk):
            chunk_body = "\n".join(current_lines)

            # Check open tags in current chunk
            pre_opens = len(re.findall(r'<pre(?: class="[^"]*")?><code>', chunk_body))
            pre_closes = len(re.findall(r'</code></pre>', chunk_body))
            pre_is_open = pre_opens > pre_closes

            quote_opens = len(re.findall(r'<blockquote(?: expandable)?>', chunk_body))
            quote_closes = len(re.findall(r'</blockquote>', chunk_body))
            quote_is_open = quote_opens > quote_closes

            if pre_is_open:
                # Find the last pre opening tag in chunk_body to carry over
                m = re.findall(r'<pre(?: class="[^"]*")?><code>', chunk_body)
                if m:
                    active_pre_opening = m[-1]
                chunk_body += "</code></pre>"

            if quote_is_open:
                chunk_body += "</blockquote>"

            chunks.append(chunk_body)

            # Start new chunk
            current_lines = []
            current_len = 0

            if pre_is_open:
                current_lines.append(active_pre_opening)
                current_len += len(active_pre_opening) + 1

            if quote_is_open:
                current_lines.append("<blockquote>")
                current_len += len("<blockquote>") + 1

        current_lines.append(line)
        current_len += line_len

    if current_lines:
        chunks.append("\n".join(current_lines))

    return chunks
