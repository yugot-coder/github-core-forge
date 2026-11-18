"""REST v3 surface: repositories, labels, issues, comments, pulls, contents,
branches, git refs and commit listing. Serializers aim at GitHub's shapes;
the suite's subset checks accept extra fields.
"""

from __future__ import annotations

import base64
import binascii
from urllib.parse import quote

from . import gitobj
from .store import ForgeError, Issue, Pull, Repo, Store, iso8601, node_id

API = "/api/v3"


class Ctx:
    def __init__(self, store: Store, base_url: str):
        self.store = store
        self.base = base_url.rstrip("/")


# --------------------------------------------------------------------------
# Serializers
# --------------------------------------------------------------------------

def user_json(store: Store) -> dict:
    viewer = store.viewer
    return {
        "login": viewer["login"],
        "id": viewer["id"],
        "node_id": node_id("User", viewer["id"]),
        "type": "User",
        "name": viewer["name"],
        "email": viewer["email"],
        "site_admin": False,
    }


def repo_json(store: Store, repo: Repo) -> dict:
    return {
        "id": repo.db_id,
        "node_id": node_id("Repository", repo.db_id),
        "name": repo.name,
        "full_name": repo.full_name,
        "owner": user_json(store),
        "private": repo.private,
        "description": repo.description,
        "html_url": f"https://github.com/{repo.full_name}",
        "url": f"{API}/repos/{repo.full_name}",
        "clone_url": f"https://github.com/{repo.full_name}.git",
        "default_branch": repo.default_branch,
        "has_issues": True,
        "has_projects": True,
        "has_wiki": True,
        "open_issues_count": sum(1 for i in repo.issues.values() if i.state == "open")
        + sum(1 for p in repo.pulls.values() if p.state == "open"),
        "forks_count": 0,
        "stargazers_count": 0,
        "watchers_count": 0,
        "size": 0,
        "language": None,
        "created_at": store.iso_of(repo.created_tick),
        "updated_at": store.iso_of(repo.updated_tick),
        "pushed_at": store.iso_of(repo.updated_tick),
    }


def label_json(label: dict) -> dict:
    return {
        "id": label["id"],
        "name": label["name"],
        "color": label["color"],
        "description": label["description"],
        "default": label["default"],
    }


def issue_json(store: Store, repo: Repo, issue: Issue) -> dict:
    return {
        "id": issue.db_id,
        "node_id": node_id("Issue", issue.db_id),
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "state": issue.state,
        "labels": [label_json(repo.labels[name]) for name in issue.labels if name in repo.labels],
        "user": user_json(store),
        "assignees": [],
        "comments": len(repo.comments.get(issue.number, [])),
        "created_at": store.iso_of(issue.created_tick),
        "updated_at": store.iso_of(issue.updated_tick),
        "closed_at": store.iso_of(issue.closed_tick),
        "url": f"{API}/repos/{repo.full_name}/issues/{issue.number}",
        "html_url": f"https://github.com/{repo.full_name}/issues/{issue.number}",
        "repository_url": f"{API}/repos/{repo.full_name}",
    }


def pull_as_issue_json(store: Store, repo: Repo, pull: Pull) -> dict:
    return {
        "id": pull.db_id,
        "node_id": node_id("PullRequest", pull.db_id),
        "number": pull.number,
        "title": pull.title,
        "body": pull.body,
        "state": pull.state,
        "labels": [],
        "user": user_json(store),
        "assignees": [],
        "comments": len(repo.comments.get(pull.number, [])),
        "created_at": store.iso_of(pull.created_tick),
        "updated_at": store.iso_of(pull.updated_tick),
        "closed_at": store.iso_of(pull.closed_tick),
        "url": f"{API}/repos/{repo.full_name}/issues/{pull.number}",
        "html_url": f"https://github.com/{repo.full_name}/pull/{pull.number}",
        "repository_url": f"{API}/repos/{repo.full_name}",
        "pull_request": {
            "url": f"{API}/repos/{repo.full_name}/pulls/{pull.number}",
            "html_url": f"https://github.com/{repo.full_name}/pull/{pull.number}",
            "diff_url": f"https://github.com/{repo.full_name}/pull/{pull.number}.diff",
            "patch_url": f"https://github.com/{repo.full_name}/pull/{pull.number}.patch",
        },
    }


def _ref_side(store: Store, repo: Repo, ref: str, sha: str) -> dict:
    return {
        "label": f"{repo.owner}:{ref}",
        "ref": ref,
        "sha": sha,
        "user": user_json(store),
        "repo": repo_json(store, repo),
    }


def pull_json(store: Store, repo: Repo, pull: Pull) -> dict:
    head_sha = repo.branch_tip(pull.head_ref) or pull.head_sha
    base_sha = repo.branch_tip(pull.base_ref) or pull.base_sha
    return {
        "id": pull.db_id,
        "node_id": node_id("PullRequest", pull.db_id),
        "number": pull.number,
        "state": pull.state,
        "title": pull.title,
        "body": pull.body,
        "user": user_json(store),
        "labels": [],
        "head": _ref_side(store, repo, pull.head_ref, head_sha),
        "base": _ref_side(store, repo, pull.base_ref, base_sha),
        "merged": pull.merged,
        "merged_at": store.iso_of(pull.merged_tick),
        "merge_commit_sha": pull.merge_commit_sha,
        "closed_at": store.iso_of(pull.closed_tick),
        "created_at": store.iso_of(pull.created_tick),
        "updated_at": store.iso_of(pull.updated_tick),
        "url": f"{API}/repos/{repo.full_name}/pulls/{pull.number}",
        "html_url": f"https://github.com/{repo.full_name}/pull/{pull.number}",
        "mergeable": None,
        "draft": False,
    }


def comment_json(store: Store, repo: Repo, comment) -> dict:
    return {
        "id": comment.db_id,
        "node_id": node_id("IssueComment", comment.db_id),
        "body": comment.body,
        "user": user_json(store),
        "created_at": store.iso_of(comment.created_tick),
        "updated_at": store.iso_of(comment.updated_tick),
        "url": f"{API}/repos/{repo.full_name}/issues/comments/{comment.db_id}",
        "html_url": f"https://github.com/{repo.full_name}/issues/{comment.number}#issuecomment-{comment.db_id}",
        "issue_url": f"{API}/repos/{repo.full_name}/issues/{comment.number}",
    }


def _ident_parts(line: str) -> tuple[str, str, str]:
    """'Name <email> 1735689600 +0000' -> (name, email, iso date)."""
    try:
        lt = line.index("<")
        gt = line.index(">", lt)
        name = line[:lt].strip()
        email = line[lt + 1:gt]
        stamp = int(line[gt + 1:].split()[0])
        return name, email, iso8601(stamp)
    except Exception:
        return line, "", iso8601(0)


def commit_json(store: Store, repo: Repo, sha: str) -> dict:
    parsed = gitobj.parse_commit(repo.objects[sha][1])
    a_name, a_email, a_date = _ident_parts(parsed.get("author", ""))
    c_name, c_email, c_date = _ident_parts(parsed.get("committer", ""))
    return {
        "sha": sha,
        "node_id": node_id("Commit", abs(hash(sha)) % 10**9),
        "commit": {
            "message": parsed["message"],
            "author": {"name": a_name, "email": a_email, "date": a_date},
            "committer": {"name": c_name, "email": c_email, "date": c_date},
            "tree": {"sha": parsed["tree"]},
            "comment_count": 0,
        },
        "author": user_json(store) if a_email == store.viewer["email"] else None,
        "committer": user_json(store) if c_email == store.viewer["email"] else None,
        "parents": [{"sha": parent} for parent in parsed["parents"]],
        "url": f"{API}/repos/{repo.full_name}/commits/{sha}",
        "html_url": f"https://github.com/{repo.full_name}/commit/{sha}",
    }


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------

def paginate(ctx: Ctx, path: str, items: list, query: dict) -> tuple[list, dict]:
    try:
        per_page = int(query.get("per_page", ["30"])[0])
    except ValueError:
        per_page = 30
    try:
        page = int(query.get("page", ["1"])[0])
    except ValueError:
        page = 1
    per_page = max(1, min(per_page, 100))
    page = max(1, page)
    total = len(items)
    start = (page - 1) * per_page
    sliced = items[start:start + per_page]
    last_page = max(1, (total + per_page - 1) // per_page)
    links = []
    if page < last_page:
        links.append(f'<{ctx.base}{path}?per_page={per_page}&page={page + 1}>; rel="next"')
        links.append(f'<{ctx.base}{path}?per_page={per_page}&page={last_page}>; rel="last"')
    if page > 1:
        links.append(f'<{ctx.base}{path}?per_page={per_page}&page={page - 1}>; rel="prev"')
        links.append(f'<{ctx.base}{path}?per_page={per_page}&page=1>; rel="first"')
    headers = {"Link": ", ".join(links)} if links else {}
    return sliced, headers


def _sort_key_desc(item) -> tuple:
    return (item.created_tick, item.number)


def _list_window(items: list, query: dict, default_state: str = "open") -> list:
    state = query.get("state", [default_state])[0]
    direction = query.get("direction", ["desc"])[0]
    if state != "all":
        items = [item for item in items if item.state == state]
    items.sort(key=_sort_key_desc, reverse=(direction != "asc"))
    return items


# --------------------------------------------------------------------------
# Handlers: each returns (status, payload, extra_headers); payload None = empty body
# --------------------------------------------------------------------------

def handle(ctx: Ctx, method: str, segments: list[str], query: dict, body) -> tuple[int, object, dict]:
    store = ctx.store
    if not segments:
        if method == "GET":
            return 200, {
                "current_user_url": f"{ctx.base}{API}/user",
                "repository_url": f"{ctx.base}{API}/repos/{{owner}}/{{repo}}",
                "issue_search_url": f"{ctx.base}{API}/search/issues",
            }, {}
        raise ForgeError(404, "Not Found")
    head = segments[0]
    if head == "meta" and method == "GET":
        return 200, {
            "installed_version": "3.10.0",
            "verifiable_password_authentication": True,
            "ssh_key_fingerprints": {},
            "hooks": [],
            "api": [f"{ctx.base}{API}"],
            "git": [],
            "packages": [],
            "pages": [],
            "importer": [],
            "actions": [],
            "dependabot": [],
        }, {}
    if head == "user":
        return _user(ctx, method, segments[1:], query, body)
    if head == "repos" and len(segments) >= 3:
        owner, name = segments[1], segments[2]
        return _repo(ctx, method, owner, name, segments[3:], query, body)
    raise ForgeError(404, "Not Found")


def _user(ctx: Ctx, method: str, rest: list[str], query: dict, body) -> tuple[int, object, dict]:
    store = ctx.store
    if not rest and method == "GET":
        return 200, user_json(store), {}
    if rest == ["repos"]:
        if method == "GET":
            repos = store.list_repos()
            repos.sort(key=lambda repo: repo.db_id)
            sliced, headers = paginate(ctx, f"{API}/user/repos", repos, query)
            return 200, [repo_json(store, repo) for repo in sliced], headers
        if method == "POST":
            data = body or {}
            name = data.get("name")
            if not name or not isinstance(name, str):
                raise ForgeError(422, "Validation Failed: name is required")
            repo = store.create_repo(name, data.get("description") or "", bool(data.get("private", False)))
            return 201, repo_json(store, repo), {}
    raise ForgeError(404, "Not Found")


def _repo(ctx: Ctx, method: str, owner: str, name: str, rest: list[str], query: dict, body) -> tuple[int, object, dict]:
    store = ctx.store
    if not rest:
        if method == "GET":
            return 200, repo_json(store, store.require_repo(owner, name)), {}
        if method == "DELETE":
            if not store.delete_repo(owner, name):
                raise ForgeError(404, "Not Found")
            return 204, None, {}
        if method == "PATCH":
            repo = store.require_repo(owner, name)
            data = body or {}
            if "description" in data:
                repo.description = data["description"] or ""
            if "private" in data:
                repo.private = bool(data["private"])
            repo.updated_tick = store.tick()
            return 200, repo_json(store, repo), {}
        raise ForgeError(404, "Not Found")
    repo = store.require_repo(owner, name)
    group = rest[0]
    tail = rest[1:]
    if group == "labels":
        return _labels(ctx, method, repo, tail, query, body)
    if group == "issues":
        return _issues(ctx, method, repo, tail, query, body)
    if group == "pulls":
        return _pulls(ctx, method, repo, tail, query, body)
    if group == "contents":
        return _contents(ctx, method, repo, tail, query, body)
    if group == "branches":
        return _branches(ctx, method, repo, tail, query)
    if group == "git":
        return _git_refs(ctx, method, repo, tail, query, body)
    if group == "commits" and not tail and method == "GET":
        return _commits(ctx, repo, query)
    raise ForgeError(404, "Not Found")


def _labels(ctx: Ctx, method: str, repo: Repo, tail: list[str], query: dict, body) -> tuple[int, object, dict]:
    store = ctx.store
    if not tail:
        if method == "GET":
            labels = [repo.labels[name] for name in sorted(repo.labels)]
            sliced, headers = paginate(ctx, f"{API}/repos/{repo.full_name}/labels", labels, query)
            return 200, [label_json(label) for label in sliced], headers
        if method == "POST":
            data = body or {}
            name = data.get("name")
            if not name:
                raise ForgeError(422, "Validation Failed: name is required")
            label = store.create_label(repo, str(name), str(data.get("color") or "ededed"), data.get("description") or "")
            return 201, label_json(label), {}
    if len(tail) == 1 and method == "GET":
        label = repo.labels.get(tail[0])
        if label is None:
            raise ForgeError(404, "Not Found")
        return 200, label_json(label), {}
    raise ForgeError(404, "Not Found")


def _issues(ctx: Ctx, method: str, repo: Repo, tail: list[str], query: dict, body) -> tuple[int, object, dict]:
    store = ctx.store
    if not tail:
        if method == "GET":
            items = _list_window(list(repo.issues.values()), query)
            sliced, headers = paginate(ctx, f"{API}/repos/{repo.full_name}/issues", items, query)
            return 200, [issue_json(store, repo, issue) for issue in sliced], headers
        if method == "POST":
            data = body or {}
            label_names: list[str] = []
            for entry in data.get("labels") or []:
                label_names.append(entry["name"] if isinstance(entry, dict) else str(entry))
            issue = store.create_issue(repo, data.get("title"), data.get("body") or "", label_names)
            return 201, issue_json(store, repo, issue), {}
    if tail and tail[0].isdigit():
        number = int(tail[0])
        if len(tail) == 1:
            if method == "GET":
                if number in repo.issues:
                    return 200, issue_json(store, repo, repo.issues[number]), {}
                if number in repo.pulls:
                    return 200, pull_as_issue_json(store, repo, repo.pulls[number]), {}
                raise ForgeError(404, "Not Found")
            if method == "PATCH":
                return _patch_issue(ctx, repo, number, body or {})
        if len(tail) == 2 and tail[1] == "comments":
            return _comments(ctx, method, repo, number, query, body)
    raise ForgeError(404, "Not Found")


def _patch_issue(ctx: Ctx, repo: Repo, number: int, data: dict) -> tuple[int, object, dict]:
    store = ctx.store
    target = repo.issues.get(number) or repo.pulls.get(number)
    if target is None:
        raise ForgeError(404, "Not Found")
    if "title" in data and data["title"] is not None:
        target.title = str(data["title"])
    if "body" in data:
        target.body = data["body"] or ""
    if isinstance(target, Issue) and "labels" in data and data["labels"] is not None:
        names = [entry["name"] if isinstance(entry, dict) else str(entry) for entry in data["labels"]]
        for name in names:
            if name not in repo.labels:
                raise ForgeError(422, f"Validation Failed: label '{name}' does not exist")
        target.labels = names
    if "state" in data and data["state"] in ("open", "closed"):
        if not (isinstance(target, Pull) and target.merged):
            target.state = data["state"]
            target.closed_tick = store.tick() if data["state"] == "closed" else None
    target.updated_tick = store.tick()
    if isinstance(target, Issue):
        return 200, issue_json(store, repo, target), {}
    return 200, pull_as_issue_json(store, repo, target), {}


def _comments(ctx: Ctx, method: str, repo: Repo, number: int, query: dict, body) -> tuple[int, object, dict]:
    store = ctx.store
    if number not in repo.issues and number not in repo.pulls:
        raise ForgeError(404, "Not Found")
    if method == "GET":
        comments = sorted(repo.comments.get(number, []), key=lambda c: c.db_id)
        sliced, headers = paginate(ctx, f"{API}/repos/{repo.full_name}/issues/{number}/comments", comments, query)
        return 200, [comment_json(store, repo, comment) for comment in sliced], headers
    if method == "POST":
        data = body or {}
        if data.get("body") is None:
            raise ForgeError(422, "Validation Failed: body is required")
        comment = store.create_comment(repo, number, str(data["body"]))
        return 201, comment_json(store, repo, comment), {}
    raise ForgeError(404, "Not Found")


def _pulls(ctx: Ctx, method: str, repo: Repo, tail: list[str], query: dict, body) -> tuple[int, object, dict]:
    store = ctx.store
    if not tail:
        if method == "GET":
            items = _list_window(list(repo.pulls.values()), query)
            sliced, headers = paginate(ctx, f"{API}/repos/{repo.full_name}/pulls", items, query)
            return 200, [pull_json(store, repo, pull) for pull in sliced], headers
        if method == "POST":
            data = body or {}
            head = str(data.get("head") or "")
            base = str(data.get("base") or repo.default_branch)
            if ":" in head:
                head = head.split(":", 1)[1]
            if ":" in base:
                base = base.split(":", 1)[1]
            pull = store.create_pull(repo, data.get("title"), data.get("body") or "", head, base)
            return 201, pull_json(store, repo, pull), {}
    if tail and tail[0].isdigit():
        number = int(tail[0])
        pull = repo.pulls.get(number)
        if len(tail) == 1 and method == "GET":
            if pull is None:
                raise ForgeError(404, "Not Found")
            return 200, pull_json(store, repo, pull), {}
        if len(tail) == 2 and tail[1] == "files" and method == "GET":
            if pull is None:
                raise ForgeError(404, "Not Found")
            files = store.pull_files(repo, pull)
            sliced, headers = paginate(ctx, f"{API}/repos/{repo.full_name}/pulls/{number}/files", files, query)
            return 200, sliced, headers
        if len(tail) == 2 and tail[1] == "merge" and method == "PUT":
            if pull is None:
                raise ForgeError(404, "Not Found")
            data = body or {}
            sha = store.merge_pull(repo, pull, str(data.get("merge_method") or "merge"))
            return 200, {"sha": sha, "merged": True, "message": "Pull Request successfully merged"}, {}
    raise ForgeError(404, "Not Found")


def _resolve_commit(repo: Repo, ref: str | None) -> str | None:
    if ref is None:
        return repo.branch_tip(repo.default_branch)
    tip = repo.branch_tip(ref)
    if tip is not None:
        return tip
    if ref in repo.objects and repo.objects[ref][0] == "commit":
        return ref
    tag_ref = repo.refs.get(f"refs/tags/{ref}")
    if tag_ref is not None:
        return gitobj.peel_to_commit(repo.objects, tag_ref)
    return None


def _flat_at(repo: Repo, commit_sha: str | None) -> dict[str, tuple[str, str]]:
    if commit_sha is None:
        return {}
    tree = gitobj.parse_commit(repo.objects[commit_sha][1])["tree"]
    return gitobj.flatten_tree(repo.objects, tree)


def _contents(ctx: Ctx, method: str, repo: Repo, tail: list[str], query: dict, body) -> tuple[int, object, dict]:
    store = ctx.store
    path = "/".join(tail)
    if method == "GET":
        ref = query.get("ref", [None])[0]
        commit_sha = _resolve_commit(repo, ref)
        if commit_sha is None:
            if path == "":
                return 200, [], {}
            raise ForgeError(404, "Not Found")
        return _get_contents(store, repo, commit_sha, path)
    if method == "PUT":
        data = body or {}
        if path == "":
            raise ForgeError(422, "Validation Failed: path is required")
        return _put_contents(store, repo, path, data)
    if method == "DELETE":
        data = body or {}
        if path == "":
            raise ForgeError(422, "Validation Failed: path is required")
        return _delete_contents(store, repo, path, data)
    raise ForgeError(404, "Not Found")


def _tree_entries_at(store: Store, repo: Repo, commit_sha: str, dir_path: str) -> list[dict] | None:
    tree_sha = gitobj.parse_commit(repo.objects[commit_sha][1])["tree"]
    if dir_path:
        flat_prefix = dir_path.rstrip("/") + "/"
        flat = gitobj.flatten_tree(repo.objects, tree_sha)
        # Walk to the subtree.
        node = tree_sha
        for segment in dir_path.split("/"):
            entry = next((e for e in gitobj.parse_tree(repo.objects[node][1]) if e[1] == segment), None)
            if entry is None or entry[0] != "40000":
                return None
            node = entry[2]
        tree_sha = node
        _ = flat_prefix
    entries = []
    for mode, name, sha in gitobj.parse_tree(repo.objects[tree_sha][1]):
        entry_path = f"{dir_path}/{name}" if dir_path else name
        if mode == "40000":
            entries.append({"name": name, "path": entry_path, "sha": sha, "size": 0, "type": "dir"})
        else:
            size = len(repo.objects[sha][1])
            entries.append({"name": name, "path": entry_path, "sha": sha, "size": size, "type": "file"})
    return entries


def _get_contents(store: Store, repo: Repo, commit_sha: str, path: str) -> tuple[int, object, dict]:
    flat = _flat_at(repo, commit_sha)
    if path == "":
        return 200, _tree_entries_at(store, repo, commit_sha, ""), {}
    if path in flat:
        mode, blob_sha = flat[path]
        data = repo.objects[blob_sha][1]
        name = path.split("/")[-1]
        return 200, {
            "type": "file",
            "name": name,
            "path": path,
            "sha": blob_sha,
            "size": len(data),
            "encoding": "base64",
            "content": base64.b64encode(data).decode("ascii"),
            "url": f"{API}/repos/{repo.full_name}/contents/{path}",
            "html_url": f"https://github.com/{repo.full_name}/blob/{repo.default_branch}/{path}",
            "download_url": f"https://raw.githubusercontent.com/{repo.full_name}/{repo.default_branch}/{path}",
        }, {}
    prefix = path.rstrip("/") + "/"
    if any(candidate.startswith(prefix) for candidate in flat):
        entries = _tree_entries_at(store, repo, commit_sha, path)
        if entries is not None:
            return 200, entries, {}
    raise ForgeError(404, "Not Found")


def _effective_flat(repo: Repo, branch: str) -> tuple[dict[str, tuple[str, str]], str | None]:
    """The tree a contents write would build on: the branch tip, or the
    default branch tip when the branch is about to be created from it."""
    tip = repo.branch_tip(branch)
    if tip is None:
        tip = repo.branch_tip(repo.default_branch)
    return _flat_at(repo, tip), tip


def _put_contents(store: Store, repo: Repo, path: str, data: dict) -> tuple[int, object, dict]:
    raw_content = data.get("content")
    if raw_content is None:
        raise ForgeError(422, "Validation Failed: content is required")
    try:
        content = base64.b64decode("".join(str(raw_content).split()), validate=True)
    except (binascii.Error, ValueError):
        raise ForgeError(422, "Validation Failed: content is not valid base64")
    branch = str(data.get("branch") or repo.default_branch)
    flat, _tip = _effective_flat(repo, branch)
    existing = flat.get(path)
    supplied_sha = data.get("sha")
    if existing is not None:
        if supplied_sha is None:
            raise ForgeError(422, "Validation Failed: sha is required when updating a file")
        if supplied_sha != existing[1]:
            raise ForgeError(409, "Conflict: the sha does not match the current file")
        status = 200
    else:
        if supplied_sha is not None:
            raise ForgeError(422, "Validation Failed: sha supplied for a file that does not exist")
        status = 201
    message = str(data.get("message") or f"Update {path}")
    commit_sha, _created = store.commit_files(repo, branch, {path: content}, message)
    blob_sha = gitobj.hash_object("blob", content)
    name = path.split("/")[-1]
    return status, {
        "content": {
            "type": "file",
            "encoding": "base64",
            "name": name,
            "path": path,
            "sha": blob_sha,
            "size": len(content),
            "url": f"{API}/repos/{repo.full_name}/contents/{path}",
            "html_url": f"https://github.com/{repo.full_name}/blob/{branch}/{path}",
            "download_url": f"https://raw.githubusercontent.com/{repo.full_name}/{branch}/{path}",
        },
        "commit": {"sha": commit_sha, "message": message},
    }, {}


def _delete_contents(store: Store, repo: Repo, path: str, data: dict) -> tuple[int, object, dict]:
    branch = str(data.get("branch") or repo.default_branch)
    flat, _tip = _effective_flat(repo, branch)
    existing = flat.get(path)
    if existing is None:
        raise ForgeError(404, "Not Found")
    supplied_sha = data.get("sha")
    if supplied_sha is not None and supplied_sha != existing[1]:
        raise ForgeError(409, "Conflict: the sha does not match the current file")
    message = str(data.get("message") or f"Delete {path}")
    commit_sha, _created = store.commit_files(repo, branch, {path: None}, message)
    return 200, {"content": None, "commit": {"sha": commit_sha, "message": message}}, {}


def _branches(ctx: Ctx, method: str, repo: Repo, tail: list[str], query: dict) -> tuple[int, object, dict]:
    if method != "GET":
        raise ForgeError(404, "Not Found")
    if not tail:
        branches = []
        for ref in sorted(repo.refs):
            if ref.startswith("refs/heads/"):
                branches.append({"name": ref[len("refs/heads/"):], "commit": {"sha": repo.refs[ref]}, "protected": False})
        sliced, headers = paginate(ctx, f"{API}/repos/{repo.full_name}/branches", branches, query)
        return 200, sliced, headers
    name = "/".join(tail)
    tip = repo.branch_tip(name)
    if tip is None:
        raise ForgeError(404, "Not Found")
    return 200, {
        "name": name,
        "commit": commit_json(ctx.store, repo, tip),
        "protected": False,
    }, {}


def _git_refs(ctx: Ctx, method: str, repo: Repo, tail: list[str], query: dict, body) -> tuple[int, object, dict]:
    store = ctx.store
    if tail and tail[0] == "refs":
        sub = tail[1:]
        if not sub:
            if method == "GET":
                return 200, [_ref_json(repo, ref) for ref in sorted(repo.refs)], {}
            if method == "POST":
                data = body or {}
                ref = str(data.get("ref") or "")
                sha = str(data.get("sha") or "")
                if not ref.startswith("refs/"):
                    raise ForgeError(422, "Validation Failed: ref must start with refs/")
                if sha not in repo.objects:
                    raise ForgeError(422, "Validation Failed: object does not exist")
                if ref in repo.refs:
                    raise ForgeError(422, "Validation Failed: reference already exists")
                with store.lock:
                    repo.refs[ref] = sha
                return 201, _ref_json(repo, ref), {}
            raise ForgeError(404, "Not Found")
        ref = "refs/" + "/".join(sub)
        if method == "GET":
            if ref in repo.refs:
                return 200, _ref_json(repo, ref), {}
            prefix = ref + "/"
            matching = [_ref_json(repo, name) for name in sorted(repo.refs) if name.startswith(prefix)]
            if matching:
                return 200, matching, {}
            raise ForgeError(404, "Not Found")
    raise ForgeError(404, "Not Found")


def _ref_json(repo: Repo, ref: str) -> dict:
    sha = repo.refs[ref]
    type_name = repo.objects.get(sha, ("commit", b""))[0]
    return {
        "ref": ref,
        "node_id": node_id("Ref", abs(hash(ref)) % 10**9),
        "url": f"{API}/repos/{repo.full_name}/git/{ref}",
        "object": {"sha": sha, "type": type_name, "url": f"{API}/repos/{repo.full_name}/git/{type_name}s/{sha}"},
    }


def _commits(ctx: Ctx, repo: Repo, query: dict) -> tuple[int, object, dict]:
    store = ctx.store
    sha_param = query.get("sha", [None])[0]
    start = _resolve_commit(repo, sha_param)
    if start is None:
        return 200, [], {}
    distances = gitobj.commit_ancestors(repo.objects, start)
    ordered = sorted(distances.items(), key=lambda item: (item[1], item[0]))
    commits = [commit_json(store, repo, sha) for sha, _dist in ordered]
    sliced, headers = paginate(ctx, f"{API}/repos/{repo.full_name}/commits", commits, query)
    return 200, sliced, headers
