"""One line, always on screen: what the board is waiting on the operator for.

The dashboard answers "what is going on across every stream" and costs a deliberate act to read.
This answers the smaller question the operator would otherwise have to remember to ask — *is
anything waiting on me?* — and costs nothing, because Claude Code already draws a status line.

The pane count is the whole argument. A background TUI shows more, but only while the operator is
looking at it, and an escalation nobody looks at is an escalation that did not happen. This surface
is glanced at continuously and points at `python3 -m plugin.dashboard` for the detail it cannot fit.

Three properties are load-bearing:

1. **The needs-you segment always renders, including when it is zero.** Every other segment is
   dropped when empty, so its absence means "nothing there". If the needs-you segment could also
   vanish, a silent status line would be ambiguous between "clear" and "not running".
2. **Ordering matches the dashboard.** `top:` names the stream the dashboard would show first, via
   the same `sort_key`. Two surfaces disagreeing about what is most urgent teaches the operator to
   trust neither.
3. **Adjudications are counted, never coloured.** The word is "to adjudicate", not "failing" — the
   status line is the surface most likely to train a reflex, and the reflex must not be to dismiss.

Read-only, stdlib only, no ANSI.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

# Bootstrapped like the hooks, and for the same reason: Claude Code invokes a status line by
# absolute script path from whatever directory the session is in, so this file has to work as
# `python3 .../plugin/statusline.py` and not only as `python3 -m plugin.statusline`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugin import dashboard  # noqa: E402
from plugin.hooks import session_end, session_start  # noqa: E402
from plugin.hooks.session_start import hook_payload  # noqa: E402
from plugin.lib import claims, vault  # noqa: E402

_MIN_WIDTH, _MAX_WIDTH = 40, 200

# Shown in place of the summary when the vault cannot be resolved. Names the diagnostic rather than
# the fault: the status line is redrawn constantly, so a full explanation here is a nag, and silence
# would be indistinguishable from a clear board.
UNAVAILABLE = "board unavailable · python3 -m plugin.lib.doctor"


def _terminal_width() -> int:
    return max(_MIN_WIDTH, min(_MAX_WIDTH, shutil.get_terminal_size(fallback=(96, 24)).columns))


@dataclass(frozen=True)
class Summary:
    """The counts behind the line, separated from their formatting so both can be tested."""

    streams: int
    asks: list[tuple[str, str]]
    top: str | None
    inbox: int
    signals: int
    untriaged: int


def summarize(vault_root: Path) -> Summary:
    views = [dashboard.build_view(brief) for brief in vault.list_streams(vault_root)]
    # Counted across every stream, archived included, so this number matches the dashboard header.
    asks = [(v.brief.slug, ask) for v in views for ask in v.asks]
    # Pointed at an active stream only: an archived stream is readable, never resolved into, so
    # sending the operator there as "the next thing" would be a dead end.
    active = sorted((v for v in views if not v.brief.archived), key=dashboard.sort_key)
    top = next((v.brief.slug for v in active if v.asks), None)
    return Summary(
        streams=len(views),
        asks=asks,
        top=top,
        inbox=sum(len(v.inbox) for v in views),
        signals=sum(len(v.signals) for v in views),
        untriaged=len(dashboard.unassigned_traces(vault_root)),
    )


def session_segment(vault_root: Path, cwd: str, env: dict[str, str] | None = None) -> str:
    """Where *this* session stands with the board, stated so nothing is left to guess.

    The board-wide counts answer "is anything waiting on me?"; this answers the question the
    operator was otherwise reduced to guessing at — *is this session being tracked, and will
    anything be written when it ends?* Every state names its exit behavior explicitly, because a
    session whose record silently fails to appear is indistinguishable from one that was never
    tracked, and that ambiguity is what erodes trust in the whole surface.

    The states mirror `session_end.run` exactly — this segment is a promise about what that hook
    will do, so the two must share one resolution ladder or the promise is a lie. That is why the
    disable check comes first, before resolution: the hook bails on the flag before resolving, so a
    disabled session that *would* resolve must still say "disabled", not name a stream it will
    never write to.
    """
    if session_end.is_disabled(env):
        return "this: disabled (nothing written on exit)"
    hints = session_start.gather_hints(cwd)
    streams = [b for b in vault.list_streams(vault_root) if b.resolvable]
    resolution = claims.resolve_for_read(streams, hints)
    if resolution.stream is not None:
        return f"this: {resolution.stream.slug}"
    if resolution.ambiguous:
        return f"this: ambiguous ({len(resolution.candidates)} streams match)"
    in_repo = session_start._git(["rev-parse", "--is-inside-work-tree"], cwd) == "true"
    if not in_repo and session_end.is_ignored(cwd, env):
        return "this: ignored (nothing written on exit)"
    return "this: untracked (trace on exit)"


def payload_cwd(payload: dict) -> str:
    """The session's directory from the status-line JSON, with the workspace form as fallback."""
    direct = str(payload.get("cwd") or "").strip()
    if direct:
        return direct
    workspace = payload.get("workspace")
    nested = str((workspace or {}).get("current_dir") or "").strip() if isinstance(workspace, dict) else ""
    return nested or os.getcwd()


def segments(summary: Summary) -> list[str]:
    """The line's parts, most urgent first. Only the first is unconditional."""
    parts = [f"{len(summary.asks)} need you" if summary.asks else "nothing needs you"]
    if summary.top:
        parts.append(f"top: {summary.top}")
    if summary.inbox:
        parts.append(f"{summary.inbox} inbox")
    if summary.signals:
        parts.append(f"{summary.signals} to adjudicate")
    if summary.untriaged:
        parts.append(f"{summary.untriaged} untriaged")
    return parts


def format_line(summary: Summary, width: int | None = None, this: str | None = None) -> str:
    """Fit the segments into `width` by dropping from the least urgent end.

    Dropped rather than truncated: a half-written slug is worse than an absent one, because the
    operator cannot tell a shortened name from a real one.
    """
    width = width or _terminal_width()
    parts = segments(summary)
    if this:
        # Unconditional alongside needs-you, for the same reason: "am I tracked?" answered with
        # silence is exactly the guessing game this segment exists to end.
        parts.insert(0, this)
    # The first segment is unconditional. A width too narrow to hold it is a terminal problem; a
    # status line that answers "is anything waiting on me?" with silence is a correctness problem.
    line = f"board  {parts[0]}"
    start = 1
    if this:
        line = f"{line} · {parts[1]}"
        start = 2
    for part in parts[start:]:
        candidate = f"{line} · {part}"
        if len(candidate) > width:
            break
        line = candidate
    return line


def render(vault_root: Path, width: int | None = None, this: str | None = None) -> str:
    return format_line(summarize(vault_root), width, this=this)


def main(argv: list[str]) -> int:
    if argv:
        print("usage: python3 -m plugin.statusline", file=sys.stderr)
        return 2
    # Claude Code writes its session JSON to stdin; `hook_payload` is the reader that cannot hang
    # on an inherited idle pipe. The payload's cwd is what lets the line speak about *this* session
    # rather than only the board.
    payload = hook_payload()
    try:
        root = vault.resolve_vault()
    except vault.VaultError:
        print(UNAVAILABLE)
        return 0
    try:
        this = session_segment(root, payload_cwd(payload))
    except (OSError, vault.VaultError):
        # The board-wide line still renders; a broken session probe must not blank the surface.
        this = None
    print(render(root, this=this))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
