"""Report whether the board is actually wired up.

This exists because the plugin is deliberately silent when unconfigured — a session that never heard
of the board still has to start — and that silence is indistinguishable from a board that is working
but simply has nothing to say. Every check below reports the difference explicitly, including the
healthy cases, so "quiet" is never left ambiguous.

The failure this was written after: hooks run in NON-INTERACTIVE shells, which do not source
`~/.zshrc`, so a vault path exported there is invisible to them. Nothing errored; nothing happened.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import vault

OK, WARN, BAD = "ok", "warn", "problem"


@dataclass
class Check:
    status: str
    name: str
    detail: str
    fix: str = ""


def _settings(project: Path) -> dict:
    path = project / ".claude" / "settings.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def probe_non_interactive_shell() -> str:
    """What a non-interactive shell sees. Separated so a test can supply an answer.

    Not a detail: this is the one check that reads state no argument controls, and a test that
    passes a scrubbed `env` while the real shell still inherits the operator's exported vault path
    is testing the machine it runs on rather than the code.
    """
    try:
        return subprocess.run(
            ["zsh", "-c", f"echo ${vault.VAULT_ENV}"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def check_env_reaches_hooks(
    project: Path, env: dict[str, str] | None = None, probe=probe_non_interactive_shell
) -> Check:
    """The vault path must reach a non-interactive shell, not just the user's terminal."""
    env = os.environ if env is None else env
    in_settings = _settings(project).get("env", {}).get(vault.VAULT_ENV)
    if in_settings:
        return Check(OK, "vault path reaches hooks", f"{vault.VAULT_ENV} set in .claude/settings.json")

    out = probe()

    if out:
        return Check(OK, "vault path reaches hooks", "visible to a non-interactive shell")
    if env.get(vault.VAULT_ENV):
        return Check(
            BAD, "vault path reaches hooks",
            f"{vault.VAULT_ENV} is set in your terminal but NOT in a non-interactive shell. "
            "Hooks run non-interactively and will silently do nothing.",
            'add it to "env" in .claude/settings.json — ~/.zshrc is not sourced for hooks',
        )
    return Check(BAD, "vault path reaches hooks", f"{vault.VAULT_ENV} is unset everywhere",
                 'set "env": {"%s": "/path/to/vault"} in .claude/settings.json' % vault.VAULT_ENV)


def effective_env(project: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    """What a hook will actually see: settings.json `env` layered over the ambient environment.

    Checking only the current shell would report a failure the hooks will not have, which is the
    same class of confusion this module exists to remove.
    """
    merged = dict(os.environ if env is None else env)
    merged.update({k: str(v) for k, v in _settings(project).get("env", {}).items()})
    return merged


def check_vault(env: dict[str, str] | None = None) -> Check:
    try:
        root = vault.resolve_vault(env)
    except vault.VaultError as err:
        return Check(BAD, "vault", str(err))
    if not vault.streams_dir(root).is_dir():
        return Check(WARN, "vault", f"{root} has no streams/ directory yet",
                     f'mkdir -p "{root}/streams"')
    is_git = (root / ".git").is_dir()
    n = len(vault.list_streams(root))
    detail = f"{root} · {n} stream(s)" + ("" if is_git else " · NOT a git repo")
    return Check(OK if is_git else WARN, "vault", detail,
                 "" if is_git else f'git -C "{root}" init — history is what makes overwrites and pruning safe')


def check_hooks(project: Path) -> Check:
    hooks = _settings(project).get("hooks", {})
    found = {
        event
        for event, groups in hooks.items()
        for group in groups
        for hook in group.get("hooks", [])
        if "plugin/hooks" in hook.get("command", "")
    }
    missing = {"SessionStart", "SessionEnd"} - found
    if missing:
        return Check(BAD, "hooks registered", f"missing: {', '.join(sorted(missing))}",
                     "add them to .claude/settings.json")
    return Check(OK, "hooks registered", "SessionStart, SessionEnd")


def check_skills(project: Path) -> Check:
    """Skills must be discoverable, or no agent can ever write to the board.

    Hooks write ground truth on their own, but every judgment write — decisions, blockers, inbox
    entries — is a deliberate skill invocation. Skills that live only in plugin/ are invisible.
    """
    wanted = {"brief-read", "brief-write", "inbox-append"}
    found = {p.name for p in (project / ".claude" / "skills").glob("*") if (p / "SKILL.md").is_file()}
    missing = wanted - found
    if missing:
        return Check(BAD, "skills discoverable", f"missing: {', '.join(sorted(missing))}",
                     "symlink plugin/skills/* into .claude/skills/ — otherwise nothing can write")
    return Check(OK, "skills discoverable", ", ".join(sorted(found)))


def check_ignored(env: dict[str, str] | None = None) -> Check:
    """Report ignore rules, because a silencing rule the operator forgot is indistinguishable from a
    broken hook — which is the exact confusion this whole module exists to remove."""
    from plugin.hooks import session_end

    rules = session_end.ignored_dirs(env)
    if not rules:
        return Check(OK, "ignore rules", "none — every unresolved session leaves a trace")
    listed = ", ".join(str(r) for r in rules)
    return Check(
        OK,
        "ignore rules",
        f"{len(rules)} configured: {listed} (non-repo directories only)",
    )


def run(project: Path, env: dict[str, str] | None = None, probe=probe_non_interactive_shell) -> list[Check]:
    merged = effective_env(project, env)
    return [
        check_env_reaches_hooks(project, env, probe),
        check_ignored(merged),
        check_vault(merged),
        check_hooks(project),
        check_skills(project),
    ]


def render(checks: list[Check]) -> str:
    mark = {OK: "ok  ", WARN: "warn", BAD: "FAIL"}
    lines = []
    for c in checks:
        lines.append(f"{mark[c.status]}  {c.name}: {c.detail}")
        if c.fix:
            lines.append(f"        fix: {c.fix}")
    worst = BAD if any(c.status == BAD for c in checks) else (
        WARN if any(c.status == WARN for c in checks) else OK)
    lines.append("")
    lines.append({
        OK: "Board is wired. If it is quiet, it is quiet because nothing has been written.",
        WARN: "Board works, with caveats above.",
        BAD: "Board is NOT working. Silence here means misconfiguration, not an empty board.",
    }[worst])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    project = Path(argv[0]) if argv else Path.cwd()
    checks = run(project)
    print(render(checks))
    return 1 if any(c.status == BAD for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
