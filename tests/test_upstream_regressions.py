from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_bot.core.handlers import commands
from telegram_bot.core.messages import t
from telegram_bot.core.services import tmux_manager as tmux_manager_module
from telegram_bot.core.services.providers import (
    CODEX_ADAPTER,
    CodexTranscriptSnapshot,
    TuiSessionInfo,
)
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.tmux_state import TmuxSessionState


@pytest.mark.asyncio
async def test_fresh_codex_late_acceptance_continues_from_discovered_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first prompt accepted after the pane retry window must still be tailed."""
    manager = TmuxManager(tmp_path / "sessions")
    channel_key = (100, 200)
    state = TmuxSessionState(
        session_name="cc-100-200",
        session_dir=str(tmp_path / "sessions" / "cc-100-200"),
        session_id=None,
        mode="acceptEdits",
        cwd=str(tmp_path),
        mcp_config="",
        chat_id=100,
        offset=0,
        runner_version="codex-tui-v1",
        provider="codex",
        model=None,
        transcript_path=None,
        base_mcp_config=None,
    )
    manager._sessions[channel_key] = state

    snapshot = CodexTranscriptSnapshot(root=tmp_path / "codex-sessions", files={})
    monkeypatch.setattr(CODEX_ADAPTER, "capture_tui_transcript_snapshot", lambda: snapshot)
    monkeypatch.setattr(CODEX_ADAPTER, "is_modal_present", lambda _pane: False)
    monkeypatch.setattr(manager, "_codex_delivery_visible", lambda *_args: True)
    monkeypatch.setattr(manager, "_codex_prompt_still_in_input_bar", lambda *_args: True)
    monkeypatch.setattr(tmux_manager_module, "_CODEX_PASTE_POLL_STEP_SEC", 0.0)
    monkeypatch.setattr(tmux_manager_module, "_ENTER_RETRY_SETTLE_SEC", 0.0)

    capture_pane = AsyncMock(return_value="pane with prompt still visible")
    send_text = AsyncMock()
    send_enter = AsyncMock()
    alert = AsyncMock()
    monkeypatch.setattr(tmux_manager_module, "capture_pane", capture_pane)
    monkeypatch.setattr(tmux_manager_module, "send_text_to_tmux", send_text)
    monkeypatch.setattr(tmux_manager_module, "send_enter", send_enter)
    monkeypatch.setattr(manager, "_send_modal_alert", alert)

    async def discover_transcript(
        discovered_state: TmuxSessionState,
        *,
        snapshot: CodexTranscriptSnapshot,
        prompt: str,
    ) -> None:
        assert snapshot.root == tmp_path / "codex-sessions"
        assert prompt == "first prompt"
        discovered_state.session_id = "00000000-0000-4000-8000-000000000001"
        discovered_state.transcript_path = str(tmp_path / "turn.jsonl")

    locate = AsyncMock(side_effect=discover_transcript)
    tail = AsyncMock(return_value=("", None))
    monkeypatch.setattr(manager, "_locate_codex_transcript_after_send", locate)
    monkeypatch.setattr(manager, "_tail_until_done", tail)

    await manager.send_stream(channel_key, "first prompt", AsyncMock())

    assert send_enter.await_count == tmux_manager_module._ENTER_RETRY_LIMIT
    locate.assert_awaited_once()
    tail.assert_awaited_once()
    alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_codex_discovery_allows_slow_cold_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh-session confirmation must outlive the former 30-second boundary."""
    manager = TmuxManager(tmp_path / "sessions")
    state = TmuxSessionState(
        session_name="cc-100-202",
        session_dir=str(tmp_path / "sessions" / "cc-100-202"),
        session_id=None,
        mode="acceptEdits",
        cwd=str(tmp_path),
        mcp_config="",
        chat_id=100,
        offset=0,
        runner_version="codex-tui-v1",
        provider="codex",
        model=None,
        transcript_path=None,
        base_mcp_config=None,
    )
    snapshot = CodexTranscriptSnapshot(root=tmp_path / "codex-sessions", files={})
    transcript = tmp_path / "turn.jsonl"
    locate = AsyncMock(
        return_value=TuiSessionInfo(
            session_id="00000000-0000-4000-8000-000000000003",
            transcript_path=transcript,
            tail_start_offset=17,
        )
    )
    monkeypatch.setattr(CODEX_ADAPTER, "locate_tui_transcript", locate)
    save_state = MagicMock()
    monkeypatch.setattr(manager, "_save_state", save_state)

    await manager._locate_codex_transcript_after_send(
        state,
        snapshot=snapshot,
        prompt="slow first prompt",
    )

    locate.assert_awaited_once_with(
        cwd=str(tmp_path),
        snapshot=snapshot,
        prompt="slow first prompt",
        timeout_sec=60.0,
    )
    assert state.session_id == "00000000-0000-4000-8000-000000000003"
    assert state.transcript_path == str(transcript)
    assert state.offset == 17
    save_state.assert_called_once_with()


@pytest.mark.asyncio
async def test_existing_codex_late_acceptance_continues_without_resend_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lagging input bar on an existing transcript must not invite a duplicate."""
    manager = TmuxManager(tmp_path / "sessions")
    channel_key = (100, 201)
    state = TmuxSessionState(
        session_name="cc-100-201",
        session_dir=str(tmp_path / "sessions" / "cc-100-201"),
        session_id="00000000-0000-4000-8000-000000000002",
        mode="acceptEdits",
        cwd=str(tmp_path),
        mcp_config="",
        chat_id=100,
        offset=0,
        runner_version="codex-tui-v1",
        provider="codex",
        model=None,
        transcript_path=str(tmp_path / "turn.jsonl"),
        base_mcp_config=None,
    )
    manager._sessions[channel_key] = state

    monkeypatch.setattr(CODEX_ADAPTER, "is_modal_present", lambda _pane: False)
    monkeypatch.setattr(manager, "_codex_delivery_visible", lambda *_args: True)
    monkeypatch.setattr(manager, "_codex_prompt_still_in_input_bar", lambda *_args: True)
    monkeypatch.setattr(tmux_manager_module, "_CODEX_PASTE_POLL_STEP_SEC", 0.0)
    monkeypatch.setattr(tmux_manager_module, "_ENTER_RETRY_SETTLE_SEC", 0.0)

    monkeypatch.setattr(
        tmux_manager_module,
        "capture_pane",
        AsyncMock(return_value="pane with prompt still visible"),
    )
    monkeypatch.setattr(tmux_manager_module, "send_text_to_tmux", AsyncMock())
    send_enter = AsyncMock()
    monkeypatch.setattr(tmux_manager_module, "send_enter", send_enter)
    alert = AsyncMock()
    monkeypatch.setattr(manager, "_send_modal_alert", alert)
    locate = AsyncMock()
    monkeypatch.setattr(manager, "_locate_codex_transcript_after_send", locate)
    tail = AsyncMock(return_value=("", None))
    monkeypatch.setattr(manager, "_tail_until_done", tail)

    delivered = await manager.send_direct(channel_key, "existing prompt")

    assert delivered is True
    assert send_enter.await_count == tmux_manager_module._ENTER_RETRY_LIMIT
    locate.assert_not_awaited()
    tail.assert_not_awaited()
    alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_recycle_ignores_idle_queue_lock_held_by_tmux_tail() -> None:
    """A completed provider turn can be recycled while its tail callback remains alive."""
    message = MagicMock()
    message.chat.id = 100
    message.message_thread_id = 200
    message.answer = AsyncMock()

    tmux_manager = MagicMock()
    tmux_manager.is_active.return_value = True
    tmux_manager.is_processing.return_value = False
    tmux_manager.recycle = AsyncMock(return_value=True)

    message_queue = MagicMock()
    message_queue.is_busy.return_value = True
    message_queue.has_pending.return_value = False
    session_manager = MagicMock()

    await commands.handle_recycle(
        message,
        tmux_manager,
        session_manager,
        message_queue,
    )

    tmux_manager.recycle.assert_awaited_once_with((100, 200), session_manager)
    message.answer.assert_awaited_once_with(t("ui.recycle_done"))
    message_queue.is_busy.assert_not_called()


@pytest.mark.asyncio
async def test_recycle_still_blocks_messages_waiting_in_queue() -> None:
    message = MagicMock()
    message.chat.id = 100
    message.message_thread_id = 200
    message.answer = AsyncMock()

    tmux_manager = MagicMock()
    tmux_manager.is_active.return_value = True
    tmux_manager.is_processing.return_value = False
    tmux_manager.recycle = AsyncMock(return_value=True)

    message_queue = MagicMock()
    message_queue.has_pending.return_value = True

    await commands.handle_recycle(
        message,
        tmux_manager,
        MagicMock(),
        message_queue,
    )

    tmux_manager.recycle.assert_not_awaited()
    message.answer.assert_awaited_once_with(t("ui.exec_mode_busy"))
