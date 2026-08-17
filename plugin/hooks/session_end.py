"""SessionEnd hook: record mechanical ground truth.

Hooks are the SOLE writer of `ground-truth.md`. That separation is the whole point: agent judgment
lands in BRIEF.md, machine-observed reality lands here, and the gap between them is a first-class
signal rather than an inconsistency to reconcile. A hook that wrote into BRIEF.md — even its
frontmatter — would destroy the independence that makes the comparison worth anything.

Everything here is observed, never inferred. When a fact cannot be observed the absence is recorded
explicitly ("not a git repository") rather than omitted, because a missing line and a negative
finding are different claims and the dashboard has to tell them apart.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from plugin.hooks import session_start  # noqa: E402
from plugin.lib import claims, vault, writer  # noqa: E402

GROUND_TRUTH_FILE = "ground-truth.md"
NOT_A_REPO = "not a git repository"

# A trace is supposed to be "one line saying what happened, plus enough context to promote it".
# Only the context half was ever written, which is why a queue entry could report five negatives and
# a directory path and leave the operator with no way to judge it. Claude Code already writes a
# session title into the transcript; reading it is observation, not inference, and it is the single
# most useful thing a trace can carry.
NO_SUMMARY = writer.NO_SUMMARY

# Guardrail for a pathological transcript. A 6 MB file scans in ~8 ms with the substring pre-filter
# below, so this is far above anything real — it exists so a hook can never hang a session shutdown.
TRANSCRIPT_BYTE_LIMIT = 64 * 1024 * 1024

SUMMARY_CHARS = 140

# User records that are not the operator talking. Slash-command envelopes, hook injections, and
# local command output all arrive as `user` messages, and taking the first one blindly makes every
# trace of a session that opened with a slash command read as `/init`.
_NOT_A_PROMPT = (
    "<command-message>",
    "<command-name>",
    "<local-command-",
    "<system-reminder>",
    "<bash-input>",
    "<bash-stdout>",
    "<user-prompt-submit-hook>",
)


def _git(args: list[str], cwd: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value if out.returncode == 0 else (value or None)


def observe(cwd: str) -> dict[str, str]:
    """Collect mechanical facts. Every key is always present; absence is a value, not a gap."""
    in_repo = _git(["rev-parse", "--is-inside-work-tree"], cwd) == "true"
    if not in_repo:
        return {
            "repo": NOT_A_REPO,
            "branch": "n/a",
            "commit": "n/a",
            "dirty": "n/a",
            "cwd": cwd,
        }

    remote = _git(["remote", "get-url", "origin"], cwd)
    repo = "unknown"
    if remote:
        trimmed = remote.removesuffix(".git").rstrip("/")
        parts = [p for p in trimmed.replace(":", "/").split("/") if p]
        if len(parts) >= 2:
            repo = "/".join(parts[-2:])

    status = _git(["status", "--porcelain"], cwd)
    return {
        "repo": repo,
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd) or "unknown",
        "commit": _git(["log", "-1", "--format=%h %s"], cwd) or "none",
        "dirty": "yes" if status else "no",
        "cwd": cwd,
    }


def _message_text(record: dict) -> str:
    content = record.get("message", {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _one_line(text: str, limit: int = SUMMARY_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def observe_session(transcript: str | None) -> dict[str, str]:
    """What this session was about, read off its own transcript.

    Two independent signals, because they fail in different ways: Claude Code's generated title is
    the best one-line answer but is absent on short sessions, and the operator's opening prompt is
    always there but can be a bare slash command. Every key is returned even when empty — a trace
    that silently omits its summary is exactly the entry nobody can triage.
    """
    found = {"title": "", "asked": "", "turns": "0"}
    if not transcript:
        return found
    path = Path(transcript)
    try:
        if not path.is_file() or path.stat().st_size > TRANSCRIPT_BYTE_LIMIT:
            return found
    except OSError:
        return found

    turns = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                # Pre-filtered before parsing: most lines are assistant turns and tool results, and
                # json.loads on all of them is the difference between 8 ms and several seconds.
                is_title = '"ai-title"' in line
                if not is_title and '"type":"user"' not in line.replace(" ", ""):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if is_title or record.get("type") == "ai-title":
                    # Last one wins: the title is refined as the session goes on.
                    found["title"] = _one_line(record.get("aiTitle") or found["title"])
                    continue
                if record.get("type") != "user" or record.get("isSidechain"):
                    continue
                text = _message_text(record).strip()
                if not text or text.startswith(_NOT_A_PROMPT):
                    continue
                turns += 1
                if not found["asked"]:
                    found["asked"] = _one_line(text)
    except OSError:
        return found

    found["turns"] = str(turns)
    return found


def summarize(session: dict[str, str]) -> str:
    """The one line a triage listing shows. Title first, the opening ask as the fallback."""
    return session.get("title") or session.get("asked") or NO_SUMMARY


def render_ground_truth(facts: dict[str, str], now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "---",
        "source: hook",
        f"updated: {stamp}",
        "---",
        "",
    ]
    lines += [f"- **{key.capitalize()}:** {value}" for key, value in facts.items()]
    return "\n".join(lines) + "\n"


def write_ground_truth(root: Path, slug: str, facts: dict[str, str], now: datetime | None = None) -> Path:
    """Overwrite ground-truth.md for one stream.

    Overwrite rather than append: ground truth is a snapshot of current reality, and a growing log of
    stale snapshots would push the brief past the budget that keeps cold start cheap. History lives
    in the vault's git repo.
    """
    target = vault.streams_dir(root) / slug / GROUND_TRUTH_FILE
    if not target.parent.is_dir():
        raise vault.VaultError(f"No stream '{slug}' in {root}.")
    tmp = target.with_suffix(".tmp")
    tmp.write_text(render_ground_truth(facts, now), encoding="utf-8")
    os.replace(tmp, target)  # atomic; a torn ground-truth file would read as a false negative
    return target


def run(
    root: Path,
    cwd: str,
    session: str = "unknown",
    env: dict[str, str] | None = None,
    transcript: str | None = None,
) -> str:
    """Write ground truth for the resolved stream, or a trace when none resolves.

    Resolution here reuses the read-path ladder deliberately. These are observations, not judgments:
    recording them against a wrong stream is recoverable and visible, whereas refusing to record
    anything loses the only evidence that is independent of agent self-report.
    """
    facts = observe(cwd)
    hints = session_start.gather_hints(cwd, env=env)
    streams = [b for b in vault.list_streams(root) if b.resolvable]
    resolution = claims.resolve_for_read(streams, hints)

    if resolution.stream is None:
        session_facts = observe_session(transcript)
        body = [f"- **{k.capitalize()}:** {v}" for k, v in facts.items()]
        # Recorded even when empty. A trace that shows only what it *could* observe reads as though
        # the session had no subject, rather than as a transcript this hook could not see.
        body += [
            f"- **Asked:** {session_facts['asked'] or 'not captured'}",
            f"- **Turns:** {session_facts['turns']}",
        ]
        writer.write_unassigned(
            root,
            "\n".join(body),
            reason=resolution.reason or "no stream resolved for this session",
            session=session,
            cwd=cwd,
            branch=None if facts["branch"] in ("n/a", "unknown") else facts["branch"],
            # Carried in the header, not only the body, because triage groups the queue by work item
            # and (repo, branch) is what a work item is. Parsing it back out of rendered prose would
            # make the grouping depend on the body's layout. Same for the summary: it is the first
            # thing a listing shows, so it cannot depend on how the body happens to be laid out.
            repo=None if facts["repo"] in (NOT_A_REPO, "unknown") else facts["repo"],
            summary=summarize(session_facts),
        )
        return "unassigned"

    write_ground_truth(root, resolution.stream.slug, facts)
    return resolution.stream.slug


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    payload = session_start.hook_payload()
    cwd = argv[0] if argv else (str(payload.get("cwd") or "") or os.getcwd())
    try:
        root = vault.resolve_vault()
    except vault.VaultError:
        return 0  # additive plugin: an unconfigured vault must never fail a session
    try:
        run(
            root,
            cwd,
            session=session_start.payload_session(payload),
            transcript=str(payload.get("transcript_path") or "") or None,
        )
    except (vault.VaultError, writer.WriteError) as err:
        print(f"[message-board] {err}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
