from pathlib import Path

from telegram_bot.core.tui.paths import transcript_path

SID = "ccabbeb3-dc4b-41a2-945c-96ef6da91345"


def _touch(home: Path, slug: str) -> Path:
    path = home / ".claude" / "projects" / slug / f"{SID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{}\n")
    return path


def test_derived_from_cwd(tmp_path: Path) -> None:
    expected = _touch(tmp_path, "-proj")
    assert transcript_path("/proj", SID, home=tmp_path) == expected


def test_follows_session_moved_to_worktree(tmp_path: Path) -> None:
    """After EnterWorktree the transcript lives under the worktree slug."""
    moved = _touch(tmp_path, "-proj--claude-worktrees-task")
    assert transcript_path("/proj", SID, home=tmp_path) == moved


def test_missing_everywhere_returns_derived(tmp_path: Path) -> None:
    expected = tmp_path / ".claude" / "projects" / "-proj" / f"{SID}.jsonl"
    assert transcript_path("/proj", SID, home=tmp_path) == expected
