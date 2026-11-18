"""The forge's in-memory store and domain operations.

One store holds every repository (its git objects, refs, labels, issues,
pull requests and comments) plus the global id counters. All timestamps come
from a logical clock — a fixed base plus a tick counter — so nothing the
forge does depends on the wall clock. Every mutation runs under one lock;
reads take the same lock, which keeps the concurrent workloads consistent.
"""

from __future__ import annotations

import base64
import difflib
import os
import threading
from functools import lru_cache
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import gitobj
from .scenario import VIEWER

DEFAULT_BRANCH = "main"


def _trial_now_base() -> int:
    raw = os.environ.get("MAKEFUN_TRIAL_NOW", "").strip()
    if raw:
        try:
            return int(float(raw))
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return int(parsed.timestamp())
        except ValueError:
            pass
    return 1_735_689_600  # 2025-01-01T00:00:00Z, the scenario's day one


@lru_cache(maxsize=16384)
def iso8601(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@lru_cache(maxsize=16384)
def node_id(type_name: str, db_id: int) -> str:
    return base64.b64encode(f"{type_name}:{db_id}".encode("ascii")).decode("ascii")


def decode_node_id(value: str) -> tuple[str, int] | None:
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True).decode("ascii")
        type_name, _, raw_id = decoded.partition(":")
        return type_name, int(raw_id)
    except Exception:
        return None


class ForgeError(Exception):
    """A domain error carrying the HTTP status the REST surface should use."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Issue:
    number: int
    db_id: int
    title: str
    body: str
    state: str = "open"  # open | closed
    labels: list[str] = field(default_factory=list)
    created_tick: int = 0
    updated_tick: int = 0
    closed_tick: int | None = None


@dataclass
class Pull:
    number: int
    db_id: int
    title: str
    body: str
    head_ref: str
    head_sha: str
    base_ref: str
    base_sha: str
    state: str = "open"  # open | closed
    merged: bool = False
    merge_commit_sha: str | None = None
    created_tick: int = 0
    updated_tick: int = 0
    closed_tick: int | None = None
    merged_tick: int | None = None


@dataclass
class Comment:
    db_id: int
    number: int  # the issue/pull number it hangs on
    body: str
    created_tick: int = 0
    updated_tick: int = 0


@dataclass
class Repo:
    db_id: int
    name: str
    owner: str
    description: str = ""
    private: bool = False
    default_branch: str = DEFAULT_BRANCH
    objects: dict[str, tuple[str, bytes]] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    labels: dict[str, dict] = field(default_factory=dict)
    issues: dict[int, Issue] = field(default_factory=dict)
    pulls: dict[int, Pull] = field(default_factory=dict)
    comments: dict[int, list[Comment]] = field(default_factory=dict)
    next_number: int = 1
    next_label_id: int = 1
    created_tick: int = 0
    updated_tick: int = 0
    # gitproto response cache (see gitproto._fingerprint); never serialized.
    resp_cache: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def branch_tip(self, branch: str) -> str | None:
        return self.refs.get(f"refs/heads/{branch}")

    def head_ref(self) -> str:
        return f"refs/heads/{self.default_branch}"


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.repos: dict[tuple[str, str], Repo] = {}
        self.repos_by_id: dict[int, Repo] = {}
        self.things_by_id: dict[int, tuple[str, Repo, int]] = {}  # db_id -> (kind, repo, number)
        self._repo_counter = 100_000
        self._thing_counter = 500_000
        self._tick = 0
        self._base = _trial_now_base()
        self.viewer = VIEWER

    # -- logical clock ------------------------------------------------------
    def tick(self) -> int:
        self._tick += 1
        return self._base + self._tick

    def tick_iso(self) -> str:
        return iso8601(self.tick())

    def iso_of(self, tick: int | None) -> str | None:
        if tick is None:
            return None
        return iso8601(tick)

    # -- repositories -------------------------------------------------------
    def create_repo(self, name: str, description: str = "", private: bool = False) -> Repo:
        with self.lock:
            key = (self.viewer["login"], name)
            if key in self.repos:
                raise ForgeError(422, "name already exists on this account")
            self._repo_counter += 1
            repo = Repo(db_id=self._repo_counter, name=name, owner=self.viewer["login"],
                        description=description or "", private=bool(private))
            repo.created_tick = repo.updated_tick = self.tick()
            self.repos[key] = repo
            self.repos_by_id[repo.db_id] = repo
            return repo

    def get_repo(self, owner: str, name: str) -> Repo | None:
        return self.repos.get((owner, name))

    def require_repo(self, owner: str, name: str) -> Repo:
        repo = self.get_repo(owner, name)
        if repo is None:
            raise ForgeError(404, "Not Found")
        return repo

    def delete_repo(self, owner: str, name: str) -> bool:
        with self.lock:
            repo = self.repos.pop((owner, name), None)
            if repo is None:
                return False
            self.repos_by_id.pop(repo.db_id, None)
            for issue in repo.issues.values():
                self.things_by_id.pop(issue.db_id, None)
            for pull in repo.pulls.values():
                self.things_by_id.pop(pull.db_id, None)
            return True

    def list_repos(self) -> list[Repo]:
        return [self.repos[key] for key in sorted(self.repos)]

    # -- labels ---------------------------------------------------------------
    def create_label(self, repo: Repo, name: str, color: str, description: str = "") -> dict:
        with self.lock:
            if name in repo.labels:
                raise ForgeError(422, "Validation Failed: label already exists")
            label = {"id": repo.next_label_id, "name": name, "color": color,
                     "description": description or "", "default": False}
            repo.next_label_id += 1
            repo.labels[name] = label
            return label

    # -- issues ---------------------------------------------------------------
    def _next_thing_id(self) -> int:
        self._thing_counter += 1
        return self._thing_counter

    def create_issue(self, repo: Repo, title: str, body: str = "", label_names: list[str] | None = None) -> Issue:
        with self.lock:
            if title is None or str(title) == "":
                raise ForgeError(422, "Validation Failed: title is required")
            labels = list(label_names or [])
            for name in labels:
                if name not in repo.labels:
                    raise ForgeError(422, f"Validation Failed: label '{name}' does not exist")
            number = repo.next_number
            repo.next_number += 1
            issue = Issue(number=number, db_id=self._next_thing_id(), title=str(title),
                          body=body or "", labels=labels)
            issue.created_tick = issue.updated_tick = self.tick()
            repo.issues[number] = issue
            self.things_by_id[issue.db_id] = ("issue", repo, number)
            repo.updated_tick = issue.updated_tick
            return issue

    def create_comment(self, repo: Repo, number: int, body: str) -> Comment:
        with self.lock:
            if number not in repo.issues and number not in repo.pulls:
                raise ForgeError(404, "Not Found")
            comment = Comment(db_id=self._next_thing_id(), number=number, body=body or "")
            comment.created_tick = comment.updated_tick = self.tick()
            repo.comments.setdefault(number, []).append(comment)
            self.things_by_id[comment.db_id] = ("comment", repo, comment.db_id)
            return comment

    # -- pull requests ----------------------------------------------------------
    def create_pull(self, repo: Repo, title: str, body: str, head_ref: str, base_ref: str) -> Pull:
        with self.lock:
            if title is None or str(title) == "":
                raise ForgeError(422, "Validation Failed: title is required")
            head_sha = repo.branch_tip(head_ref)
            base_sha = repo.branch_tip(base_ref)
            if head_sha is None:
                raise ForgeError(422, f"Validation Failed: head branch '{head_ref}' does not exist")
            if base_sha is None:
                raise ForgeError(422, f"Validation Failed: base branch '{base_ref}' does not exist")
            if head_ref == base_ref:
                raise ForgeError(422, "Validation Failed: head and base must differ")
            for existing in repo.pulls.values():
                if existing.state == "open" and existing.head_ref == head_ref and existing.base_ref == base_ref:
                    raise ForgeError(422, "Validation Failed: a pull request already exists for this branch combination")
            if head_sha == base_sha or gitobj.is_ancestor(repo.objects, head_sha, base_sha):
                raise ForgeError(422, "Validation Failed: no commits between base and head")
            number = repo.next_number
            repo.next_number += 1
            pull = Pull(number=number, db_id=self._next_thing_id(), title=str(title), body=body or "",
                        head_ref=head_ref, head_sha=head_sha, base_ref=base_ref, base_sha=base_sha)
            pull.created_tick = pull.updated_tick = self.tick()
            repo.pulls[number] = pull
            self.things_by_id[pull.db_id] = ("pull", repo, number)
            repo.updated_tick = pull.updated_tick
            return pull

    def pull_mergeable(self, repo: Repo, pull: Pull) -> bool | None:
        """True/False when the head still merges cleanly into the current base, else None."""
        head_sha = repo.branch_tip(pull.head_ref)
        base_sha = repo.branch_tip(pull.base_ref)
        if head_sha is None or base_sha is None:
            return None
        merged = self._merged_tree(repo, base_sha, head_sha)
        return merged is not None

    def _merged_tree(self, repo: Repo, base_sha: str, head_sha: str) -> dict[str, tuple[str, str]] | None:
        store = repo.objects
        base_commit = gitobj.parse_commit(store[base_sha][1])
        head_commit = gitobj.parse_commit(store[head_sha][1])
        ancestor = gitobj.merge_base(store, base_sha, head_sha)
        ancestor_flat = {}
        if ancestor is not None:
            ancestor_flat = gitobj.flatten_tree(store, gitobj.parse_commit(store[ancestor][1])["tree"])
        base_flat = gitobj.flatten_tree(store, base_commit["tree"])
        head_flat = gitobj.flatten_tree(store, head_commit["tree"])
        return gitobj.three_way_merge(ancestor_flat, base_flat, head_flat)

    def merge_pull(self, repo: Repo, pull: Pull, method: str = "squash") -> str:
        """Merge a pull request into its base branch; returns the merge commit sha."""
        with self.lock:
            if pull.state == "closed" and pull.merged:
                raise ForgeError(405, "Pull Request is not mergeable")
            if pull.state == "closed":
                raise ForgeError(405, "Pull Request is not mergeable")
            head_sha = repo.branch_tip(pull.head_ref)
            base_sha = repo.branch_tip(pull.base_ref)
            if head_sha is None or base_sha is None:
                raise ForgeError(405, "Pull Request is not mergeable")
            merged_flat = self._merged_tree(repo, base_sha, head_sha)
            if merged_flat is None:
                raise ForgeError(405, "Pull Request is not mergeable")
            tree_sha = gitobj.tree_from_flat(repo.objects, merged_flat)
            stamp = self.tick()
            identity = f"{self.viewer['name']} <{self.viewer['email']}> {stamp} +0000"
            message = f"{pull.title} (#{pull.number})"
            if pull.body:
                message += f"\n\n{pull.body}"
            commit_data = gitobj.build_commit(tree_sha, [base_sha], identity, identity, message)
            commit_sha = gitobj.hash_object("commit", commit_data)
            repo.objects[commit_sha] = ("commit", commit_data)
            repo.refs[f"refs/heads/{pull.base_ref}"] = commit_sha
            pull.state = "closed"
            pull.merged = True
            pull.merge_commit_sha = commit_sha
            pull.merged_tick = pull.closed_tick = pull.updated_tick = stamp
            repo.updated_tick = stamp
            return commit_sha

    # -- tree diffs ---------------------------------------------------------------
    def diff_files(self, repo: Repo, from_commit: str, to_commit: str) -> list[dict]:
        """File-level diff between two commits' trees (merge-base not applied)."""
        store = repo.objects
        from_flat = gitobj.flatten_tree(store, gitobj.parse_commit(store[from_commit][1])["tree"])
        to_flat = gitobj.flatten_tree(store, gitobj.parse_commit(store[to_commit][1])["tree"])
        return diff_flats(store, from_flat, to_flat)

    def pull_files(self, repo: Repo, pull: Pull) -> list[dict]:
        head_sha = repo.branch_tip(pull.head_ref) or pull.head_sha
        ancestor = gitobj.merge_base(repo.objects, pull.base_sha, head_sha)
        if ancestor is None:
            return self.diff_files(repo, pull.base_sha, head_sha)
        return self.diff_files(repo, ancestor, head_sha)

    # -- contents commits -----------------------------------------------------------
    def commit_files(self, repo: Repo, branch: str, changes: dict[str, bytes | None],
                     message: str, modes: dict[str, str] | None = None) -> tuple[str, bool]:
        """Apply file changes (path -> bytes, or None to delete) as one commit on a branch.

        Creates the branch from the default branch when it does not exist yet,
        or as an unborn branch on an empty repository. Returns (commit_sha, created_branch).
        """
        with self.lock:
            modes = modes or {}
            ref = f"refs/heads/{branch}"
            created_branch = False
            tip = repo.refs.get(ref)
            if tip is None:
                created_branch = True
                tip = repo.refs.get(repo.head_ref())
            flat: dict[str, tuple[str, str]] = {}
            parents: list[str] = []
            if tip is not None:
                parents = [tip]
                tree = gitobj.parse_commit(repo.objects[tip][1])["tree"]
                flat = gitobj.flatten_tree(repo.objects, tree)
            for path, content in changes.items():
                if content is None:
                    flat.pop(path, None)
                else:
                    blob_sha = gitobj.hash_object("blob", content)
                    repo.objects[blob_sha] = ("blob", content)
                    flat[path] = (modes.get(path, flat.get(path, ("100644", ""))[0]), blob_sha)
            tree_sha = gitobj.tree_from_flat(repo.objects, flat)
            stamp = self.tick()
            identity = f"{self.viewer['name']} <{self.viewer['email']}> {stamp} +0000"
            commit_data = gitobj.build_commit(tree_sha, parents, identity, identity, message)
            commit_sha = gitobj.hash_object("commit", commit_data)
            repo.objects[commit_sha] = ("commit", commit_data)
            repo.refs[ref] = commit_sha
            repo.updated_tick = stamp
            return commit_sha, created_branch


def diff_flats(store: dict[str, tuple[str, bytes]], from_flat: dict, to_flat: dict) -> list[dict]:
    files = []
    for path in sorted(set(from_flat) | set(to_flat)):
        before = from_flat.get(path)
        after = to_flat.get(path)
        if before == after:
            continue
        if before is None:
            status = "added"
            content = store[after[1]][1]
            additions, deletions = _line_counts(b"", content)
            sha = after[1]
        elif after is None:
            status = "removed"
            content = store[before[1]][1]
            additions, deletions = _line_counts(content, b"")
            sha = None
        else:
            status = "modified"
            additions, deletions = _line_counts(store[before[1]][1], store[after[1]][1])
            sha = after[1]
        files.append({
            "filename": path,
            "status": status,
            "additions": additions,
            "deletions": deletions,
            "changes": additions + deletions,
            "sha": sha,
        })
    return files


def _line_counts(before: bytes, after: bytes) -> tuple[int, int]:
    try:
        before_lines = before.decode("utf-8").splitlines()
        after_lines = after.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return 0, 0
    additions = deletions = 0
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            deletions += i2 - i1
        if tag in ("replace", "insert"):
            additions += j2 - j1
    return additions, deletions
