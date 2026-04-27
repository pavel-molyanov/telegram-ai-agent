"""HTML rendering and splitting for Telegram messages.

Extracted from `core/handlers/streaming.py`. Telegram accepts a small
whitelist of HTML tags (b, i, u, s, code, pre, a) plus a handful of
entity escapes (&amp; &lt; &gt; &quot; &#N; &#xHEX;); anything else
must be escaped. These helpers:

- `markdown_to_html` — GFM markdown → Telegram HTML subset.
- `sanitize_html` — escape stray HTML while preserving whitelisted tags.
- `split_html_message` — format + split into <=4096-char chunks with
  tag balancing across chunk boundaries.

Pipeline (split_html_message):
  1. markdown → HTML with code-block placeholders.
  2. sanitize_html — escape outside placeholders.
  3. Restore placeholders.
  4. Newline-aware split.
  5. _balance_html_tags per chunk.
"""

from __future__ import annotations

import re

_TG_MSG_LIMIT = 4096

# Real (unescaped) whitelisted tags. Order matters — attributed forms
# (<a href="...">, <code class="...">) must match BEFORE the generic
# simple-tag pattern so the attributes don't get dropped by the shorter
# match.  All patterns are strict: no trailing whitespace, no extra attrs —
# keeps the whitelist hard to bypass.
_REAL_A_HREF_RE = re.compile(r'<a\s+href="([^"]*)">')
_REAL_CODE_CLASS_RE = re.compile(r'<code\s+class="([^"]*)">')
_REAL_SIMPLE_TAGS_RE = re.compile(r"</?(?:b|i|u|s|code|pre|a)>")

# Valid HTML entity *start*: &amp; &lt; &gt; &quot; plus decimal (&#N;) and
# hex (&#xHEX;) numeric refs. Named entities beyond the 4 core ones
# (&nbsp;, &copy;, ...) are deliberately NOT preserved — Telegram only
# accepts the 4 core + numeric forms, so treating e.g. &nbsp; as a valid
# entity would let unsupported markup survive into the final HTML and
# trip parse errors. Current behaviour: &amp;nbsp; in output.
_STRAY_AMP_RE = re.compile(r"&(?!(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);)")


def _escape_amp_smart(text: str) -> str:
    """Escape stray & to &amp;; leave valid entities (&amp; &lt; &gt; &quot; &#N; &#xHEX;) alone."""
    return _STRAY_AMP_RE.sub("&amp;", text)


def _smart_escape(text: str) -> str:
    """HTML-escape <, >, ", and stray & — without double-escaping existing entities."""
    text = _escape_amp_smart(text)
    text = text.replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    return text


def _markdown_escape(s: str) -> str:
    """HTML-escape user content before it goes into a code placeholder.

    Prevents literal `<`, `>`, `&` inside `` `...` `` or ``` ```...``` ```
    from ever reaching sanitize_html in unescaped form — the placeholder
    body would then be treated as a real whitelisted tag on restore.
    """
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _markdown_to_html_parts(text: str) -> tuple[str, dict[str, str]]:
    """Internal: markdown → HTML with code blocks left as opaque placeholders.

    Returns ``(text_with_placeholders, placeholders)`` where placeholders
    are null-byte-framed markers (no `<`, `>`, `&`) that survive
    `sanitize_html` intact. Call sites that want a single-string result
    use the public `markdown_to_html` wrapper.

    Call sites that pass the output through `sanitize_html` (i.e.
    `split_html_message`) must restore placeholders AFTER sanitize.

    Content inside code placeholders is HTML-escaped at extraction time,
    so later sanitize passes that unescape-then-reescape never wake it
    back into a real tag.
    """
    if not text:
        return text, {}

    placeholders: dict[str, str] = {}
    counter = [0]

    def _store(html: str) -> str:
        key = f"\x00MDPH{counter[0]}\x00"
        counter[0] += 1
        placeholders[key] = html
        return key

    def _replace_fenced(m: re.Match[str]) -> str:
        lang = _markdown_escape(m.group(1).strip())
        code = _markdown_escape(m.group(2))
        tag = f'<code class="language-{lang}">' if lang else "<code>"
        return _store(f"<pre>{tag}{code}</code></pre>")

    text = re.sub(r"```([^\n`]*)\n(.*?)```", _replace_fenced, text, flags=re.DOTALL)

    def _replace_inline_code(m: re.Match[str]) -> str:
        return _store(f"<code>{_markdown_escape(m.group(1))}</code>")

    text = re.sub(r"`([^`\n]+)`", _replace_inline_code, text)

    # ATX headings: # / ## / ### … at line start → <b>text</b>
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # Inline elements (order matters — bold before italic).
    # No re.DOTALL: emphasis must not span newlines to avoid false positives
    # when lone asterisks appear in unrelated lines (e.g. "price*3" and "4*tax").
    text = re.sub(r"\*\*([^\n]+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"~~([^\n]+?)~~", r"<s>\1</s>", text)
    # Links: allow one level of balanced parens in URL (e.g. Wikipedia URLs).
    text = re.sub(
        r"\[([^\]]+)\]\(([^()]+(?:\([^)]*\)[^()]*)*)\)",
        r'<a href="\2">\1</a>',
        text,
    )
    # *italic* — not preceded/followed by * (avoids matching inside **)
    text = re.sub(r"(?<!\*)\*(?!\*)([^\n]+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    # _italic_ — only when underscore is NOT adjacent to a word char or another
    # underscore on either side (prevents snake_case and __dunder__ from becoming italic)
    text = re.sub(r"(?<!\w)_(?![_\s])([^\n]+?)(?<![_\s])_(?!\w)", r"<i>\1</i>", text)

    return text, placeholders


def markdown_to_html(text: str) -> str:
    """Convert GFM markdown to Telegram HTML subset.

    Handles: **bold**, *italic*, _italic_, ~~strike~~,
    `inline code`, fenced code blocks, [links](url), # headings.

    Code blocks are extracted first so their content is never converted.
    Already-valid HTML tags are left untouched — sanitize_html handles them.
    Unsupported markdown (tables, blockquotes, HR) passes through as-is.

    Notes:
    - __dunder__ is NOT converted to bold to avoid false positives on Python
      identifiers like __init__. Use **bold** instead.
    - Inline emphasis does not span newlines (prevents stray asterisks from
      accidentally matching across paragraphs).
    """
    result, placeholders = _markdown_to_html_parts(text)
    for key, value in placeholders.items():
        result = result.replace(key, value)
    return result


def sanitize_html(text: str) -> str:
    """Escape all HTML except Telegram-allowed tags (placeholder-based).

    Strategy:
    1. Match real (unescaped) whitelisted tags — attributed variants first
       (<a href="...">, <code class="...">), then simple pairs. Store each
       match under an opaque null-byte-framed placeholder. Normalise `&` in
       href/class attributes (raw → &amp;) at store time.
    2. Smart-escape the remainder: <, >, " always; & only when NOT part of
       a valid entity (&amp; &lt; &gt; &quot; &#N; &#xHEX;).
    3. Restore placeholders back into the sanitised text.

    Why this instead of the old unescape → escape → revive trick: the old
    approach revived whitelisted tags from their ESCAPED form, so entity-
    escaped prose like `&lt;code&gt;` first got unescaped to `<code>`
    (step 1) and then revived as a real tag (step 3), inverting the
    author's intent. The placeholder approach only extracts tags that
    were ALREADY real (unescaped) at the input boundary — entity-escaped
    text stays literal.
    """
    if not text:
        return text

    placeholders: dict[str, str] = {}
    counter = [0]

    def _store(tag: str) -> str:
        key = f"\x00SHP{counter[0]}\x00"
        counter[0] += 1
        placeholders[key] = tag
        return key

    # Match order: attributed tags first so their attributes aren't dropped
    # by the shorter simple-tag pattern grabbing the opening `<code>`.
    text = _REAL_A_HREF_RE.sub(
        lambda m: _store(f'<a href="{_escape_amp_smart(m.group(1))}">'),
        text,
    )
    text = _REAL_CODE_CLASS_RE.sub(
        lambda m: _store(f'<code class="{_escape_amp_smart(m.group(1))}">'),
        text,
    )
    text = _REAL_SIMPLE_TAGS_RE.sub(lambda m: _store(m.group(0)), text)

    text = _smart_escape(text)

    for key, value in placeholders.items():
        text = text.replace(key, value)
    return text


_BALANCE_TAG_RE = re.compile(r"<(/?)([a-z]+)(?:\s+[^>]*)?>", re.IGNORECASE)
_BALANCE_TAGS = frozenset({"b", "i", "u", "s", "code", "pre", "a"})


def _balance_html_tags(chunk: str) -> str:
    """Ensure every Telegram-subset tag in *chunk* is balanced.

    Three failure modes this repairs:
      1. A multi-line ``<pre><code>…</code></pre>`` block that
         `split_html_message` cut in two — the first chunk ends with
         open ``<pre><code>``, the second starts with orphan
         ``</code></pre>``.
      2. CC writing literal tag names in prose (outside backticks) that
         `sanitize_html` revives into real tags — e.g. an un-closed
         ``<code>`` mentioned as text.
      3. Mismatched nesting: ``<pre><code>x</pre></code>`` (unusual but
         observed when CC mirrors documentation that intermixes tag
         names). Plain stack-pop would treat ``</pre>`` as orphan and
         leave ``</code>`` inside ``<pre>`` unclosed. We instead emit
         intermediate closers until the stack top matches the incoming
         closer, mirroring how real HTML parsers recover.

    Strategy: single-pass over `chunk` with a stack of open tags.
    Closers that match stack top just pop. Closers whose name appears
    deeper in the stack close intermediate tags first (inserted into
    the rewritten output). Closers with no matching opener in stack
    become orphans and trigger a prepend of openers at the start.
    Unclosed tags at EOF become a suffix of closers. Empty input and
    already-balanced input are returned unchanged.

    ``<a href="…">`` loses its href when re-opened by the prepend path —
    but the anchor text survives, which is better than a
    ``TelegramBadRequest``. Idempotent on already-balanced input.
    """
    stack: list[str] = []
    orphans: list[str] = []
    out: list[str] = []
    cursor = 0

    for m in _BALANCE_TAG_RE.finditer(chunk):
        name = m.group(2).lower()
        if name not in _BALANCE_TAGS:
            continue
        if m.group(1):  # closing tag
            if stack and stack[-1] == name:
                stack.pop()
            elif name in stack:
                # Mismatched nesting — close intermediates until this name matches.
                # Insert synthetic closers BEFORE the current closer in output.
                out.append(chunk[cursor : m.start()])
                while stack and stack[-1] != name:
                    out.append(f"</{stack.pop()}>")
                out.append(m.group(0))  # the real closer
                if stack and stack[-1] == name:
                    stack.pop()
                cursor = m.end()
            else:
                orphans.append(name)
        else:
            stack.append(name)

    if not stack and not orphans and not out:
        return chunk

    out.append(chunk[cursor:])
    body = "".join(out)

    prefix = "".join(f"<{n}>" for n in reversed(orphans))
    suffix = "".join(f"</{n}>" for n in reversed(stack))
    return prefix + body + suffix


def split_html_message(text: str, limit: int = _TG_MSG_LIMIT) -> list[str]:
    """Format *text* as Telegram HTML and split into chunks each <= *limit* chars.

    Pipeline:
      1. `_markdown_to_html_parts` — markdown → HTML with code placeholders
         holding escaped content (so literal `<pre>` inside backticks can
         never resurface as a real tag).
      2. `sanitize_html` — escape stray HTML outside placeholders.
         Placeholder markers (`\\x00MDPH{N}\\x00`) contain no `<`, `>`, `&`
         so they pass through untouched.
      3. Restore placeholders with their pre-escaped, ready-to-send HTML.
      4. Newline-aware split at <= *limit* char boundaries.
      5. `_balance_html_tags` on every chunk — handles the residual case of
         a multi-line ``<pre>`` block crossing a chunk boundary, plus any
         stray tag revived by sanitize.

    Caveat: split still happens at newline boundaries inside a <pre>.
    The balancer closes/re-opens the code block across chunks — two
    pre-blocks back-to-back look slightly worse than one unbroken block,
    but Telegram accepts it.
    """
    with_placeholders, placeholders = _markdown_to_html_parts(text)
    sanitized = sanitize_html(with_placeholders)
    for key, value in placeholders.items():
        sanitized = sanitized.replace(key, value)
    formatted = sanitized

    if len(formatted) <= limit:
        return [_balance_html_tags(formatted)]

    result: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in formatted.split("\n"):
        line_len = len(line)
        sep = 1 if current else 0  # "\n" separator between lines

        if current_len + sep + line_len > limit:
            if current:
                result.append("\n".join(current))
                current, current_len = [], 0

            if line_len > limit:
                # Single line exceeds limit — fall back to character split.
                # This may still break a tag, but only for pathologically long
                # lines without newlines, which are rare in practice.
                for i in range(0, line_len, limit):
                    result.append(line[i : i + limit])
            else:
                current = [line]
                current_len = line_len
        else:
            current.append(line)
            current_len += sep + line_len

    if current:
        result.append("\n".join(current))

    # Fallback: only reachable if formatted is non-empty but every line is empty
    # (e.g. text == "\n" * N), which produces an empty result list. Guard against
    # returning [] so callers always get at least one chunk.
    chunks = result or [formatted[:limit]]
    return [_balance_html_tags(c) for c in chunks]
