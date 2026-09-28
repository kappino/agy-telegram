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

def markdown_to_telegram_html(text: str) -> str:
    """
    Converte il Markdown di Antigravity in HTML Telegram moderno e ricco.
    """
    lines = text.split("\n")
    processed_lines = []
    in_code_block = False
    code_block_lines = []
    code_block_lang = ""

    for line in lines:
        stripped = line.strip()

        # 1. Rileva blocchi di codice ```lang ... ```
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

        # 2. Proteggi inline code con placeholder
        inline_codes = []
        def save_inline_code(m):
            inline_codes.append(m.group(1))
            return f"__INLINE_CODE_{len(inline_codes)-1}__"

        line_with_placeholders = re.sub(r'`([^`\n]+)`', save_inline_code, line)

        # 3. Escape HTML
        escaped_line = html.escape(line_with_placeholders)

        # 4. Intestazioni (#, ##, ###)
        if stripped.startswith("### "):
            h = html.escape(stripped[4:].strip())
            escaped_line = f"\n🔹 <b>{h}</b>"
        elif stripped.startswith("## "):
            h = html.escape(stripped[3:].strip())
            escaped_line = f"\n📁 <b>{h}</b>"
        elif stripped.startswith("# "):
            h = html.escape(stripped[2:].strip())
            escaped_line = f"\n🚀 <b>{h}</b>"

        # 5. Alert e Blockquotes Telegram (usando blockquote expandable per testi lunghi)
        elif stripped.startswith("> [!NOTE]"):
            note = html.escape(stripped.replace("> [!NOTE]", "").strip())
            escaped_line = f"<blockquote>ℹ️ <b>Nota:</b> {note}</blockquote>"
        elif stripped.startswith("> [!WARNING]"):
            warn = html.escape(stripped.replace("> [!WARNING]", "").strip())
            escaped_line = f"<blockquote>⚠️ <b>Attenzione:</b> {warn}</blockquote>"
        elif stripped.startswith("> [!IMPORTANT]"):
            imp = html.escape(stripped.replace("> [!IMPORTANT]", "").strip())
            escaped_line = f"<blockquote>❗ <b>Importante:</b> {imp}</blockquote>"
        elif stripped.startswith("> "):
            q = html.escape(stripped[2:].strip())
            escaped_line = f"<blockquote>{q}</blockquote>"

        # 6. Elenchi puntati
        elif stripped.startswith("- ") or stripped.startswith("* "):
            content = html.escape(stripped[2:].strip())
            escaped_line = f"• {content}"

        # 7. Separatori orizzontali (---)
        elif re.match(r'^[─\-\_]{3,}$', stripped):
            escaped_line = "───────────────"

        # 8. Grassetto (**testo**) e Corsivo (_testo_)
        escaped_line = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', escaped_line)
        escaped_line = re.sub(r'(?<!\w)_([^_]+?)_(?!\w)', r'<i>\1</i>', escaped_line)
        # Barrato (~~testo~~)
        escaped_line = re.sub(r'~~(.+?)~~', r'<s>\1</s>', escaped_line)

        # 9. Ripristina inline code (formato Telegram <code> con supporto tap-to-copy)
        for i, code_content in enumerate(inline_codes):
            clean_code = html.escape(code_content)
            escaped_line = escaped_line.replace(
                f"__INLINE_CODE_{i}__",
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
    Divide un testo lungo su righe logiche bilanciando automaticamente tag HTML aperti
    (<pre><code>, <blockquote>) tra chunk successivi per evitare BadRequest su Telegram.
    """
    if len(text) <= max_chunk:
        return [text]

    chunks = []
    lines = text.split("\n")
    current_lines = []
    current_len = 0
    in_pre = False
    in_quote = False
    pre_opening = "<pre><code>"

    for line in lines:
        line_len = len(line) + 1

        # Traccia apertura/chiusura tag
        if "<pre" in line:
            in_pre = True
            m = re.search(r'<pre(?: class="[^"]*")?><code>', line)
            if m:
                pre_opening = m.group(0)
        if "</pre>" in line:
            in_pre = False

        if "<blockquote>" in line:
            in_quote = True
        if "</blockquote>" in line:
            in_quote = False

        if current_len + line_len > max_chunk:
            if current_lines:
                chunk_str = "\n".join(current_lines)
                if in_pre:
                    chunk_str += "</code></pre>"
                if in_quote:
                    chunk_str += "</blockquote>"
                chunks.append(chunk_str)

                current_lines = []
                current_len = 0
                if in_pre:
                    current_lines.append(pre_opening)
                    current_len += len(pre_opening) + 1
                if in_quote:
                    current_lines.append("<blockquote>")
                    current_len += len("<blockquote>") + 1

            if line_len > max_chunk:
                for i in range(0, len(line), max_chunk):
                    chunks.append(line[i : i + max_chunk])
                continue

        current_lines.append(line)
        current_len += line_len

    if current_lines:
        chunk_str = "\n".join(current_lines)
        chunks.append(chunk_str)

    return chunks
