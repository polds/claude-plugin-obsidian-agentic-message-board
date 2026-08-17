"""External reference resolution: names in a brief -> URLs a fresh agent can follow.

A brief is a conclusion summary, so it cites its evidence by name — `a1b2c3d4e`, `#1025`,
`PLAT-1719` — and a cold-start agent handed only the stream directory reported the predictable
result: "everything is by ID/filename reference only, so following up requires guessing URLs or
repo access." This module closes that gap by construction, from strings already in the vault.

Three rules shape the whole file, and all three come from that cold-start test rather than taste:

1. **Reachability, not reproduction.** Compression is the point of the artifact. Nothing here
   inlines evidence or makes a brief self-verifying; it only makes the evidence addressable.
2. **A wrong link is worse than no link.** A missing link costs a search. A confidently wrong one
   sends the next agent to someone else's commit and it will be believed, because a link looks
   verified. So body text is rewritten only when the stream's own claims settle the target
   unambiguously, and every unresolved reference is left exactly as the author typed it.
3. **Conventions are derived, never assumed to be one organisation's.** Hosts and path shapes come
   from the claim values themselves, then from configuration, and only then from a documented
   default that the `Ref.certain` flag marks as a guess.

Stdlib only, and deliberately offline: every URL here is *constructed*, never fetched. Nothing can
be checked against a live forge, which is precisely why rule 2 is strict.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from . import claims, vault

# Configuration for vaults whose forge or issue tracker cannot be derived from the claims. Named
# after MESSAGE_BOARD_VAULT so all plugin configuration reads as one family. Both are optional:
# unset means "derive or degrade", never "fail".
FORGE_BASE_ENV = "MESSAGE_BOARD_FORGE_BASE"
ISSUE_BASE_ENV = "MESSAGE_BOARD_ISSUE_BASE"

# A bare `owner/name` repo claim names no host. GitHub's own shorthand is exactly this spelling —
# `gh repo view` prints it, `git clone owner/name` assumes it — so that is the convention the
# spelling implies. It is a default, not a law: FORGE_BASE_ENV overrides it, and a claim written as
# a full URL beats both.
DEFAULT_FORGE_HOST = "github.com"

# Reference kinds that count as citing evidence for the lint. `repo`, `branch` and `worktree` are
# context, not proof: knowing which repo a decision touched does not tell you what to go read.
EVIDENCE_KINDS = ("commit", "pr", "issue", "path")

# Path shapes per forge family. Anything not matched here falls to the GitHub shape because gitea,
# forgejo, gogs and GitHub Enterprise all copy it — a guessed path on a host the claim itself named
# beats dropping the link, and it is wrong only for a self-hosted GitLab whose hostname hides it.
_FORGE_PATHS = {
    "github": {"pr": "pull/{n}", "commit": "commit/{sha}", "branch": "tree/{ref}"},
    "gitlab": {"pr": "-/merge_requests/{n}", "commit": "-/commit/{sha}", "branch": "-/tree/{ref}"},
    "bitbucket": {"pr": "pull-requests/{n}", "commit": "commits/{sha}", "branch": "src/{ref}"},
}

# Path segments that mean "the repo ended here" — a claim pasted from a browser address bar carries
# them, and appending `/pull/905` to `.../pull/905` produces a link that looks plausible and 404s.
_REPO_PATH_STOP = {
    "-", "blob", "commit", "commits", "compare", "issues", "merge_requests", "pull", "pulls",
    "pull-requests", "releases", "src", "tree", "wiki",
}

# Issue keys use the same regex claim resolution uses. A second issue-key dialect would let the
# linker and the resolver disagree about what a key even is, which is the duplicate-scheme bug the
# slug derivation in claims.py already warns about.
_ISSUE_KEY = claims._ISSUE_KEY

# `owner/repo#905` is self-describing and needs no claim to resolve; `#905` needs one.
_QUALIFIED_PR = re.compile(r"(?<![\w/#-])([A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*)#(\d{1,7})(?![\w-])")
_BARE_PR = re.compile(r"(?<![\w#/])#(\d{1,7})(?![\w-])")

# Abbreviated SHAs run 7-40 hex chars. Uppercase is excluded (git prints lowercase, and `CAFEBABE`
# in prose is not a commit), and a token must mix letters and digits: that rejection costs roughly
# the 1.5% of real 9-char SHAs that happen to be all digits, and buys immunity from every year,
# count, port number and `deadbeef` in the corpus. Losing a link is recoverable; inventing one is
# the failure mode this file exists to prevent.
_SHA = re.compile(r"(?<![\w/-])([0-9a-f]{7,40})(?![\w-])")

# File paths. The extension needs two or more characters so `e.g.` and `i.e.` stay prose.
_PATH = re.compile(r"(?<![\w./-])([\w{}][\w.{},/-]*\.[A-Za-z][\w{},-]{1,7})(?![\w-])")

_URL = re.compile(r"<?https?://[^\s<>)\]]+>?")
_WIKILINK = re.compile(r"\[\[[^\]]+\]\]")
_MD_LINK = re.compile(r"\[[^\]]*\]\([^)]*\)")
_CODE_SPAN = re.compile(r"`[^`\n]*`")
_FENCE = re.compile(r"^```.*?^```", re.S | re.M)
_BULLET = re.compile(r"^(?:[-*+]\s|\d+[.)]\s)")


@dataclass(frozen=True)
class RepoRef:
    """One repository, resolved far enough to build URLs against it."""

    host: str
    path: str
    scheme: str = "https"
    # False when the host was supplied by the default or by configuration rather than by the claim.
    # Recorded rather than acted on: the repo URL is still the best answer available.
    host_certain: bool = True

    @property
    def family(self) -> str:
        host = self.host.lower()
        if "gitlab" in host:
            return "gitlab"
        if "bitbucket" in host:
            return "bitbucket"
        return "github"

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}/{self.path}"

    def _sub(self, key: str, **parts: str) -> str:
        return f"{self.url}/{_FORGE_PATHS[self.family][key].format(**parts)}"

    def pr_url(self, number: object) -> str:
        return self._sub("pr", n=str(number).lstrip("#"))

    def commit_url(self, sha: str) -> str:
        return self._sub("commit", sha=sha)

    def branch_url(self, ref: str) -> str:
        return self._sub("branch", ref=ref)


@dataclass(frozen=True)
class Ref:
    """One external reference and the best URL available for it.

    An empty `url` is a real answer, not a failure — it means the claims did not say enough, and
    `basis` carries what was missing so a caller can tell the author what to add.
    """

    kind: str  # repo | branch | worktree | issue | pr | commit | path
    text: str  # verbatim, as the author wrote it
    url: str = ""
    certain: bool = True
    basis: str = ""
    span: tuple[int, int] | None = None

    @property
    def linkable(self) -> bool:
        """Safe to rewrite into body text. Guesses are not."""
        return bool(self.url) and self.certain


@dataclass(frozen=True)
class RefContext:
    """Everything a stream's claims say about where its references live.

    Built once per render and passed around, because deriving it per reference would re-parse the
    claims for every `#1234` in the body.
    """

    repos: tuple[RepoRef, ...] = ()
    branches: tuple[str, ...] = ()
    issue_template: str = ""  # contains the literal `{key}`
    issue_basis: str = ""
    issue_certain: bool = False
    projects: tuple[str, ...] = ()  # issue-key prefixes this stream actually claims
    default_host: str = DEFAULT_FORGE_HOST
    default_scheme: str = "https"

    @property
    def repo(self) -> RepoRef | None:
        """The single repo this stream claims, or None when it claims zero or several.

        Several is not better than none here. PR numbers and short SHAs are only unique within a
        repo, so a second claim turns every bare reference into a coin flip.
        """
        return self.repos[0] if len(self.repos) == 1 else None

    def issue_url(self, key: str) -> str:
        return self.issue_template.replace("{key}", key) if self.issue_template else ""


@dataclass(frozen=True)
class Finding:
    """Advisory output for the brief's author. Never an error, never a blocker.

    A brief that cites `a1b2c3d4e` without saying where to find it is still a legitimate brief —
    the compression is the point. It just has to say where the proof lives.
    """

    section: str
    index: int
    entry: str
    citations: tuple[Ref, ...] = ()
    message: str = ""
    details: tuple[str, ...] = ()

    def lines(self) -> list[str]:
        return [self.message, *[f"    {d}" for d in self.details]]

    def __str__(self) -> str:
        return "\n".join(self.lines())


# ---------------------------------------------------------------------------- context construction


def parse_repo(
    value: str,
    default_host: str = DEFAULT_FORGE_HOST,
    default_scheme: str = "https",
) -> RepoRef | None:
    """Parse a repo claim in any spelling a human or a tool might have written it.

    Accepts `owner/name`, `https://host/owner/name(.git)`, `git@host:owner/name.git`, and GitLab
    subgroup paths. Returns None when the value cannot address a repository — an owner with no
    repository name is not a location.
    """
    text = str(value).strip().rstrip("/")
    if not text:
        return None

    scheme, host, path = default_scheme, "", text
    if "://" in text:
        scheme, _, rest = text.partition("://")
        host, _, path = rest.partition("/")
    elif re.match(r"^[^/\s]+@[^/\s:]+:", text):  # git@github.com:Owner/repo.git
        host, _, path = text.partition(":")
        scheme = default_scheme
    if "@" in host:  # strip any userinfo; the credential is not part of the location
        host = host.rsplit("@", 1)[-1]

    segments: list[str] = []
    for segment in (s for s in path.split("/") if s):
        if segments and segment in _REPO_PATH_STOP:
            break
        segments.append(segment)
    if segments and segments[-1].endswith(".git"):
        segments[-1] = segments[-1][:-4]
    if len(segments) < 2:
        return None

    return RepoRef(
        host=host or default_host,
        path="/".join(segments),
        scheme=scheme,
        host_certain=bool(host),
    )


def _forge_default(env: dict[str, str]) -> tuple[str, str]:
    raw = env.get(FORGE_BASE_ENV, "").strip()
    if not raw:
        return DEFAULT_FORGE_HOST, "https"
    scheme, sep, rest = raw.partition("://")
    if not sep:
        return raw.strip("/").split("/")[0], "https"
    return rest.strip("/").split("/")[0], scheme


def _issue_template_from_base(base: str) -> str:
    """Turn an issue-tracker base into a `{key}` template.

    Trackers disagree about the shape — Linear is `<workspace>/issue/<KEY>`, Jira is
    `<host>/browse/<KEY>` — so a caller who needs a shape neither branch produces can pass the
    whole template and have it used verbatim.
    """
    base = base.strip()
    if "{key}" in base:
        return base
    base = base.rstrip("/")
    if not base:
        return ""
    if "linear.app" in base:
        return f"{base}/issue/{{key}}"
    return f"{base}/{{key}}"


def _issue_template(
    brief: vault.Brief,
    repos: Sequence[RepoRef],
    env: dict[str, str],
) -> tuple[str, str, bool]:
    """Where this stream's issue keys live: (template, basis, certain).

    Ordered most specific first. The last rung is a guess and says so, because an issue key carries
    its project but never its workspace — `PLAT-1962` is not enough to build a URL from, and no
    amount of string handling can make it enough.
    """
    for raw in brief.claim("issue"):
        value = str(raw).strip()
        if not _URL.match(value):
            continue
        keys = claims.parse_issue_keys(value)
        if not keys:
            continue
        # Rebuild the claim URL as a template by blanking the key it already contains, so sibling
        # keys in the body resolve to the same tracker the author actually used.
        template = re.sub(re.escape(keys[0]), "{key}", value, count=1, flags=re.I)
        return template, "from the `issue` claim URL", True

    configured = env.get(ISSUE_BASE_ENV, "").strip()
    if configured:
        return _issue_template_from_base(configured), f"from ${ISSUE_BASE_ENV}", True

    if len(repos) == 1:
        owner = repos[0].path.split("/")[0]
        workspace = claims.slugify(owner)
        if workspace:
            return (
                f"https://linear.app/{workspace}/issue/{{key}}",
                f"workspace guessed from repo owner '{owner}' — set ${ISSUE_BASE_ENV} to confirm",
                False,
            )

    return "", f"no issue-tracker base: no `issue` claim URL and ${ISSUE_BASE_ENV} is unset", False


def reference_context(brief: vault.Brief, env: dict[str, str] | None = None) -> RefContext:
    """Derive everything needed to resolve this stream's references. Never raises."""
    env = os.environ if env is None else env
    default_host, default_scheme = _forge_default(env)

    repos = tuple(
        repo
        for repo in (parse_repo(v, default_host, default_scheme) for v in brief.claim("repo"))
        if repo is not None
    )
    branches = tuple(str(b).strip() for b in brief.claim("branch") if str(b).strip())

    # A project prefix is only "known" if the stream claims it. That is the whole guard against
    # linking `UTF-8` to an issue tracker: claims.parse_issue_keys matches it, the claims do not.
    projects: list[str] = []
    for value in list(brief.claim("issue")) + list(branches):
        for key in claims.parse_issue_keys(str(value)):
            project = key.split("-")[0]
            if project not in projects:
                projects.append(project)

    template, basis, certain = _issue_template(brief, repos, env)
    return RefContext(
        repos=repos,
        branches=branches,
        issue_template=template,
        issue_basis=basis,
        issue_certain=certain,
        projects=tuple(projects),
        default_host=default_host,
        default_scheme=default_scheme,
    )


# ------------------------------------------------------------------------------- reference lookup


def _no_repo_basis(ctx: RefContext, what: str) -> str:
    if not ctx.repos:
        return f"no `repo` claim, so {what} names no location"
    return f"{len(ctx.repos)} `repo` claims, so {what} is ambiguous"


def _repo_for_path(ctx: RefContext, path: str) -> RepoRef:
    """Match a written-out `owner/name` against the claims, or build one on the default host."""
    for repo in ctx.repos:
        if repo.path.lower() == path.lower() or repo.path.lower().endswith("/" + path.lower()):
            return repo
    return RepoRef(
        host=ctx.default_host,
        path=path,
        scheme=ctx.default_scheme,
        host_certain=False,
    )


def _pr_ref(ctx: RefContext, number: str, text: str, span, repo: RepoRef | None) -> Ref:
    if repo is None:
        return Ref("pr", text, certain=False, basis=_no_repo_basis(ctx, "a PR number"), span=span)
    return Ref("pr", text, url=repo.pr_url(number), basis="from the `repo` claim", span=span)


def _commit_ref(ctx: RefContext, sha: str, span) -> Ref:
    repo = ctx.repo
    if repo is None:
        return Ref("commit", sha, certain=False, basis=_no_repo_basis(ctx, "a commit SHA"), span=span)
    return Ref("commit", sha, url=repo.commit_url(sha), basis="from the `repo` claim", span=span)


def _issue_ref(ctx: RefContext, text: str, key: str, span) -> Ref:
    project = key.split("-")[0]
    if project not in ctx.projects:
        return Ref(
            "issue",
            text,
            certain=False,
            basis=f"no `issue` or `branch` claim names project {project}",
            span=span,
        )
    url = ctx.issue_url(key)
    return Ref("issue", text, url=url, certain=bool(url) and ctx.issue_certain,
               basis=ctx.issue_basis, span=span)


def _path_ref(ctx: RefContext, text: str, span) -> Ref:
    # Deliberately never linked. A repo-relative path resolves only against a branch or commit, and
    # picking one the brief never named is exactly the confidently-wrong link rule 2 forbids. The
    # lint still reports it, because "which file" is evidence the author can point at.
    return Ref(
        "path",
        text,
        certain=False,
        basis="a repo-relative path needs a branch or commit to pin it to",
        span=span,
    )


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _spans(text: str, patterns: Iterable[re.Pattern[str]]) -> list[tuple[int, int]]:
    return [m.span() for pattern in patterns for m in pattern.finditer(text)]


def find_refs(text: str, ctx: RefContext, scan_code: bool = False) -> list[Ref]:
    """Every external reference in `text`, resolved as far as the claims allow.

    Already-linked references are never returned: a URL, a `[[wikilink]]` and a `[md](link)` are
    all "the author said where this lives", which is both the cure the lint asks for and the text
    a rewriter must not touch.

    `scan_code` decides whether code spans and fences are read. Rewriting inside them would corrupt
    the markdown, so the renderer leaves them alone — but a backticked `datastore.xrd.yaml` is
    still a citation, so the lint reads them.
    """
    linked = _spans(text, (_URL, _WIKILINK, _MD_LINK))
    code = _spans(text, (_FENCE, _CODE_SPAN))
    blocked = linked if scan_code else linked + code
    taken: list[tuple[int, int]] = []
    found: list[Ref] = []

    def free(span: tuple[int, int]) -> bool:
        return not any(_overlap(span, other) for other in blocked + taken)

    def add(ref: Ref) -> None:
        taken.append(ref.span)  # type: ignore[arg-type]
        found.append(ref)

    # Qualified PRs first: `owner/repo#905` contains a `#905` that must not also match on its own.
    for match in _QUALIFIED_PR.finditer(text):
        if free(match.span()):
            repo = _repo_for_path(ctx, match.group(1))
            add(_pr_ref(ctx, match.group(2), match.group(0), match.span(), repo))

    for match in _BARE_PR.finditer(text):
        if free(match.span()):
            add(_pr_ref(ctx, match.group(1), match.group(0), match.span(), ctx.repo))

    for match in _SHA.finditer(text):
        sha = match.group(1)
        if not (any(c.isdigit() for c in sha) and any(c.isalpha() for c in sha)):
            continue
        if free(match.span()):
            add(_commit_ref(ctx, sha, match.span()))

    for match in _ISSUE_KEY.finditer(text):
        if free(match.span()):
            key = f"{match.group(1).upper()}-{match.group(2)}"
            add(_issue_ref(ctx, match.group(0), key, match.span()))

    for match in _PATH.finditer(text):
        token = match.group(1)
        # A dotted token only counts as a path when it is either a real path or was backticked.
        # Bare prose is full of `Node.js` and `v1.2`; neither is evidence anyone can go read.
        if "/" not in token and not any(_overlap(match.span(), c) for c in code):
            continue
        if free(match.span()):
            add(_path_ref(ctx, token, match.span()))

    return sorted(found, key=lambda r: r.span or (0, 0))


# --------------------------------------------------------------------------------- public surface


def expand_claims(
    brief: vault.Brief,
    ctx: RefContext | None = None,
    env: dict[str, str] | None = None,
) -> list[Ref]:
    """Every claim this stream holds, with the URL that reaches it.

    Claim order follows vault.CLAIM_KINDS, which is the schema's order and therefore the reading
    order. Claims with no reachable URL are still returned — `worktree` never has one — because a
    caller rendering only the resolvable half would silently hide half the stream's identity.
    """
    ctx = reference_context(brief, env) if ctx is None else ctx
    refs: list[Ref] = []
    for kind in vault.CLAIM_KINDS:
        for raw in brief.claim(kind):
            refs.append(_claim_ref(ctx, kind, str(raw)))
    return refs


def _claim_ref(ctx: RefContext, kind: str, value: str) -> Ref:
    if kind == "repo":
        repo = parse_repo(value, ctx.default_host, ctx.default_scheme)
        if repo is None:
            return Ref(kind, value, certain=False, basis="not an addressable `owner/name`")
        basis = "" if repo.host_certain else f"host assumed: the claim names none, so {ctx.default_host}"
        return Ref(kind, value, url=repo.url, basis=basis)

    if kind == "branch":
        repo = ctx.repo
        if repo is None:
            return Ref(kind, value, certain=False, basis=_no_repo_basis(ctx, "a branch name"))
        return Ref(kind, value, url=repo.branch_url(value), basis="from the `repo` claim")

    if kind == "worktree":
        # A local absolute path on one machine. The vault is explicitly single-machine, so this is
        # already the most useful form it has; dressing it up as file:// would only imply otherwise.
        return Ref(kind, value, certain=False, basis="a local path, not a URL")

    if kind == "issue":
        if _URL.match(value.strip()):
            return Ref(kind, value, url=value.strip(), basis="the claim is already a URL")
        keys = claims.parse_issue_keys(value)
        key = keys[0] if keys else value.strip()
        url = ctx.issue_url(key)
        return Ref(kind, value, url=url, certain=bool(url) and ctx.issue_certain, basis=ctx.issue_basis)

    if _URL.match(value.strip()):  # pr
        return Ref(kind, value, url=value.strip(), basis="the claim is already a URL")
    repo = ctx.repo
    if repo is None:
        return Ref(kind, value, certain=False, basis=_no_repo_basis(ctx, "a PR number"))
    return Ref(kind, value, url=repo.pr_url(value), basis="from the `repo` claim")


def claim_lines(refs: Sequence[Ref]) -> list[str]:
    """Render expanded claims as aligned `kind value -> url` lines.

    `->` is derived, `~>` is a guess with its reason attached, and an unreachable claim says what
    was missing. The distinction is the whole point: an unmarked guess is the wrong link rule 2
    forbids, just displayed instead of embedded.
    """
    if not refs:
        return []
    kind_width = max(len(r.kind) for r in refs)
    text_width = max(len(r.text) for r in refs)
    lines = []
    for ref in refs:
        head = f"{ref.kind:<{kind_width}}  {ref.text:<{text_width}}"
        if ref.url and ref.certain:
            lines.append(f"{head}  -> {ref.url}")
        elif ref.url:
            lines.append(f"{head}  ~> {ref.url}  ({ref.basis})" if ref.basis else f"{head}  ~> {ref.url}")
        else:
            lines.append(f"{head}     ({ref.basis})" if ref.basis else head.rstrip())
    return lines


def link_text(text: str, ctx: RefContext) -> str:
    """Rewrite bare references in body text as markdown links.

    Only unambiguous references are touched. A guessed tracker workspace, a PR number with no repo
    claim, a SHA in a stream claiming two repos and anything already inside a link or a code span
    all come through byte-identical — the author's words are the fallback, and they are a correct
    one.
    """
    out = text
    for ref in sorted(find_refs(text, ctx), key=lambda r: r.span[0], reverse=True):  # type: ignore[index]
        if not ref.linkable or ref.kind == "path" or ref.span is None:
            continue
        start, end = ref.span
        out = f"{out[:start]}[{ref.text}]({ref.url}){out[end:]}"
    return out


def _entries(section: str) -> list[str]:
    """Split a section into entries. A wrapped line belongs to the entry above it."""
    entries: list[str] = []
    buffer: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped or _BULLET.match(stripped):
            if buffer:
                entries.append(" ".join(buffer))
                buffer = []
        if stripped:
            buffer.append(stripped)
    if buffer:
        entries.append(" ".join(buffer))
    return entries


def _summarize(entry: str, limit: int = 72) -> str:
    body = _BULLET.sub("", entry).strip()
    return body if len(body) <= limit else body[: limit - 1].rstrip() + "…"


def lint_decided(
    brief: vault.Brief,
    ctx: RefContext | None = None,
    env: dict[str, str] | None = None,
    section: str = "Decided",
) -> list[Finding]:
    """Advisory: `## Decided` entries that cite evidence without saying where it lives.

    Advisory is the whole contract — never an error, never a blocker. The brief is legitimately a
    conclusion summary, and the cold-start agent's complaint was not that the trace was missing but
    that it was unreachable: "anyone resuming is asked to trust the 'traced to a live replacement'
    claim without being shown the trace."

    A citation the claims can resolve is still reported, with the URL it resolved to. That is not
    redundant with the renderer: resolution depends on claims that can change, so an entry whose
    only route to its evidence is inference goes dark the moment a `repo` claim is edited. The
    suggested URL is there to be pasted in.
    """
    ctx = reference_context(brief, env) if ctx is None else ctx
    findings: list[Finding] = []

    for index, entry in enumerate(_entries(brief.sections.get(section, "")), start=1):
        cited = [r for r in find_refs(entry, ctx, scan_code=True) if r.kind in EVIDENCE_KINDS]
        if not cited:
            continue

        details = []
        for ref in cited:
            if ref.url and ref.certain:
                details.append(f"{ref.text}  -> {ref.url}  ({ref.basis})")
            elif ref.url:
                details.append(f"{ref.text}  ~> {ref.url}  (guess — {ref.basis})")
            else:
                details.append(f"{ref.text}  -- unreachable: {ref.basis}")

        names = ", ".join(dict.fromkeys(r.text for r in cited))
        findings.append(
            Finding(
                section=section,
                index=index,
                entry=entry,
                citations=tuple(cited),
                message=(
                    f"advisory: ## {section} entry {index} cites {names} but links none of it "
                    f"— \"{_summarize(entry)}\""
                ),
                details=tuple(details),
            )
        )
    return findings
