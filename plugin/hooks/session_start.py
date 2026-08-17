"""SessionStart hook: inject the best-guess brief so an agent starts briefed rather than cold.

This is the ONLY place inference is permitted. Reading the wrong brief is cheap — the agent notices
immediately because the content obviously does not match. Writing the wrong brief is corrupting, so
the write path (writer.py, mint.py) demands an explicit stream and this module never writes anything.

Injected content is labeled with how it was reached. An agent that cannot tell a declaration from a
guess will treat a guess as authority, which is exactly how the wrong stream's decisions leak into
unrelated work.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from plugin.lib import brief_read, claims, vault  # noqa: E402

# Hints an operator or dispatcher can set explicitly. An explicit declaration outranks every
# inferred signal, and is the only rung that is not a guess.
STREAM_ENV = "MESSAGE_BOARD_STREAM"

UNKNOWN_SESSION = "unknown"

# How long to wait for hook input before concluding there is none. Claude Code writes its JSON and
# closes immediately, so this is never reached in practice — it exists so that an inherited, idle,
# never-closed stdin costs a moment instead of hanging the session.
STDIN_TIMEOUT = 2.0


def _has_input(stream, timeout: float) -> bool:
    """Whether reading `stream` will return rather than block.

    An `isatty()` check is not enough. Any non-tty stdin that stays open with nothing on it — a
    pipe from a parent process, a CI runner, a test harness — makes a bare `read()` block forever,
    which is how a SessionEnd hook hangs a shutdown. `select` reports ready on data *or* EOF, so
    the only case that waits is the one that would otherwise wait indefinitely.

    In-memory streams have no descriptor and nothing to wait on, so they are always readable.
    """
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return True
    try:
        return bool(select.select([fd], [], [], timeout)[0])
    except (OSError, ValueError):
        return True


def hook_payload(stream=None, timeout: float = STDIN_TIMEOUT) -> dict:
    """The hook's JSON input, or `{}` when there is none.

    Claude Code passes `session_id`, `cwd`, and friends as JSON on **stdin** — not as environment
    variables. Reading `CLAUDE_SESSION_ID` instead is silent rather than wrong-looking: every trace
    is simply stamped `unknown`, which reads as a missing session rather than a hook that never
    looked. Since the session reference is what makes a trace promotable, that is a real loss.

    Never blocks, and never fails the session on malformed input — a hook that hangs or raises is
    worse than one that records less than it could.
    """
    stream = sys.stdin if stream is None else stream
    try:
        if stream is None or stream.isatty():
            return {}
    except (AttributeError, ValueError):
        return {}
    if not _has_input(stream, timeout):
        return {}
    try:
        payload = json.loads(stream.read() or "{}")
    except (OSError, ValueError, AttributeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def payload_session(payload: dict) -> str:
    value = str(payload.get("session_id") or "").strip()
    return value or UNKNOWN_SESSION


def _git(args: list[str], cwd: str) -> str | None:
    """Run a read-only git command, returning None when this is not a repo.

    Two of the three target workflows (design review, code review) legitimately run outside a git
    checkout, so absence of git is an ordinary case here, not an error.
    """
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value if out.returncode == 0 and value else None


def gather_hints(cwd: str, env: dict[str, str] | None = None) -> claims.Hints:
    env = os.environ if env is None else env
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    # `main` is never a valid claim: it would resolve every stream in the repo and therefore none.
    if branch == "main":
        branch = None

    repo = None
    remote = _git(["remote", "get-url", "origin"], cwd)
    if remote:
        trimmed = remote.removesuffix(".git").rstrip("/")
        parts = [p for p in trimmed.replace(":", "/").split("/") if p]
        if len(parts) >= 2:
            repo = "/".join(parts[-2:])

    return claims.Hints(
        stream=env.get(STREAM_ENV) or None,
        worktree=cwd,
        branch=branch,
        repo=repo,
    )


def build_injection(root: Path, hints: claims.Hints) -> str:
    """Return the text to inject, or "" when nothing should be injected.

    Silence is a real outcome, not a failure. Injecting an ambiguous match would hand the agent a
    plausible-looking brief for work it is not doing.
    """
    streams = [b for b in vault.list_streams(root) if b.resolvable]
    resolution = claims.resolve_for_read(streams, hints)

    if resolution.stream is None:
        if resolution.candidates:
            names = ", ".join(b.slug for b in resolution.candidates)
            return (
                f"[message-board] Several streams match this checkout: {names}\n"
                f"No brief injected — {resolution.reason}. "
                f"Name one explicitly ({STREAM_ENV}=<slug>) or read it with "
                f"`python3 -m plugin.lib.brief_read <slug>`."
            )
        return ""

    explicit = resolution.via == "explicit"
    header = (
        f"[message-board] Brief for stream '{resolution.stream.slug}' (declared explicitly)."
        if explicit
        else (
            f"[message-board] GUESS: this session looks like stream "
            f"'{resolution.stream.slug}', matched via {resolution.via}. "
            f"Verify before relying on it; if it is wrong, say so rather than working around it."
        )
    )
    return f"{header}\n\n{brief_read.render(resolution.stream)}"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    payload = hook_payload()
    cwd = argv[0] if argv else (str(payload.get("cwd") or "") or os.getcwd())
    try:
        root = vault.resolve_vault()
    except vault.VaultError:
        # An unconfigured vault must not break the session. The plugin is additive; a session that
        # never heard of the board still has to start.
        return 0
    try:
        text = build_injection(root, gather_hints(cwd))
    except vault.VaultError as err:
        print(f"[message-board] {err}", file=sys.stderr)
        return 0
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
