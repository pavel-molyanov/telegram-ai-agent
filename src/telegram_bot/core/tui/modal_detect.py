"""Send-and-verify delivery: the only reliable way to tell if CC TUI is
accepting keyboard input or a modal is blocking it.

WHY this approach
-----------------
Claude Code TUI blocks keyboard input behind interactive dialogs (trust,
Bash permission, sensitive-file edit, /usage, /mcp, /login, /config,
/model, /status). When a modal is up:
  - `tmux send-keys -l <text>` is a NO-OP — characters do not reach the
    input buffer (verified live on CC 2.1.117).
  - `tmux send-keys Enter` acts as "confirm selected item", which can
    silently approve a shell command, edit a sensitive file, change a
    setting, or switch the model. This is not just lost input — it is a
    potentially dangerous write.

So the bot must NEVER send Enter into a modal. Preflight regex on
`Esc to cancel` works for known dialogs but breaks when CC ships a new
modal with different wording. The robust solution is empirical:
  1. Type the text with `-l` (no-op on modals, harmless otherwise).
  2. Wait a render tick.
  3. Capture the pane.
  4. If the prompt head shows up in the input-bar zone → idle/busy CC
     accepted it → send Enter.
  5. If it does NOT show up → a modal ate the input → alert the user.

Verified in live probes across trust / Bash-permission / /usage / /login
modals (all swallow input) and idle / thinking / counting (all accept
input into the input bar, even while CC is mid-response).

This module exposes the verification primitives. The send+verify+Enter
choreography lives in `tmux_manager.send_direct`.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from typing import TypedDict


class ModalDiagSignals(TypedDict):
    """Structured return of `collect_diagnostic_signals`. Each field
    pinpoints one candidate cause of `BLOCKED reason=modal` rejection."""

    len_pane_before: int
    len_pane_after: int
    pane_equal: bool
    elapsed_ms: int | None
    pane_width: int | None
    has_input_bar_before: bool
    has_input_bar_after: bool
    gate_a_hit: bool
    prompt_head_len: int
    head_in_after_bar: bool


# Head of the prompt to look for in the input bar. Long prompts get wrapped
# across multiple bar lines, so we only need enough uniqueness to confirm
# acceptance. 30 chars is plenty for human messages; also covers slash
# commands like `/model` or short replies like `да`.
_PROMPT_HEAD_CHARS = 30

# Minimum length of the prompt's first line for the first-line-only fallback
# path in `prompt_visible_in_pane` to fire. Below this threshold the first
# line is likely a single word (`"да"`, `"ok"`) that could collide with
# unrelated non-modal content that happens to linger in the pane (thinking
# status text, previous response fragments, scrollback quotes in agent-team
# runs). Modal body text is already rejected earlier via Gate A/B in
# `_input_bar_content`; this guard protects the fallback path from
# non-modal short-substring collisions.
_FIRST_LINE_MIN_LEN = 10

# CC TUI prepends this visual indent to every continuation line (after the
# first) inside the input bar. Empirically verified on CC 2.1.117-2.1.119
# across ASCII / Cyrillic / emoji payloads and pane widths 80-200. If a
# future CC release ships a different indent (e.g. 3 spaces, a tab), bump
# this constant — the rest of the normalization pipeline is width-agnostic
# via `_ws_collapse`, so this is a belt-and-suspenders cleanup rather than
# the primary match mechanism.
_CC_CONTINUATION_INDENT = "  "

# Default settle time after `send-keys -l` before re-capturing. Measured
# on real CC 2.1.117: input bar re-renders within ~100ms; 200ms is safely
# above that with headroom for tmux refresh latency. Tunable via the
# arg on verify_delivery for tests.
DEFAULT_SETTLE_SEC = 0.2

# Upper bound on a single capture-pane invocation. Protects send_direct
# from a wedged tmux server hanging the whole request path indefinitely.
# 5 s is generous — a healthy capture returns in single-digit ms.
_CAPTURE_TIMEOUT_SEC = 5.0

# How many lines from the bottom to scan when hunting for the `❯ ` input-bar
# marker. CC TUI normally renders the input bar within the last ~5 rows with
# a horizontal-line separator above/below it, but long voice transcripts and
# pasted text can wrap the bar across dozens of rows. Keep this wider than
# ordinary pane height; tmux captures may include extra history in tests or
# future runtime probes. Modal detection safety does not rely on this being
# tight: Gate A still scans only the footer, and Gate B requires the framed
# input-bar sandwich rather than a loose `❯` substring.
_INPUT_BAR_SEARCH_LINES = 80

# The Claude input-bar marker: `❯` at the start of a line (after any leading
# whitespace). The same regex is used in `capture.py:_PROMPT_LINE_RE`.
_INPUT_BAR_MARKER_RE = re.compile(r"^\s*❯\s?(?P<rest>.*)$")

# Codex uses a no-frame prompt marker (`›`) instead of Claude's boxed input
# sandwich. It needs a separate extractor so long-paste placeholder detection
# can still use before/after guards.
_CODEX_INPUT_BAR_MARKER_RE = re.compile(r"^\s*›\s?(?P<rest>.*)$")
_CODEX_FOOTER_RE = re.compile(r"\b(gpt-[\w.:-]+|default)\b.*\b(model|effort)\b|·\s*~?/")

# Lines made of `─` box-drawing that delimit the input bar from the
# footer. When we find one AFTER a `❯` line we stop collecting the bar's
# wrap continuation — anything below it is a footer, not the bar.
_BAR_SEPARATOR_RE = re.compile(r"^\s*─{10,}\s*$")

# Belt-and-suspenders: every interactive modal observed on CC 2.1.117
# advertises its dismiss keys in the footer. Non-modal states (idle,
# thinking, compact) use lowercase `esc to interrupt` — the capital-E
# form is the discriminator. This short-circuits to "modal" even if a
# future CC layout accidentally looks like the idle sandwich.
_MODAL_FOOTER_TOKEN_RE = re.compile(
    r"\bEsc to (cancel|clear|exit|dismiss|close)\b|\bEnter to confirm\b"
)

# CC/Codex collapse very long bracketed-paste payloads into literal
# placeholders like `[Pasted text #N]` or `[Pasted Content 1024 chars]`
# inside the input bar — the typed characters never render as such.
# `prompt_visible_in_pane` accepts the placeholder itself as a delivery
# signal, guarded by a count check against pane_before to avoid replaying
# a stale placeholder from a prior round-trip. Verified on CC 2.1.118:
# this placeholder is exclusive to the idle/thinking input bar after a
# successful paste — no known modal renders it inside its body. If a
# future CC release ever echoes the placeholder into a modal, Gate A
# (capital-E footer-token sniff inside `_input_bar_content`) still
# structurally blocks delivery before the placeholder path runs.
_PASTED_PLACEHOLDER_RE = re.compile(
    r"\[(?:Pasted text #\d+(?: \+\d+ lines?)?|Pasted Content \d+ chars)\]",
    re.IGNORECASE,
)

# Modal footers render within 1-3 lines of the pane bottom on CC 2.1.117.
# 5 is a safe margin that also avoids false positives from scrollback
# documentation quoting "Esc to cancel" in text — our
# `historical_mention.txt` fixture has "Esc to cancel" as deep as line 5
# from the bottom, so we stop there. A future CC release with a taller
# footer would need a layout change, at which point this bound updates
# alongside fixtures. Gate B still independently protects against modal
# Enter in the fake-sandwich edge case.
_MODAL_FOOTER_SCAN_LINES = 5


def _prompt_head(prompt: str) -> str:
    """First N non-blank chars of the prompt — what should be visible
    in the input bar after a successful `send-keys -l`. Strip leading
    whitespace because the input bar prefix `❯ ` is already a separator."""
    stripped = prompt.lstrip()
    return stripped[:_PROMPT_HEAD_CHARS]


def _ws_collapse(s: str) -> str:
    """Collapse all whitespace runs (spaces, tabs, LF, CRLF, Unicode-ws)
    into a single space. Strips leading/trailing whitespace as a side
    effect of `str.split() + " ".join(...)`.

    CC's input bar transforms our payload in two ways we need to reverse
    before substring-matching against the original prompt:

      1. Prepends a 2-space visual indent (`_CC_CONTINUATION_INDENT`) to
         every continuation line — so `"a\\nb"` becomes `"a\\n  b"` in the
         bar.
      2. On word-wrap (long lines exceeding the input-bar frame width,
         ~66 chars inside the terminal rendering), CC replaces a space
         with a newline — so `"File: /path"` becomes `"File:\\n  /path"`
         when the path pushes past the frame edge.

    Collapsing both `_prompt_head(prompt)` and `_input_bar_content(pane)`
    into a canonical single-space form via this helper makes the
    substring match invariant to both transforms. The indent-strip in
    `_input_bar_content` is a secondary cleanup (benefits human-readable
    logs in `collect_diagnostic_signals` and any future consumer), but
    this function is the primary matching guarantee.
    """
    return " ".join(s.split())


def _strip_blank_tail(pane: str) -> str:
    """Normalize tmux capture-pane output for downstream last-N-line scans.

    Tmux nondeterministically leaves trailing blank rows (pane padding to
    the configured `-y` height — 50 in prod) or strips them, depending on
    whether the pane was dirtied by recent I/O. All gate scans downstream
    key on `splitlines()[-N:]` (5 lines for modal footer, 15 for input
    bar) — without this trim, N counts from the physical pane bottom
    rather than the last non-empty line, and a modal footer or idle bar
    silently slips out of the scan window on fresh / lightly-filled
    panes.

    Verified on live CC 2.1.118 repro: trust-modal footer at line 28 of
    a 33-line pane was invisible to the 5-line footer scan; same state
    after `send-keys` re-emitted a 20-line pane with the footer at line
    6 of 20 — detected fine. Same TUI state, different pane length,
    different verdict. Strip makes the verdict deterministic.
    """
    lines = pane.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _input_bar_content(pane: str) -> str | None:
    """Return the text currently inside the input bar, or None if no bar
    is rendered (modal state / broken layout / unknown).

    Note: the returned text is a **lossy approximation** of the payload
    the user pasted, not a faithful reconstruction. CC's visual
    decorations are stripped (leading `"  "` on continuation lines —
    see `_CC_CONTINUATION_INDENT`), which helps downstream substring
    matching but mutates any legitimate 2-space line leads the user
    may have typed. Downstream callers that need an exact payload
    echo should not use this function — use the Telegram-side stored
    prompt instead.

    Two gates must both pass:

    Gate A — modal-footer sniff. If the last lines of the pane contain
    `Esc to cancel|clear|exit|dismiss|close` or `Enter to confirm` with a
    capital E, that's a known modal footer → return None. Non-modals use
    lowercase `esc to interrupt` and are unaffected. This gate handles
    the case where a modal accidentally renders something that looks
    like an idle bar sandwich.

    Gate B — structural sandwich. The idle bar is
    `────── / ❯ <text> / ──────` with `─{10,}` separators on BOTH sides.
    Require both: a separator in the 3 lines ABOVE the `❯`, AND a
    separator somewhere BELOW it before the next `❯` or end-of-zone.
    Modals contain `❯` as menu cursors (e.g. `❯ 1. Yes` in Bash-perm)
    but lack the full sandwich — this gate rejects them.

    Why both gates: gate B rejects attacks where a user prompt head
    matches modal menu text (`Yes`, `No`, `trust`). Gate A is belt-and-
    suspenders: if CC ships a release where a modal body happens to
    bracket its menu with `─────` on both sides, gate B would pass but
    gate A still returns None.
    """
    if not pane:
        return None
    lines = _strip_blank_tail(pane).splitlines()[-_INPUT_BAR_SEARCH_LINES:]

    # Gate A — known modal footer in the LAST few lines only. Scanning
    # the full 15-line window would false-positive when conversation
    # history (visible in scrollback above the idle bar) happens to
    # quote "Esc to cancel" as part of documentation or code. Modals
    # always render their footer within ~3 lines of the pane bottom;
    # 5 is a safe margin.
    footer = "\n".join(lines[-_MODAL_FOOTER_SCAN_LINES:])
    if _MODAL_FOOTER_TOKEN_RE.search(footer):
        return None

    # Gate B — structural sandwich. Walk bottom-up, take the first `❯`,
    # require separators above AND below.
    for idx in range(len(lines) - 1, -1, -1):
        m = _INPUT_BAR_MARKER_RE.match(lines[idx])
        if m is None:
            continue
        has_frame_above = any(
            _BAR_SEPARATOR_RE.match(line) for line in lines[max(0, idx - 3) : idx]
        )
        # Scan all lines below until we either hit a separator (sandwich
        # valid) or another `❯` / end-of-zone (sandwich invalid). Open-
        # ended below-scan handles wrapped multi-line prompts which put
        # continuation rows between `❯` and the closing `─────`.
        has_frame_below = False
        for cont in lines[idx + 1 :]:
            if _BAR_SEPARATOR_RE.match(cont):
                has_frame_below = True
                break
            if _INPUT_BAR_MARKER_RE.match(cont):
                break
        if not (has_frame_above and has_frame_below):
            return None
        # Collect the bar's wrapped content: starts with this line's
        # remainder, plus any continuation until a `─────` terminator.
        # Strip CC's `_CC_CONTINUATION_INDENT` from each continuation so
        # the returned text is an approximation of the original payload
        # rather than its decorated input-bar rendering. The whitespace-
        # collapse normalization downstream in `prompt_visible_in_pane`
        # also neutralizes the indent, but stripping here keeps diagnostic
        # logs and any future consumer free of the visual artifact.
        parts = [m.group("rest")]
        for cont in lines[idx + 1 :]:
            if _BAR_SEPARATOR_RE.match(cont):
                break
            if _INPUT_BAR_MARKER_RE.match(cont):
                break
            if cont.startswith(_CC_CONTINUATION_INDENT):
                parts.append(cont[len(_CC_CONTINUATION_INDENT) :])
            else:
                parts.append(cont)
        return "\n".join(parts)
    return None


def _codex_input_bar_content(pane: str) -> str | None:
    """Return text inside Codex's visible `›` prompt line, if present."""
    if not pane:
        return None
    lines = _strip_blank_tail(pane).splitlines()[-_INPUT_BAR_SEARCH_LINES:]
    footer = "\n".join(lines[-_MODAL_FOOTER_SCAN_LINES:])
    if _MODAL_FOOTER_TOKEN_RE.search(footer):
        return None

    for idx in range(len(lines) - 1, -1, -1):
        m = _CODEX_INPUT_BAR_MARKER_RE.match(lines[idx])
        if m is None:
            continue
        parts = [m.group("rest")]
        for cont in lines[idx + 1 :]:
            if not cont.strip():
                break
            if _CODEX_INPUT_BAR_MARKER_RE.match(cont):
                break
            if _MODAL_FOOTER_TOKEN_RE.search(cont):
                return None
            if _CODEX_FOOTER_RE.search(cont):
                break
            if cont.startswith(_CC_CONTINUATION_INDENT):
                parts.append(cont[len(_CC_CONTINUATION_INDENT) :])
            else:
                parts.append(cont)
        return "\n".join(parts)
    return None


def _prompt_visible_in_bar(before_bar: str | None, after_bar: str | None, prompt: str) -> bool:
    head = _prompt_head(prompt)
    if not head or after_bar is None:
        return False

    head_norm = _ws_collapse(head)
    after_norm = _ws_collapse(after_bar)
    before_norm = _ws_collapse(before_bar) if before_bar else ""
    if head_norm and head_norm in after_norm:
        return not (before_norm and head_norm in before_norm)

    after_pastes = _PASTED_PLACEHOLDER_RE.findall(after_bar)
    if after_pastes:
        before_pastes = _PASTED_PLACEHOLDER_RE.findall(before_bar or "")
        if len(after_pastes) > len(before_pastes):
            return True

    first_line = _ws_collapse(head.split("\n", 1)[0])
    if len(first_line) >= _FIRST_LINE_MIN_LEN and first_line in after_norm:
        return not (before_norm and first_line in before_norm)

    return False


def is_modal_present(pane: str) -> bool:
    """True if the pane's footer carries a known modal dismiss token.

    Uses Gate A (capital-E footer-token scan) in isolation — unlike
    `_input_bar_content`, this does NOT require the full sandwich
    structure to conclude "no modal". The sandwich check (Gate B) is
    skipped on purpose here because it returns None on any transient
    render state (CC startup, pane refresh mid-tick, empty/broken
    capture), which would produce spurious idle-time alerts. Gate A
    is tight: real modals always advertise their dismiss keys with
    the capital-E forms (`Esc to cancel|clear|exit|dismiss|close`,
    `Enter to confirm`), while idle / thinking / compacting states
    use lowercase `esc to interrupt`.

    Used by the TmuxManager modal watchdog to surface modals that pop
    while CC is working autonomously (Bash permission, /usage, auto-
    compact confirmations) and no user message is in flight — the
    send_direct send-and-verify flow only catches those when a user
    tries to send, which may be hours later.
    """
    if not pane:
        return False
    lines = _strip_blank_tail(pane).splitlines()[-_MODAL_FOOTER_SCAN_LINES:]
    footer = "\n".join(lines)
    return bool(_MODAL_FOOTER_TOKEN_RE.search(footer))


def prompt_visible_in_pane(pane_before: str, pane_after: str, prompt: str) -> bool:
    """True if the prompt head is inside the input bar of `pane_after`
    and was not already inside the input bar of `pane_before`.

    Three delivery signals are accepted, tried in order:

      1. Normalized-head substring match (primary). Both `head` and
         `after_bar` are collapsed via `_ws_collapse` — all whitespace
         runs become single spaces, leading/trailing whitespace stripped.
         This neutralizes the two CC input-bar transforms that would
         otherwise break a raw substring check:
           - 2-space continuation indent (`_CC_CONTINUATION_INDENT`).
           - Word-wrap replacing spaces with newlines on long lines
             that overflow the visual frame width.
         Empirically validated (CC 2.1.119, 18 payloads) — this path
         catches the entire prod payload shape: text messages, photo/
         document batches, reply-context, forwards, mixed batches.

      2. `[Pasted text #N]` placeholder. CC collapses bracketed-paste
         payloads above ~1500 chars into a literal placeholder — the
         head never renders as text, but the placeholder itself is a
         reliable signal that bytes reached the input buffer. Guarded
         by a count check (`after > before`) so a stale placeholder from
         a prior round-trip doesn't replay.

      3. First-line-only substring fallback. If the normalized full head
         somehow didn't match (unknown future CC transformation), the
         first line of the head alone is a resilient signal — CC never
         indents the first input-bar line, never word-wraps the very
         beginning of a freshly pasted payload. Guarded by
         `_FIRST_LINE_MIN_LEN` to prevent ultra-short first lines
         (`"да"`, `"ok"`) from colliding with unrelated modal body text.

    The guards close three separate false-positive classes:
      (a) Modal overlay — `after` pane has no input bar
          (`_input_bar_content` returns None via Gate A or Gate B).
          Even if modal body contains substrings matching the prompt
          head, we never report delivery. Security-critical — this is
          what protects Enter from confirming a dialog item.
      (b) Lingering scrollback — prompt was already in bar from a prior
          send; `before_bar` match subtracts the lingering signal.
      (c) Stale `[Pasted text #N]` — same placeholder persists across
          the send; count-delta check neutralizes it.
    """
    after_bar = _input_bar_content(pane_after)
    before_bar = _input_bar_content(pane_before)
    return _prompt_visible_in_bar(before_bar, after_bar, prompt)


def codex_prompt_visible_in_pane(pane_before: str, pane_after: str, prompt: str) -> bool:
    """True if a Codex `›` prompt line newly contains the prompt payload."""
    after_bar = _codex_input_bar_content(pane_after)
    before_bar = _codex_input_bar_content(pane_before)
    return _prompt_visible_in_bar(before_bar, after_bar, prompt)


def codex_input_bar_content(pane: str) -> str | None:
    """Return the current Codex input-bar text, if one is visible."""
    return _codex_input_bar_content(pane)


def claude_input_bar_content(pane: str) -> str | None:
    """Return the current Claude input-bar text, if one is visible.

    Public alias for `_input_bar_content` — kept symmetric with
    `codex_input_bar_content` so callers in `tmux_manager.py` can
    pick the parser by provider without reaching into private names.
    """
    return _input_bar_content(pane)


def collect_diagnostic_signals(
    pane_before: str,
    pane_after: str,
    prompt: str,
    *,
    elapsed_ms: int | None = None,
    pane_width: int | None = None,
) -> ModalDiagSignals:
    """Structured signals for diagnosing `BLOCKED reason=modal` false
    positives. Called from `tmux_manager.send_direct` when the verify
    step fails.

    Each field isolates one candidate cause so log triage can tell them
    apart without attaching the full pane. Keep values JSON-safe so the
    dict can be passed straight to `logger.debug(..., extra={...})`.

    - `pane_equal` / `len_pane_after` — surfaces wedged tmux (capture
      returned `""`) and "nothing rendered between baseline and verify".
    - `elapsed_ms` — distinguishes "settle was too short for a long
      wrapped prompt" from "real modal".
    - `pane_width` — decorated `─═─═` separators on narrow terminals
      defeat Gate B's `─{10,}` regex; pane width in the log lets the
      operator see when that's the cause.
    - `has_input_bar_before/after` — whether `_input_bar_content`
      returns anything. `False` on both with a non-empty pane usually
      means Gate B rejected a transient render.
    - `gate_a_hit` — whether the capital-E footer regex matches. On a
      real modal this is True; a False value on a BLOCKED path points
      at Gate B as the rejecter.
    - `head_in_after_bar` — final delivery check. False on BLOCKED is
      expected; True with `gate_a_hit=False` would be a detector bug.
    """
    head = _prompt_head(prompt)
    before_bar = _input_bar_content(pane_before)
    after_bar = _input_bar_content(pane_after)

    # Gate A slice here must agree with `is_modal_present` on the same
    # pane — otherwise triage logs report gate_a_hit=False while the
    # detector reports True on a padded pane. Both trim identically.
    footer_lines = _strip_blank_tail(pane_after).splitlines()[-_MODAL_FOOTER_SCAN_LINES:]
    footer = "\n".join(footer_lines)

    # `head_in_after_bar` mirrors the runtime verdict's primary-path
    # decision: normalized substring match plus scrollback subtraction.
    # Without the subtraction, a pane carrying the head in both `before`
    # and `after` (a lingering send from a prior round-trip) reports
    # `head_in_after_bar=True` while `prompt_visible_in_pane` returns
    # False — exactly the contradiction the diag field exists to avoid.
    # Both values use the same `_ws_collapse` form so triage logs stay
    # consistent across CC's input-bar transformations (2-space indent,
    # word-wrap space→newline).
    #
    # Caveat: the placeholder-delivery path (`[Pasted text #N]` on
    # ≥1500-char payloads) has verdict=True but `head_in_after_bar=False`
    # by design — the head is genuinely absent from the bar. This is
    # documented in `prompt_visible_in_pane`'s docstring and locked down
    # by `test_head_in_after_bar_false_on_placeholder_delivery`.
    head_norm = _ws_collapse(head)
    after_norm = _ws_collapse(after_bar) if after_bar is not None else ""
    before_norm = _ws_collapse(before_bar) if before_bar is not None else ""
    head_in_after = bool(head_norm) and head_norm in after_norm
    # Scrollback subtraction: if the same head was already present in
    # `before_bar`, the "after" match is a carry-over, not a fresh
    # delivery — matches the `return not (before_norm and head_norm in
    # before_norm)` branch in `prompt_visible_in_pane`.
    if head_in_after and before_norm and head_norm in before_norm:
        head_in_after = False

    return ModalDiagSignals(
        len_pane_before=len(pane_before),
        len_pane_after=len(pane_after),
        pane_equal=pane_before == pane_after,
        elapsed_ms=elapsed_ms,
        pane_width=pane_width,
        has_input_bar_before=before_bar is not None,
        has_input_bar_after=after_bar is not None,
        gate_a_hit=bool(_MODAL_FOOTER_TOKEN_RE.search(footer)),
        prompt_head_len=len(head),
        head_in_after_bar=head_in_after,
    )


async def capture_pane(session_name: str) -> str:
    """Read the current pane. Returns empty string on any subprocess
    failure (including a timeout) — the caller must interpret empty as
    "unknown state" and err on the side of NOT sending Enter.

    Timeout bound exists because a wedged tmux server can make
    capture-pane block indefinitely, which would hang every send_direct
    behind the per-channel lock. Five seconds is far above the healthy
    latency (single-digit ms) and still keeps the request responsive.
    """
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["tmux", "capture-pane", "-t", session_name, "-p"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_CAPTURE_TIMEOUT_SEC,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout or ""
