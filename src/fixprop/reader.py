"""A model reads every pair through the API: one independent conversation per pair, with every request and response
kept.

  python -m fixprop read --out <dir> --model <model>   read every pair that has no accepted transcript in <dir> yet
  python -m fixprop read --out <dir> --collect         build <dir>/<codebase>/labels*.jsonl from the transcripts
  python -m fixprop read --out <dir> --request <id>    print the first request of one pair, without sending anything
  python -m fixprop read --out <dir> --show <id>       print one transcript in readable form
  python -m fixprop read --out <dir> --usage           calls, tokens and cost per codebase

The reader talks to the OpenRouter chat completions API. The API key is read from the environment variable
OPENROUTER_API_KEY, or from a line OPENROUTER_API_KEY=... in the file .env at the project root. The key is sent only in
the Authorization header and is never written anywhere.

One conversation per pair:
  system message   the reading rules (packets.GUIDE, or GUIDE_DIVERGED for a sampled diverged pair)
  user message     the pair's packet (packets.candidate_packet or diverged_packet)
  then the model calls tools until it submits a judgment that passes the checks.

The tools can only read: lines of a file at a commit, a directory listing, a text search in the local checkout, the
change a commit made to a file, and the latest commits of a file. Each takes a repository name and a relative path,
checks both, and runs one fixed git or grep command; the model cannot run anything else. submit_judgment runs the same
checks as the merge (labels.check_one) and returns the problems, so the model corrects them itself.

Every request is the same fixed part (model, tools, reasoning effort, output limit, provider, caching) followed by the
messages so far. Nothing earlier in a conversation is ever edited. A transcript therefore holds the fixed part once,
the messages in order, and for each call the number of messages that were sent and the complete response as received,
with its token usage and cost: request k is the fixed part plus the first calls[k].messages_sent messages.
Transcripts are written to <dir>/<codebase>/transcripts/<id>.json.gz.
"""

from __future__ import annotations

import concurrent.futures as cf
import gzip
import hashlib
import io
import json
import os
import random
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

from . import gitio, packets, paths
from .candidates import id_order
from .codebases import ORDER, all_repos
from .common import log, write_jsonl
from .labels import BASIC_FIELDS as BASIC, CONFIDENCE, IMPACT, JUDGMENT_FIELDS as FIELDS, LABELS, LABELS_DIVERGED, PORT, \
    REAL, check_one

BASE_URL = "https://openrouter.ai/api/v1"
KEY_VAR, URL_VAR = "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL"
FORMAT = 1  # version of the transcript layout
EFFORTS = ("low", "medium", "high", "xhigh", "max")

MAX_READ, DEFAULT_READ = 400, 200  # lines returned by one read_file call: at most, and when no end is given
MAX_MATCHES, MAX_ENTRIES, MAX_DIFF, MAX_LOG, MAX_WIDTH = 80, 400, 800, 30, 400  # caps on the other tool results
MAX_CALLS = 40  # API calls in one conversation
MAX_REJECTED = 5  # submissions that fail the checks before the conversation is given up
MAX_NUDGES = 2  # reminders after an answer without a tool call
RETRIES = 6  # attempts for one request
TIMEOUT = 900  # seconds for one request

NUDGE = "No judgment has been submitted yet. Call submit_judgment with your judgment of this pair."
KEYS = ("id", "codebase", "kind", "commit", "target_repo", "target_unit")

_sleep = time.sleep


# ------------------------------------------------------------------ the key and the endpoint


def _dotenv() -> dict[str, str]:
    """The NAME=value lines of .env at the project root."""
    out: dict[str, str] = {}
    f = paths.ROOT / ".env"
    if f.is_file():
        for ln in f.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln.startswith("export "):
                ln = ln[7:].lstrip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            name, value = ln.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value:
                out[name.strip()] = value
    return out


def api_key() -> str | None:
    """The API key: the environment variable OPENROUTER_API_KEY, or else the line of that name in .env."""
    return os.environ.get(KEY_VAR, "").strip() or _dotenv().get(KEY_VAR)


def endpoint() -> str:
    base = os.environ.get(URL_VAR, "").strip() or _dotenv().get(URL_VAR) or BASE_URL
    return base.rstrip("/") + "/chat/completions"


# ------------------------------------------------------------------ the tools the model can call


def tool_list(kind: str) -> list[dict]:
    """The tools of one conversation. The list is part of every request and does not change within a conversation."""
    repo = {"type": "string", "description": "Repository as <owner>/<repo>, as named in the packet."}
    path = {"type": "string", "description": "Path relative to the repository root."}
    commit = {"type": "string", "description": "A commit hash, optionally followed by ^ for the commit before it. "
                                              "Leave it out for the repository's pinned commit."}

    def fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
        return {"type": "function", "function": {"name": name, "description": description, "parameters": {
            "type": "object", "properties": properties, "required": required, "additionalProperties": False}}}

    ref = {"type": "object", "additionalProperties": False, "required": ["file", "line", "quote"], "properties": {
        "file": {"type": "string", "description": "Path inside the repository."},
        "line": {"type": "integer", "description": "Line number at the pinned commit."},
        "quote": {"type": "string", "description": "The text of that line, or a part of it, verbatim."},
        "repo": {"type": "string", "description": "Only when the file is not in the repository of the other driver: "
                                                  "<owner>/<repo>."}}}
    where = {"type": "object", "additionalProperties": False, "required": ["file", "line"], "properties": {
        "file": {"type": "string"}, "line": {"type": "integer"}},
        "description": "The file and line of the problem on the other side; one of the evidence_refs."}
    return [
        fn("read_file", f"Read lines of one file, with line numbers. At most {MAX_READ} lines per call.",
           {"repo": repo, "path": path,
            "start_line": {"type": "integer", "description": "First line, counted from 1. Default 1."},
            "end_line": {"type": "integer", "description": f"Last line. Default: {DEFAULT_READ} lines from start_line."},
            "commit": commit}, ["repo", "path"]),
        fn("list_dir", "List the entries of a directory. Directories end with /.",
           {"repo": repo, "path": {"type": "string", "description": "Directory relative to the repository root. "
                                                                   "Leave it out for the root."},
            "commit": commit}, ["repo"]),
        fn("search", "Find a string in the files checked out locally at the pinned commit. Returns path:line:text, "
                     f"at most {MAX_MATCHES} matching lines.",
           {"repo": repo, "pattern": {"type": "string", "description": "The text to find, on one line."},
            "path": {"type": "string", "description": "File or directory to search, relative to the repository root. "
                                                      "Leave it out for the whole checkout."},
            "regex": {"type": "boolean", "description": "Treat pattern as an extended regular expression. "
                                                        "Default false."}}, ["repo", "pattern"]),
        fn("commit_diff", "Show the full change one commit made to one file, as a unified diff.",
           {"repo": repo, "commit": {"type": "string", "description": "The commit hash."}, "path": path},
           ["repo", "commit", "path"]),
        fn("file_log", "List the latest commits that changed a file, newest first: short hash, date, subject.",
           {"repo": repo, "path": path,
            "limit": {"type": "integer", "description": f"How many commits, at most {MAX_LOG}. Default 10."}},
           ["repo", "path"]),
        fn("submit_judgment", "Submit your judgment of this pair. The call is checked; if it reports problems, fix "
                              "them and call again.",
           {"label": {"type": "string", "enum": list(LABELS if kind == "candidate" else LABELS_DIVERGED)},
            "confidence": {"type": "string", "enum": list(CONFIDENCE)},
            "evidence": {"type": "string"},
            "evidence_refs": {"type": "array", "items": ref},
            "port": {"type": "string", "enum": list(PORT)},
            "impact": {"type": "string", "enum": list(IMPACT)},
            "impact_note": {"type": "string"},
            "where": where,
            "title": {"type": "string"},
            "summary": {"type": "string"}}, ["label", "confidence", "evidence", "evidence_refs"]),
    ]


class ToolError(Exception):
    """A problem that is reported back to the model as the tool's result."""


_COMMIT = re.compile(r"[0-9a-fA-F]{7,40}(?:\^|~\d{1,2})?")


def _repo(args: dict) -> str:
    repo = args.get("repo")
    if not isinstance(repo, str) or repo not in all_repos():
        raise ToolError(f"unknown repository {repo!r}; use a repository named in the packet, written <owner>/<repo>")
    return repo


def _path(value, empty_ok: bool = False) -> str:
    p = "" if value is None else str(value).strip()
    while p.startswith("./"):
        p = p[2:]
    p = p.rstrip("/")
    if p in ("", "."):
        if empty_ok:
            return ""
        raise ToolError("path is required")
    if p.startswith(("/", "-")) or any(c in p for c in "\\:\0\n") or any(x in ("", ".", "..", ".git") for x in p.split("/")):
        raise ToolError("path must be relative to the repository root, without .. parts")
    return p


def _commit(args: dict, repo: str, required: bool = False) -> str:
    value = args.get("commit")
    if value in (None, ""):
        if required:
            raise ToolError("commit is required")
        return all_repos()[repo]
    if not isinstance(value, str) or not _COMMIT.fullmatch(value.strip()):
        raise ToolError("commit must be a commit hash of 7 to 40 hex digits, optionally followed by ^")
    return value.strip()


def _int(args: dict, name: str, default: int) -> int:
    value = args.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{name} must be a whole number")
    return value


def _at(repo: str, commit: str) -> str:
    return "the pinned commit" if commit == all_repos()[repo] else f"commit {commit}"


def _clip(lines: list[str], limit: int, what: str) -> list[str]:
    shown = [ln if len(ln) <= MAX_WIDTH else ln[:MAX_WIDTH] + " ..." for ln in lines[:limit]]
    return shown + ([f"... ({len(lines) - limit} more {what} not shown)"] if len(lines) > limit else [])


def _file_lines(repo: str, path: str, commit: str) -> list[str] | None:
    """The lines of a file at a commit, or None if there is no such file. The pinned commit is read from the checkout
    when the file is there and really inside it; everything else is read from the history."""
    if commit == all_repos()[repo]:
        root = paths.repo_dir(repo).resolve()
        local = root / path
        try:
            inside = local.resolve().is_relative_to(root)
        except OSError:
            inside = False
        if inside and local.is_file() and not local.is_symlink():
            return local.read_text(encoding="utf-8", errors="replace").split("\n")
    text = gitio.text(gitio.git_dir(repo), "show", f"{commit}:{path}", check=False, net=True)
    return text.split("\n") if text else None


def t_read_file(args: dict) -> str:
    repo, path = _repo(args), _path(args.get("path"))
    commit = _commit(args, repo)
    lines = _file_lines(repo, path, commit)
    if lines is None:
        raise ToolError(f"{repo} has no file {path} at {_at(repo, commit)}; list_dir shows what is there")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    start = max(1, _int(args, "start_line", 1))
    end = min(len(lines), _int(args, "end_line", start + DEFAULT_READ - 1), start + MAX_READ - 1)
    if start > len(lines) or end < start:
        raise ToolError(f"{path} has {len(lines)} lines; lines {start} to {end} are not in it")
    head = f"{repo}:{path} at {_at(repo, commit)}, lines {start}-{end} of {len(lines)}"
    return "\n".join([head] + _clip([f"{i:6} {lines[i - 1]}" for i in range(start, end + 1)], MAX_READ, "lines"))


def t_list_dir(args: dict) -> str:
    repo, path = _repo(args), _path(args.get("path"), empty_ok=True)
    commit = _commit(args, repo)
    out = gitio.text(gitio.git_dir(repo), "ls-tree", commit, *([path + "/"] if path else []), check=False)
    names = []
    for ln in out.split("\n"):
        meta, _, name = ln.partition("\t")
        if name:
            names.append(name + ("/" if meta.split()[1:2] == ["tree"] else ""))
    if not names:
        raise ToolError(f"{repo} has no directory {path or '/'} at {_at(repo, commit)}")
    head = f"{repo}:{path or '/'} at {_at(repo, commit)}, {len(names)} entries"
    return "\n".join([head] + _clip(names, MAX_ENTRIES, "entries"))


def t_search(args: dict) -> str:
    repo, path = _repo(args), _path(args.get("path"), empty_ok=True)
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip() or len(pattern) > 300 or "\n" in pattern or "\0" in pattern:
        raise ToolError("pattern must be one line of text, at most 300 characters")
    root = paths.repo_dir(repo).resolve()
    target = root / path if path else root
    try:
        ok = target.exists() and target.resolve().is_relative_to(root)
    except OSError:
        ok = False
    if not ok:
        raise ToolError(f"{path or '/'} is not checked out locally in {repo}; search covers only the local checkout "
                        "(use list_dir and read_file for other files)")
    cmd = ["grep", "-rnHI", "--exclude-dir=.git", "-E" if args.get("regex") is True else "-F", "-e", pattern, "--",
           path or "."]
    r = subprocess.run(cmd, cwd=root, capture_output=True, timeout=120,
                       env={"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C"})
    if r.returncode > 1:
        raise ToolError("pattern is not a valid regular expression" if args.get("regex") is True else "the search failed")
    lines = [ln[2:] if ln.startswith("./") else ln for ln in r.stdout.decode("utf-8", "replace").split("\n") if ln]
    where = f"{repo}:{path or '/'}"
    if not lines:
        return f"No line in {where} matches {pattern!r}."
    return "\n".join([f"{len(lines)} matching lines in {where}"] + _clip(lines, MAX_MATCHES, "matching lines"))


def t_commit_diff(args: dict) -> str:
    repo, path = _repo(args), _path(args.get("path"))
    commit = _commit(args, repo, required=True)
    out = gitio.text(gitio.git_dir(repo), "show", "--format=", "--no-color", "--no-ext-diff", "--no-textconv",
                     "--no-renames", "--diff-algorithm=myers", "-U10", commit, "--", path, check=False, net=True)
    if not out.strip():
        raise ToolError(f"commit {commit} does not change {path} in {repo}, or it is not in the fetched history")
    return "\n".join(_clip(out.rstrip("\n").split("\n"), MAX_DIFF, "lines"))


def t_file_log(args: dict) -> str:
    repo, path = _repo(args), _path(args.get("path"))
    n = min(MAX_LOG, max(1, _int(args, "limit", 10)))
    out = gitio.text(gitio.git_dir(repo), "log", f"-{n}", "--format=%h %cs %s", all_repos()[repo], "--", path,
                     check=False)
    if not out.strip():
        raise ToolError(f"no commit since 2023-06-01 changes {path} in {repo}")
    return "\n".join(_clip(out.rstrip("\n").split("\n"), MAX_LOG, "commits"))


TOOLS = {"read_file": t_read_file, "list_dir": t_list_dir, "search": t_search, "commit_diff": t_commit_diff,
         "file_log": t_file_log}


def run_tool(name: str, args) -> str:
    """Run one read-only tool and return its result as text. Errors come back as text too."""
    f = TOOLS.get(name)
    if f is None:
        return f"Error: there is no tool named {name!r}."
    if not isinstance(args, dict):
        return "Error: the arguments are not a JSON object."
    try:
        return f(args)
    except ToolError as e:
        return f"Error: {e}."
    except (subprocess.TimeoutExpired, gitio.GitError, OSError):
        return "Error: the lookup failed."


# ------------------------------------------------------------------ talking to the API


class ApiError(Exception):
    """A request that failed for good."""


class FatalApiError(ApiError):
    """A failure that would repeat for every pair (bad key, no credit, unknown model): the run stops."""


def http_post(url: str, key: str, body: dict) -> tuple[int, dict | None, str, float | None]:
    """One POST. Returns (HTTP status or 0, the JSON body if there is one, a short error name, seconds the server
    asked to wait). Only two headers are set: the authorization and the content type."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw, status, headers = r.read(), r.status, r.headers
    except urllib.error.HTTPError as e:
        raw, status, headers = e.read(), e.code, e.headers
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        return 0, None, type(e).__name__, None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except ValueError:
        parsed = None
    wait = headers.get("Retry-After") if headers is not None else None
    return status, parsed if isinstance(parsed, dict) else None, "", float(wait) if wait and wait.isdigit() else None


def _error_text(data: dict | None) -> str:
    err = (data or {}).get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or "")[:300]
    return str(err)[:300] if err else ""


def complete(post, body: dict) -> tuple[dict, int]:
    """Send one request, retrying what is worth retrying. Returns (the response, the number of attempts)."""
    message = ""
    for attempt in range(1, RETRIES + 1):
        status, data, err, wait = post(body)
        if status == 200 and data and isinstance(data.get("choices"), list) and data["choices"] \
                and isinstance(data["choices"][0].get("message"), dict):
            return data, attempt
        message = _error_text(data) or err or f"HTTP {status}"
        if status in (401, 402, 403, 404):
            raise FatalApiError(f"HTTP {status}: {message}")
        if status in (400, 413, 422):
            raise ApiError(f"HTTP {status}: {message}")
        if attempt < RETRIES:
            _sleep(wait if wait is not None else min(60.0, 2.0 ** attempt) + random.random())
    raise ApiError(f"no usable response after {RETRIES} attempts: {message}")


def fixed_request(kind: str, model: str, effort: str | None, max_tokens: int, provider: str | None) -> dict:
    """The part of the request that is the same for every call of a conversation."""
    fixed: dict = {"model": model, "tools": tool_list(kind), "max_tokens": max_tokens}
    if effort:
        fixed["reasoning"] = {"effort": effort}
    if provider:
        fixed["provider"] = {"only": [provider], "allow_fallbacks": False, "require_parameters": True}
    if model.startswith("anthropic/"):
        fixed["cache_control"] = {"type": "ephemeral"}  # automatic prompt caching: the cached prefix grows with the conversation
    return fixed


# ------------------------------------------------------------------ one conversation


def _judge(entry: dict, args, allowed: tuple[str, ...]) -> tuple[dict | None, list[str]]:
    """Check a submitted judgment. Returns (the judgment, the problems)."""
    if not isinstance(args, dict):
        return None, ["the arguments are not a JSON object"]
    lab = {k: args[k] for k in FIELDS if args.get(k) not in (None, "", [], {})}
    try:
        problems = check_one(entry["id"], entry["target_repo"], lab, allowed)
    except (AttributeError, TypeError, KeyError, ValueError):
        return None, ["the fields do not have the types the tool asks for"]
    if lab.get("label") not in REAL:
        lab = {k: v for k, v in lab.items() if k in BASIC}
    return lab, problems


def _arguments(tool_call: dict) -> tuple[str, object]:
    f = tool_call.get("function") or {}
    raw = f.get("arguments")
    try:
        return str(f.get("name")), (json.loads(raw) if isinstance(raw, str) and raw.strip() else {})
    except ValueError:
        return str(f.get("name")), None


def _usage(calls: list[dict]) -> dict:
    u = {"calls": len(calls), "prompt_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0, "completion_tokens": 0,
         "reasoning_tokens": 0, "cost": 0.0}
    for c in calls:
        x = c["response"].get("usage") or {}
        pd, cd = x.get("prompt_tokens_details") or {}, x.get("completion_tokens_details") or {}
        u["prompt_tokens"] += x.get("prompt_tokens") or 0
        u["cached_tokens"] += pd.get("cached_tokens") or 0
        u["cache_write_tokens"] += pd.get("cache_write_tokens") or 0
        u["completion_tokens"] += x.get("completion_tokens") or 0
        u["reasoning_tokens"] += cd.get("reasoning_tokens") or 0
        u["cost"] += x.get("cost") or 0.0
    u["cost"] = round(u["cost"], 6)
    return u


def read_pair(entry: dict, packet: str, fixed: dict, post, url: str = "") -> dict:
    """Run the conversation of one pair and return its transcript."""
    candidate = entry["kind"] == "candidate"
    allowed = LABELS if candidate else LABELS_DIVERGED
    messages: list[dict] = [
        {"role": "system", "content": packets.GUIDE if candidate else packets.GUIDE_DIVERGED},
        {"role": "user", "content": packet}]
    calls: list[dict] = []
    outcome, rejected, nudges = None, 0, 0
    try:
        while outcome is None:
            if len(calls) >= MAX_CALLS:
                outcome = {"status": "failed", "reason": f"no accepted judgment after {MAX_CALLS} calls"}
                break
            sent = len(messages)
            response, attempts = complete(post, {**fixed, "messages": messages})
            calls.append({"messages_sent": sent, "attempts": attempts, "response": response})
            choice = response["choices"][0]
            message = choice["message"]
            messages.append(message)  # sent back exactly as received, reasoning included
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                if choice.get("finish_reason") == "length":
                    outcome = {"status": "failed", "reason": "the output limit was reached before a judgment was submitted"}
                elif nudges >= MAX_NUDGES:
                    outcome = {"status": "failed", "reason": "the model answered without submitting a judgment"}
                else:
                    nudges += 1
                    messages.append({"role": "user", "content": NUDGE})
                continue
            accepted = None
            for tc in tool_calls:
                name, args = _arguments(tc)
                if name == "submit_judgment":
                    judgment, problems = _judge(entry, args, allowed)
                    if problems:
                        rejected += 1
                        result = "Not accepted. Fix these problems and call submit_judgment again:\n" \
                                 + "\n".join("- " + p for p in problems)
                    elif accepted is not None:
                        result = "A judgment was already accepted."
                    else:
                        accepted, result = judgment, "Accepted."
                else:
                    result = run_tool(name, args)
                messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": result})
            if accepted is not None:
                outcome = {"status": "accepted", "judgment": accepted, "rejected_submissions": rejected}
            elif rejected > MAX_REJECTED:
                outcome = {"status": "failed", "reason": f"{rejected} submissions did not pass the checks"}
    except FatalApiError:
        raise
    except ApiError as e:
        outcome = {"status": "failed", "reason": f"API error: {e}"}
    return {"format": FORMAT, **{k: entry[k] for k in KEYS}, "endpoint": url, "request": fixed, "messages": messages,
            "calls": calls, "outcome": outcome, "usage": _usage(calls)}


# ------------------------------------------------------------------ transcripts on disk


def transcript_path(out: Path, entry: dict) -> Path:
    return out / entry["codebase"] / "transcripts" / f"{entry['id']}.json.gz"


def save(path: Path, transcript: dict) -> None:
    """Write a transcript as gzipped JSON. The gzip header carries no time and no file name."""
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", fileobj=buf, mode="wb", mtime=0) as g:
        g.write(json.dumps(transcript, ensure_ascii=False, indent=1).encode("utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(buf.getvalue())
    os.replace(tmp, path)


def load(path: Path) -> dict:
    with gzip.open(path, "rb") as g:
        return json.loads(g.read().decode("utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _accepted(path: Path) -> bool:
    try:
        return path.is_file() and load(path).get("outcome", {}).get("status") == "accepted"
    except (OSError, ValueError, EOFError):
        return False


def _failed_path(path: Path) -> Path:
    """Where to keep a conversation that ended without an accepted judgment: <id>.failed-<n>.json.gz."""
    stem = path.name[:-len(".json.gz")]
    n = 1
    while (path.parent / f"{stem}.failed-{n}.json.gz").exists():
        n += 1
    return path.parent / f"{stem}.failed-{n}.json.gz"


# ------------------------------------------------------------------ a reading run


def run(out: Path, model: str, effort: str | None = "medium", max_tokens: int = 32000, provider: str | None = "anthropic",
        ids: list[str] | None = None, codebase: str | None = None, kind: str | None = None, limit: int | None = None,
        jobs: int = 4, max_cost: float | None = None, redo: bool = False, post=None) -> int:
    """Read the selected pairs that have no accepted transcript in out yet. Returns 0 when every one was accepted."""
    url = endpoint()
    if post is None:
        key = api_key()
        if not key:
            log(f"[read] no API key: set {KEY_VAR} in the environment, or put {KEY_VAR}=... in .env at the project root")
            return 2
        post = lambda body: http_post(url, key, body)  # noqa: E731
    todo, known, have = [], set(), 0
    for entry, text in packets.iter_packets(True):
        known.add(entry["id"])
        if (ids and entry["id"] not in ids) or (codebase and entry["codebase"] != codebase) or (kind and entry["kind"] != kind):
            continue
        if not redo and _accepted(transcript_path(out, entry)):
            have += 1
            continue
        todo.append((entry, text))
    unknown = sorted(set(ids or []) - known)
    if unknown:
        log(f"[read] unknown IDs: {' '.join(unknown)}")
        return 2
    if limit is not None:
        todo = todo[:limit]
    log(f"[read] {len(todo)} pairs to read ({have} already have an accepted transcript); model {model}, "
        f"effort {effort or 'not set'}, provider {provider or 'any'}, {jobs} at a time, transcripts in {paths.rel(out)}/")
    lock, stop = threading.Lock(), threading.Event()
    total = {"accepted": 0, "failed": 0, "cost": 0.0, "fatal": ""}

    def work(entry: dict, text) -> dict | None:
        if stop.is_set():
            return None
        try:
            return read_pair(entry, text(), fixed_request(entry["kind"], model, effort, max_tokens, provider), post, url)
        except FatalApiError:
            stop.set()  # set here, in the worker, so that no other pair is started in the meantime
            raise

    with cf.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = {pool.submit(work, entry, text): entry for entry, text in todo}
        for fut in cf.as_completed(futures):
            entry = futures[fut]
            try:
                t = fut.result()
            except FatalApiError as e:
                stop.set()
                total["fatal"] = total["fatal"] or str(e)
                continue
            if t is None:
                continue
            ok = t["outcome"]["status"] == "accepted"
            path = transcript_path(out, entry)
            save(path if ok else _failed_path(path), t)
            u = t["usage"]
            with lock:
                total["accepted" if ok else "failed"] += 1
                total["cost"] += u["cost"]
                spent = total["cost"]
            what = t["outcome"]["judgment"]["label"] if ok else "FAILED: " + t["outcome"]["reason"]
            log(f"[read] {entry['id']}: {what} ({u['calls']} calls, {u['prompt_tokens']:,} prompt tokens, "
                f"{u['cached_tokens']:,} cached, {u['completion_tokens']:,} completion tokens, ${u['cost']:.3f}; "
                f"run total ${spent:.2f})")
            if max_cost is not None and spent >= max_cost and not stop.is_set():
                stop.set()
                log(f"[read] the run has cost ${spent:.2f}, at or above the limit of ${max_cost:.2f}: no new pairs are started")
    left = len(todo) - total["accepted"] - total["failed"]
    if total["fatal"]:
        log(f"[read] stopped: {total['fatal']}")
    log(f"[read] accepted {total['accepted']}, failed {total['failed']}, not started {left}; cost of this run ${total['cost']:.2f}")
    return 0 if not (total["failed"] or left or total["fatal"]) else 1


# ------------------------------------------------------------------ from transcripts to judgments


def collect(out: Path, partial: bool = False) -> int:
    """Build <out>/<codebase>/labels.jsonl and labels_diverged.jsonl from the accepted transcripts in out. Every
    judgment is checked again and carries the model as its reader and the checksum of its transcript. Without partial,
    every pair must have an accepted transcript, and nothing is written otherwise. Returns the number of problems."""
    rows: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    missing, problems, models = [], [], set()
    for entry, _ in packets.iter_packets(True):
        f = transcript_path(out, entry)
        if not _accepted(f):
            missing.append(entry["id"])
            continue
        t = load(f)
        if any(t.get(k) != entry[k] for k in KEYS):
            problems.append(f"{entry['id']}: the transcript belongs to another pair")
            continue
        lab = t["outcome"]["judgment"]
        problems += check_one(entry["id"], entry["target_repo"], lab,
                              LABELS if entry["kind"] == "candidate" else LABELS_DIVERGED)
        models.add(t["request"]["model"])
        rows[(entry["codebase"], entry["kind"])][entry["id"]] = {
            "id": entry["id"], "commit": entry["commit"], "target_repo": entry["target_repo"],
            "target_unit": entry["target_unit"], **lab, "reader": t["request"]["model"], "transcript_sha256": sha256(f)}
    for p in problems:
        log("    " + p)
    if missing and not partial:
        log(f"[read] {len(missing)} pairs have no accepted transcript in {paths.rel(out)}/ "
            f"({' '.join(missing[:12])}{' ...' if len(missing) > 12 else ''}); nothing was written. "
            "Read them first, or pass --partial to collect only the pairs that are there.")
        return len(missing) + len(problems)
    if problems:
        log(f"[read] {len(problems)} problems; nothing was written")
        return len(problems)
    n = 0
    for name in ORDER:
        for kind, fname in (("candidate", "labels.jsonl"), ("diverged", "labels_diverged.jsonl")):
            path = out / name / fname
            new = rows[(name, kind)]
            if new or path.exists():
                write_jsonl(path, sorted(new.values(), key=lambda x: id_order(x["id"])))
            n += len(new)
    log(f"[read] collected {n} judgments into {paths.rel(out)}/ from transcripts read by {', '.join(sorted(models)) or 'nobody'}"
        + (f"; {len(missing)} pairs have no transcript and are left unread" if missing else ""))
    return 0


# ------------------------------------------------------------------ looking at transcripts


def _find(out: Path, sid: str) -> Path | None:
    for name in ORDER:
        f = out / name / "transcripts" / f"{sid}.json.gz"
        if f.is_file():
            return f
    return None


def first_request(sid: str, model: str, effort: str | None = "medium", max_tokens: int = 32000,
                  provider: str | None = "anthropic") -> int:
    """Print the first request of one pair as JSON: the settings, the tools, the rules and the packet. Nothing is sent."""
    for entry, text in packets.iter_packets(True):
        if entry["id"] == sid:
            guide = packets.GUIDE if entry["kind"] == "candidate" else packets.GUIDE_DIVERGED
            body = {**fixed_request(entry["kind"], model, effort, max_tokens, provider),
                    "messages": [{"role": "system", "content": guide}, {"role": "user", "content": text()}]}
            print(f"POST {endpoint()}")
            print(json.dumps(body, ensure_ascii=False, indent=1))
            return 0
    log(f"[read] there is no pair with the ID {sid}")
    return 1


def show(out: Path, sid: str, full: bool = False) -> int:
    """Print one transcript: every message in order, with the usage of each call."""
    f = _find(out, sid)
    if f is None:
        log(f"[read] no transcript for {sid} in {paths.rel(out)}/")
        return 1
    t = load(f)

    def clip(text, n: int = 30) -> str:
        lines = str(text if text is not None else "").split("\n")
        return "\n".join(lines if full or len(lines) <= n else lines[:n] + [f"    ... ({len(lines) - n} more lines; --full shows all)"])

    fixed = {k: v for k, v in t["request"].items() if k != "tools"}
    print(f"{t['id']} ({t['kind']})  {paths.rel(f)}  sha256 {sha256(f)}")
    print(f"endpoint: {t['endpoint']}\nrequest:  {json.dumps(fixed)} + {len(t['request']['tools'])} tools")
    print(f"outcome:  {json.dumps(t['outcome'], ensure_ascii=False) if full else t['outcome']['status']}\nusage:    {t['usage']}")
    call = 0
    for i, m in enumerate(t["messages"]):
        role = m.get("role")
        print(f"\n===== message {i + 1}: {role}" + (f" (reply to call {m.get('tool_call_id')})" if role == "tool" else ""))
        if role == "assistant":
            c = t["calls"][call] if call < len(t["calls"]) else {}
            r = c.get("response", {})
            u = r.get("usage") or {}
            print(f"      call {call + 1}: {c.get('messages_sent')} messages sent; model {r.get('model')}, provider "
                  f"{r.get('provider')}; prompt {u.get('prompt_tokens')}, completion {u.get('completion_tokens')}, "
                  f"cost {u.get('cost')}")
            call += 1
            if m.get("reasoning"):
                print("[reasoning]\n" + clip(m["reasoning"]))
            if m.get("content"):
                print(clip(m["content"]))
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                print(f"[tool call {tc.get('id')}] {fn.get('name')} {fn.get('arguments')}")
        else:
            print(clip(m.get("content")))
    return 0


def usage(out: Path) -> int:
    """Print calls, tokens and cost per codebase, over the accepted and the failed transcripts in out."""
    cols = ("pairs", "failed", "calls", "prompt_tokens", "cached_tokens", "cache_write_tokens", "completion_tokens",
            "reasoning_tokens", "cost")
    table = {}
    for name in ORDER:
        row = dict.fromkeys(cols, 0)
        for f in sorted((out / name / "transcripts").glob("*.json.gz")):
            t = load(f)
            row["failed" if ".failed-" in f.name else "pairs"] += 1
            for k in cols[2:]:
                row[k] += t["usage"].get(k) or 0
        table[name] = row
    table["total"] = {k: sum(table[n][k] for n in ORDER) for k in cols}
    print(f"{'':10}" + "".join(f"{c:>19}" for c in cols))
    for name, row in table.items():
        print(f"{name:10}" + "".join(f"{row[c]:>19,.2f}" if c == "cost" else f"{row[c]:>19,}" for c in cols))
    return 0
