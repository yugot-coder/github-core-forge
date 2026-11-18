"""GraphQL surface: a small parser for the query language the suite sends
(operations, variables, aliases, inline fragments, introspection) and an
executor over the same store the REST surface uses.
"""

from __future__ import annotations

import base64

from .store import ForgeError, Repo, Store, node_id

# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

class GqlError(Exception):
    pass


_PUNCT = set("!$():=@[]{}|")


def tokenize(text: str) -> list[tuple[str, object]]:
    tokens: list[tuple[str, object]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n,\ufeff":
            i += 1
            continue
        if ch == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text.startswith("...", i):
            tokens.append(("ELLIPSIS", "..."))
            i += 3
            continue
        if ch in _PUNCT:
            tokens.append(("PUNCT", ch))
            i += 1
            continue
        if ch == '"':
            if text.startswith('"""', i):
                end = text.find('"""', i + 3)
                if end < 0:
                    raise GqlError("unterminated block string")
                tokens.append(("STRING", text[i + 3:end]))
                i = end + 3
                continue
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    esc = text[j + 1]
                    mapping = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
                    if esc == "u":
                        buf.append(chr(int(text[j + 2:j + 6], 16)))
                        j += 6
                    else:
                        buf.append(mapping.get(esc, esc))
                        j += 2
                else:
                    buf.append(text[j])
                    j += 1
            if j >= n:
                raise GqlError("unterminated string")
            tokens.append(("STRING", "".join(buf)))
            i = j + 1
            continue
        if ch.isdigit() or (ch == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i
            if text[j] == "-":
                j += 1
            while j < n and text[j].isdigit():
                j += 1
            is_float = False
            if j < n and text[j] == ".":
                is_float = True
                j += 1
                while j < n and text[j].isdigit():
                    j += 1
            if j < n and text[j] in "eE":
                is_float = True
                j += 1
                if j < n and text[j] in "+-":
                    j += 1
                while j < n and text[j].isdigit():
                    j += 1
            raw = text[i:j]
            tokens.append(("FLOAT" if is_float else "INT", float(raw) if is_float else int(raw)))
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(("NAME", text[i:j]))
            i = j
            continue
        raise GqlError(f"unexpected character {ch!r}")
    tokens.append(("EOF", None))
    return tokens


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

class Parser:
    def __init__(self, text: str):
        self.tokens = tokenize(text)
        self.pos = 0

    def peek(self) -> tuple[str, object]:
        return self.tokens[self.pos]

    def next(self) -> tuple[str, object]:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def expect_punct(self, ch: str) -> None:
        kind, value = self.next()
        if kind != "PUNCT" or value != ch:
            raise GqlError(f"expected {ch!r}, got {value!r}")

    def skip_punct(self, ch: str) -> bool:
        kind, value = self.peek()
        if kind == "PUNCT" and value == ch:
            self.pos += 1
            return True
        return False

    def expect_name(self) -> str:
        kind, value = self.next()
        if kind != "NAME":
            raise GqlError(f"expected a name, got {value!r}")
        return str(value)

    def parse_document(self) -> dict:
        operations = []
        fragments = {}
        while self.peek()[0] != "EOF":
            kind, value = self.peek()
            if kind == "PUNCT" and value == "{":
                operations.append({"op": "query", "name": None, "selections": self.parse_selection_set()})
            elif kind == "NAME" and value in ("query", "mutation", "subscription"):
                self.next()
                name = None
                if self.peek()[0] == "NAME":
                    name = self.next()[1]
                if self.peek() == ("PUNCT", "("):
                    self.parse_variable_definitions()
                self.parse_directives()
                operations.append({"op": value, "name": name, "selections": self.parse_selection_set()})
            elif kind == "NAME" and value == "fragment":
                self.next()
                frag_name = self.expect_name()
                if self.expect_name() != "on":
                    raise GqlError("fragment missing 'on'")
                type_condition = self.expect_name()
                self.parse_directives()
                fragments[frag_name] = {"type": type_condition, "selections": self.parse_selection_set()}
            else:
                raise GqlError(f"unexpected token {value!r}")
        return {"operations": operations, "fragments": fragments}

    def parse_variable_definitions(self) -> None:
        self.expect_punct("(")
        while self.peek() != ("PUNCT", ")"):
            self.expect_punct("$")
            self.expect_name()
            self.expect_punct(":")
            self.parse_type()
            if self.skip_punct("="):
                self.parse_value()
            self.parse_directives()
        self.expect_punct(")")

    def parse_type(self) -> None:
        if self.skip_punct("["):
            self.parse_type()
            self.expect_punct("]")
        else:
            self.expect_name()
        self.skip_punct("!")

    def parse_directives(self) -> None:
        while self.peek() == ("PUNCT", "@"):
            self.next()
            self.expect_name()
            if self.peek() == ("PUNCT", "("):
                self.parse_arguments()

    def parse_arguments(self) -> dict:
        args = {}
        self.expect_punct("(")
        while self.peek() != ("PUNCT", ")"):
            name = self.expect_name()
            self.expect_punct(":")
            args[name] = self.parse_value()
        self.expect_punct(")")
        return args

    def parse_value(self):
        kind, value = self.next()
        if kind == "PUNCT" and value == "$":
            return ("var", self.expect_name())
        if kind in ("INT", "FLOAT", "STRING"):
            return value
        if kind == "NAME":
            if value == "true":
                return True
            if value == "false":
                return False
            if value == "null":
                return None
            return ("enum", value)
        if kind == "PUNCT" and value == "[":
            items = []
            while self.peek() != ("PUNCT", "]"):
                items.append(self.parse_value())
            self.expect_punct("]")
            return items
        if kind == "PUNCT" and value == "{":
            obj = {}
            while self.peek() != ("PUNCT", "}"):
                name = self.expect_name()
                self.expect_punct(":")
                obj[name] = self.parse_value()
            self.expect_punct("}")
            return obj
        raise GqlError(f"unexpected value token {value!r}")

    def parse_selection_set(self) -> list[dict]:
        self.expect_punct("{")
        selections = []
        while self.peek() != ("PUNCT", "}"):
            selections.append(self.parse_selection())
        self.expect_punct("}")
        return selections

    def parse_selection(self) -> dict:
        if self.peek()[0] == "ELLIPSIS":
            self.next()
            if self.peek()[0] == "NAME" and self.peek()[1] == "on":
                self.next()
                type_condition = self.expect_name()
                self.parse_directives()
                return {"kind": "inline", "type": type_condition, "selections": self.parse_selection_set()}
            name = self.expect_name()
            self.parse_directives()
            return {"kind": "spread", "name": name}
        first = self.expect_name()
        alias = None
        name = first
        if self.skip_punct(":"):
            alias = first
            name = self.expect_name()
        args = {}
        if self.peek() == ("PUNCT", "("):
            args = self.parse_arguments()
        self.parse_directives()
        selections = []
        if self.peek() == ("PUNCT", "{"):
            selections = self.parse_selection_set()
        return {"kind": "field", "name": name, "alias": alias, "args": args, "selections": selections}


def resolve_value(value, variables: dict):
    if isinstance(value, tuple):
        if value[0] == "var":
            return variables.get(value[1])
        if value[0] == "enum":
            return value[1]
    if isinstance(value, list):
        return [resolve_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: resolve_value(item, variables) for key, item in value.items()}
    return value


# --------------------------------------------------------------------------
# Schema description (for introspection)
# --------------------------------------------------------------------------

TYPE_FIELDS: dict[str, list[str]] = {
    "Query": ["viewer", "repository", "node", "nodes", "search", "rateLimit"],
    "Mutation": ["createIssue", "closeIssue", "createPullRequest", "mergePullRequest", "addComment"],
    "User": ["id", "databaseId", "login", "name", "email", "url"],
    "Repository": ["id", "databaseId", "name", "nameWithOwner", "owner", "defaultBranchRef",
                   "viewerPermission", "hasIssuesEnabled", "issue", "issues", "pullRequests",
                   "pullRequest", "description", "isPrivate", "createdAt", "updatedAt", "url"],
    "Issue": ["id", "databaseId", "number", "title", "state", "body", "author", "labels",
              "comments", "createdAt", "updatedAt", "closedAt", "repository", "url"],
    "PullRequest": ["id", "databaseId", "number", "title", "body", "state", "merged", "mergeable",
                    "headRefName", "baseRefName", "headRefOid", "baseRefOid", "files", "author",
                    "createdAt", "updatedAt", "closedAt", "mergedAt", "repository", "url"],
    "IssueComment": ["id", "databaseId", "body", "author", "createdAt", "updatedAt", "url"],
    "Ref": ["id", "name", "prefix", "target"],
    "Label": ["id", "name", "color", "description"],
    "PageInfo": ["hasNextPage", "hasPreviousPage", "startCursor", "endCursor"],
    "IssueConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "LabelConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "PullRequestConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "PullRequestFileConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "PullRequestFile": ["path", "additions", "deletions", "changeType"],
    "CreateIssuePayload": ["issue", "clientMutationId"],
    "CloseIssuePayload": ["issue", "clientMutationId"],
    "CreatePullRequestPayload": ["pullRequest", "clientMutationId"],
    "MergePullRequestPayload": ["pullRequest", "clientMutationId"],
    "AddCommentPayload": ["commentEdge", "subject", "clientMutationId"],
    "IssueCommentEdge": ["node", "cursor"],
    "__Type": ["name", "kind", "description", "fields", "enumValues", "possibleTypes", "inputFields", "interfaces", "ofType"],
    "__Field": ["name", "description", "type", "args", "isDeprecated", "deprecationReason"],
    "__EnumValue": ["name", "description", "isDeprecated", "deprecationReason"],
    "__Schema": ["queryType", "mutationType", "types", "directives"],
}

ENUM_VALUES: dict[str, list[str]] = {
    "SearchType": ["ISSUE", "REPOSITORY", "USER"],
    "IssueState": ["OPEN", "CLOSED"],
    "PullRequestState": ["OPEN", "CLOSED", "MERGED"],
    "MergeableState": ["MERGEABLE", "CONFLICTING", "UNKNOWN"],
}


# --------------------------------------------------------------------------
# Executor
# --------------------------------------------------------------------------

class Executor:
    def __init__(self, store: Store):
        self.store = store
        self.errors: list[dict] = []
        self.fragments: dict = {}
        self.variables: dict = {}

    # -- entry point ---------------------------------------------------------
    def execute(self, document: dict, variables: dict | None, operation_name: str | None) -> dict:
        self.variables = variables or {}
        self.fragments = document["fragments"]
        operations = document["operations"]
        operation = None
        if operation_name:
            operation = next((op for op in operations if op["name"] == operation_name), None)
        if operation is None and operations:
            operation = operations[0]
        if operation is None:
            return {"errors": [{"message": "no operation to execute"}]}
        root_type = "Mutation" if operation["op"] == "mutation" else "Query"
        data = self.exec_set(root_type, None, operation["selections"])
        result: dict = {"data": data}
        if self.errors:
            result["errors"] = self.errors
        return result

    # -- selection execution ---------------------------------------------------
    def exec_set(self, type_name: str, obj, selections: list[dict]) -> dict:
        out: dict = {}
        for selection in selections:
            if selection["kind"] == "field":
                key = selection["alias"] or selection["name"]
                out[key] = self.exec_field(type_name, obj, selection)
            elif selection["kind"] == "inline":
                if selection["type"] == type_name:
                    out.update(self.exec_set(type_name, obj, selection["selections"]))
            elif selection["kind"] == "spread":
                fragment = self.fragments.get(selection["name"])
                if fragment and fragment["type"] == type_name:
                    out.update(self.exec_set(type_name, obj, fragment["selections"]))
        return out

    def exec_field(self, type_name: str, obj, selection: dict):
        name = selection["name"]
        args = {key: resolve_value(value, self.variables) for key, value in selection["args"].items()}
        subs = selection["selections"]
        if name == "__type" and type_name in ("Query", "Mutation"):
            return self.complete("__Type", args.get("name"), subs)
        if name == "__schema":
            return self.complete("__Schema", None, subs)
        if name == "__typename":
            return type_name
        resolver = RESOLVERS.get((type_name, name))
        if resolver is None:
            self.errors.append({"message": f"Field '{name}' doesn't exist on type '{type_name}'"})
            return None
        value = resolver(self, obj, args)
        return self.complete_value(value, subs)

    def complete(self, type_name: str, payload, subs: list[dict]):
        if payload is None:
            return None
        if not subs:
            return payload
        return self.exec_set(type_name, payload, subs)

    def complete_value(self, value, subs: list[dict]):
        """Resolver results are (type_name, payload) tuples, lists, or scalars."""
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str) and value[0] in ALL_TYPES:
            type_name, payload = value
            if payload is None:
                return None
            if not subs:
                return payload
            return self.exec_set(type_name, payload, subs)
        if isinstance(value, list):
            return [self.complete_value(item, subs) for item in value]
        return value


ALL_TYPES = set(TYPE_FIELDS) | set(ENUM_VALUES) | {"GitObject"}


def _cursor_for(number: int) -> str:
    return base64.b64encode(f"cursor:{number}".encode("ascii")).decode("ascii")


def _number_from_cursor(cursor: str | None) -> int | None:
    if not cursor:
        return None
    try:
        decoded = base64.b64decode(cursor.encode("ascii")).decode("ascii")
        if decoded.startswith("cursor:"):
            return int(decoded[len("cursor:"):])
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------
# Resolvers. Signature: (executor, obj, args) -> scalar | (type, payload) | list
# --------------------------------------------------------------------------

def _viewer_obj(ex: Executor):
    return ("User", ex.store.viewer)


def q_viewer(ex: Executor, obj, args):
    return _viewer_obj(ex)


def q_repository(ex: Executor, obj, args):
    repo = ex.store.get_repo(str(args.get("owner") or ""), str(args.get("name") or ""))
    return ("Repository", repo)


def q_node(ex: Executor, obj, args):
    from .store import decode_node_id
    decoded = decode_node_id(str(args.get("id") or ""))
    if decoded is None:
        return None
    type_name, db_id = decoded
    if type_name == "Repository":
        repo = ex.store.repos_by_id.get(db_id)
        return ("Repository", repo)
    entry = ex.store.things_by_id.get(db_id)
    if entry is None:
        return None
    kind, repo, number = entry
    if kind == "issue":
        return ("Issue", (repo, repo.issues[number]))
    if kind == "pull":
        return ("PullRequest", (repo, repo.pulls[number]))
    if kind == "comment":
        for comments in repo.comments.values():
            for comment in comments:
                if comment.db_id == db_id:
                    return ("IssueComment", (repo, comment))
    return None


def user_field(field: str):
    def resolve(ex: Executor, obj, args):
        viewer = obj
        if field == "id":
            return node_id("User", viewer["id"])
        if field == "databaseId":
            return viewer["id"]
        return viewer.get(field)
    return resolve


def repo_field(field: str):
    def resolve(ex: Executor, obj: Repo, args):
        store = ex.store
        if field == "id":
            return node_id("Repository", obj.db_id)
        if field == "databaseId":
            return obj.db_id
        if field == "name":
            return obj.name
        if field == "nameWithOwner":
            return obj.full_name
        if field == "owner":
            return _viewer_obj(ex)
        if field == "defaultBranchRef":
            tip = obj.branch_tip(obj.default_branch)
            if tip is None:
                return None
            return ("Ref", (obj, obj.default_branch, tip))
        if field == "viewerPermission":
            return "ADMIN"
        if field == "hasIssuesEnabled":
            return True
        if field == "description":
            return obj.description
        if field == "isPrivate":
            return obj.private
        if field == "createdAt":
            return store.iso_of(obj.created_tick)
        if field == "updatedAt":
            return store.iso_of(obj.updated_tick)
        if field == "url":
            return f"https://github.com/{obj.full_name}"
        return None
    return resolve


def repo_issue(ex: Executor, obj: Repo, args):
    number = args.get("number")
    issue = obj.issues.get(number) if isinstance(number, int) else None
    if issue is None:
        return None
    return ("Issue", (obj, issue))


def _connection(items: list, first, after) -> tuple[list, bool, str | None]:
    start = 0
    after_number = _number_from_cursor(after)
    if after_number is not None:
        for index, (_repo, thing) in enumerate(items):
            if thing.number == after_number:
                start = index + 1
                break
    if isinstance(first, int) and first >= 0:
        page = items[start:start + first]
    else:
        page = items[start:]
    has_next = start + len(page) < len(items)
    end_cursor = _cursor_for(page[-1][1].number) if page else None
    return page, has_next, end_cursor


def repo_issues(ex: Executor, obj: Repo, args):
    states = args.get("states")
    wanted = None
    if isinstance(states, list):
        wanted = {str(state).lower() for state in states}
    items = [issue for issue in obj.issues.values() if wanted is None or issue.state in wanted]
    order_by = args.get("orderBy") or {}
    direction = str(order_by.get("direction") or "DESC").upper()
    items.sort(key=lambda issue: (issue.created_tick, issue.number), reverse=(direction != "ASC"))
    pairs = [(obj, issue) for issue in items]
    page, has_next, end_cursor = _connection(pairs, args.get("first"), args.get("after"))
    payload = {
        "nodes": [("Issue", pair) for pair in page],
        "pageInfo": (has_next, end_cursor),
        "totalCount": len(items),
    }
    return ("IssueConnection", payload)


def repo_pull_request(ex: Executor, obj: Repo, args):
    number = args.get("number")
    pull = obj.pulls.get(number) if isinstance(number, int) else None
    if pull is None:
        return None
    return ("PullRequest", (obj, pull))


def repo_pull_requests(ex: Executor, obj: Repo, args):
    states = args.get("states")
    wanted = None
    if isinstance(states, list):
        wanted = {str(state).lower() for state in states}
    items = []
    for pull in obj.pulls.values():
        state = "merged" if pull.merged else pull.state
        if wanted is None or state in wanted or pull.state in wanted:
            items.append(pull)
    items.sort(key=lambda pull: (pull.created_tick, pull.number), reverse=True)
    pairs = [(obj, pull) for pull in items]
    page, has_next, end_cursor = _connection(pairs, args.get("first"), args.get("after"))
    payload = {
        "nodes": [("PullRequest", pair) for pair in page],
        "pageInfo": (has_next, end_cursor),
        "totalCount": len(items),
    }
    return ("PullRequestConnection", payload)


def issue_field(field: str):
    def resolve(ex: Executor, obj, args):
        repo, issue = obj
        store = ex.store
        if field == "id":
            return node_id("Issue", issue.db_id)
        if field == "databaseId":
            return issue.db_id
        if field == "number":
            return issue.number
        if field == "title":
            return issue.title
        if field == "state":
            return issue.state.upper()
        if field == "body":
            return issue.body
        if field == "author":
            return _viewer_obj(ex)
        if field == "repository":
            return ("Repository", repo)
        if field == "labels":
            nodes = [("Label", repo.labels[name]) for name in issue.labels if name in repo.labels]
            return ("LabelConnection", {"nodes": nodes, "pageInfo": (False, None), "totalCount": len(nodes)})
        if field == "createdAt":
            return store.iso_of(issue.created_tick)
        if field == "updatedAt":
            return store.iso_of(issue.updated_tick)
        if field == "closedAt":
            return store.iso_of(issue.closed_tick)
        if field == "url":
            return f"https://github.com/{repo.full_name}/issues/{issue.number}"
        return None
    return resolve


def pull_field(field: str):
    def resolve(ex: Executor, obj, args):
        repo, pull = obj
        store = ex.store
        if field == "id":
            return node_id("PullRequest", pull.db_id)
        if field == "databaseId":
            return pull.db_id
        if field == "number":
            return pull.number
        if field == "title":
            return pull.title
        if field == "body":
            return pull.body
        if field == "state":
            if pull.merged:
                return "MERGED"
            return pull.state.upper()
        if field == "merged":
            return pull.merged
        if field == "mergeable":
            if pull.merged or pull.state == "closed":
                return "UNKNOWN"
            mergeable = store.pull_mergeable(repo, pull)
            if mergeable is None:
                return "UNKNOWN"
            return "MERGEABLE" if mergeable else "CONFLICTING"
        if field == "headRefName":
            return pull.head_ref
        if field == "baseRefName":
            return pull.base_ref
        if field == "headRefOid":
            return repo.branch_tip(pull.head_ref) or pull.head_sha
        if field == "baseRefOid":
            return repo.branch_tip(pull.base_ref) or pull.base_sha
        if field == "author":
            return _viewer_obj(ex)
        if field == "repository":
            return ("Repository", repo)
        if field == "files":
            files = store.pull_files(repo, pull)
            first = args.get("first")
            if isinstance(first, int):
                files = files[:first]
            payload = {
                "nodes": [("PullRequestFile", file) for file in files],
                "pageInfo": (False, None),
                "totalCount": len(files),
            }
            return ("PullRequestFileConnection", payload)
        if field == "createdAt":
            return store.iso_of(pull.created_tick)
        if field == "updatedAt":
            return store.iso_of(pull.updated_tick)
        if field == "closedAt":
            return store.iso_of(pull.closed_tick)
        if field == "mergedAt":
            return store.iso_of(pull.merged_tick)
        if field == "url":
            return f"https://github.com/{repo.full_name}/pull/{pull.number}"
        return None
    return resolve


def comment_field(field: str):
    def resolve(ex: Executor, obj, args):
        repo, comment = obj
        store = ex.store
        if field == "id":
            return node_id("IssueComment", comment.db_id)
        if field == "databaseId":
            return comment.db_id
        if field == "body":
            return comment.body
        if field == "author":
            return _viewer_obj(ex)
        if field == "createdAt":
            return store.iso_of(comment.created_tick)
        if field == "updatedAt":
            return store.iso_of(comment.updated_tick)
        if field == "url":
            return f"https://github.com/{repo.full_name}/issues/{comment.number}#issuecomment-{comment.db_id}"
        return None
    return resolve


def connection_field(field: str):
    def resolve(ex: Executor, obj, args):
        if field == "nodes":
            return obj["nodes"]
        if field == "pageInfo":
            has_next, end_cursor = obj["pageInfo"]
            return ("PageInfo", {"hasNextPage": has_next, "hasPreviousPage": False,
                                 "startCursor": None, "endCursor": end_cursor})
        if field == "totalCount":
            return obj["totalCount"]
        if field == "edges":
            return []
        return None
    return resolve


def pageinfo_field(field: str):
    def resolve(ex: Executor, obj, args):
        return obj.get(field)
    return resolve


def file_field(field: str):
    def resolve(ex: Executor, obj, args):
        if field == "path":
            return obj["filename"]
        if field == "additions":
            return obj["additions"]
        if field == "deletions":
            return obj["deletions"]
        if field == "changeType":
            return {"added": "ADDED", "removed": "DELETED", "modified": "MODIFIED"}.get(obj["status"], "MODIFIED")
        return None
    return resolve


def ref_field(field: str):
    def resolve(ex: Executor, obj, args):
        _repo, name, sha = obj
        if field == "name":
            return name
        if field == "prefix":
            return "refs/heads/"
        if field == "id":
            return node_id("Ref", abs(hash(name)) % 10**9)
        if field == "target":
            return ("GitObject", sha)
        return None
    return resolve


def label_field(field: str):
    def resolve(ex: Executor, obj, args):
        if field == "id":
            return node_id("Label", obj["id"])
        return obj.get(field)
    return resolve


def type_field(field: str):
    def resolve(ex: Executor, obj, args):
        name = obj
        if field == "name":
            return name
        if field == "kind":
            if name in ENUM_VALUES:
                return "ENUM"
            if name and name[0].isupper():
                return "OBJECT"
            return "SCALAR"
        if field == "fields":
            return [("__Field", fname) for fname in TYPE_FIELDS.get(name, [])]
        if field == "enumValues":
            return [("__EnumValue", value) for value in ENUM_VALUES.get(name, [])]
        if field in ("possibleTypes", "interfaces", "inputFields"):
            return []
        if field == "ofType":
            return None
        if field == "description":
            return None
        return None
    return resolve


def named_field(field: str):
    def resolve(ex: Executor, obj, args):
        if field == "name":
            return obj
        if field == "isDeprecated":
            return False
        return None
    return resolve


def schema_field(field: str):
    def resolve(ex: Executor, obj, args):
        if field == "queryType":
            return ("__Type", "Query")
        if field == "mutationType":
            return ("__Type", "Mutation")
        if field == "types":
            return [("__Type", name) for name in sorted(TYPE_FIELDS)]
        if field == "directives":
            return []
        return None
    return resolve


# -- mutations ---------------------------------------------------------------

def _repo_by_global_id(ex: Executor, raw: str | None) -> Repo:
    from .store import decode_node_id
    decoded = decode_node_id(str(raw or ""))
    if decoded is not None and decoded[0] == "Repository":
        repo = ex.store.repos_by_id.get(decoded[1])
        if repo is not None:
            return repo
    raise ForgeError(422, "Validation Failed: repository not found")


def _thing_by_global_id(ex: Executor, raw: str | None) -> tuple[str, Repo, int]:
    from .store import decode_node_id
    decoded = decode_node_id(str(raw or ""))
    if decoded is not None:
        entry = ex.store.things_by_id.get(decoded[1])
        if entry is not None:
            return entry
    raise ForgeError(422, "Validation Failed: object not found")


def m_create_issue(ex: Executor, obj, args):
    data = args.get("input") or {}
    repo = _repo_by_global_id(ex, data.get("repositoryId"))
    issue = ex.store.create_issue(repo, data.get("title"), data.get("body") or "")
    return ("CreateIssuePayload", (repo, issue))


def m_close_issue(ex: Executor, obj, args):
    data = args.get("input") or {}
    kind, repo, number = _thing_by_global_id(ex, data.get("issueId"))
    if kind != "issue":
        raise ForgeError(422, "Validation Failed: not an issue")
    issue = repo.issues[number]
    issue.state = "closed"
    issue.closed_tick = issue.updated_tick = ex.store.tick()
    return ("CloseIssuePayload", (repo, issue))


def m_create_pull_request(ex: Executor, obj, args):
    data = args.get("input") or {}
    repo = _repo_by_global_id(ex, data.get("repositoryId"))
    pull = ex.store.create_pull(repo, data.get("title"), data.get("body") or "",
                                str(data.get("headRefName") or ""), str(data.get("baseRefName") or ""))
    return ("CreatePullRequestPayload", (repo, pull))


def m_merge_pull_request(ex: Executor, obj, args):
    data = args.get("input") or {}
    kind, repo, number = _thing_by_global_id(ex, data.get("pullRequestId"))
    if kind != "pull":
        raise ForgeError(422, "Validation Failed: not a pull request")
    pull = repo.pulls[number]
    method = str(data.get("mergeMethod") or "MERGE").lower()
    ex.store.merge_pull(repo, pull, method)
    return ("MergePullRequestPayload", (repo, pull))


def m_add_comment(ex: Executor, obj, args):
    data = args.get("input") or {}
    kind, repo, number = _thing_by_global_id(ex, data.get("subjectId"))
    if kind not in ("issue", "pull"):
        raise ForgeError(422, "Validation Failed: subject is not commentable")
    comment = ex.store.create_comment(repo, number, str(data.get("body") or ""))
    return ("AddCommentPayload", (repo, comment))


def payload_issue(ex: Executor, obj, args):
    repo, issue = obj
    return ("Issue", (repo, issue))


def payload_pull(ex: Executor, obj, args):
    repo, pull = obj
    return ("PullRequest", (repo, pull))


def payload_comment_edge(ex: Executor, obj, args):
    repo, comment = obj
    return ("IssueCommentEdge", (repo, comment))


def edge_node(ex: Executor, obj, args):
    repo, comment = obj
    return ("IssueComment", (repo, comment))


RESOLVERS: dict[tuple[str, str], object] = {
    ("Query", "viewer"): q_viewer,
    ("Query", "repository"): q_repository,
    ("Query", "node"): q_node,
    ("Mutation", "createIssue"): m_create_issue,
    ("Mutation", "closeIssue"): m_close_issue,
    ("Mutation", "createPullRequest"): m_create_pull_request,
    ("Mutation", "mergePullRequest"): m_merge_pull_request,
    ("Mutation", "addComment"): m_add_comment,
    ("CreateIssuePayload", "issue"): payload_issue,
    ("CloseIssuePayload", "issue"): payload_issue,
    ("CreatePullRequestPayload", "pullRequest"): payload_pull,
    ("MergePullRequestPayload", "pullRequest"): payload_pull,
    ("AddCommentPayload", "commentEdge"): payload_comment_edge,
    ("IssueCommentEdge", "node"): edge_node,
    ("IssueCommentEdge", "cursor"): lambda ex, obj, args: _cursor_for(obj[1].db_id),
    ("__Type", "name"): type_field("name"),
    ("__Type", "kind"): type_field("kind"),
    ("__Type", "fields"): type_field("fields"),
    ("__Type", "enumValues"): type_field("enumValues"),
    ("__Type", "description"): type_field("description"),
    ("__Type", "possibleTypes"): type_field("possibleTypes"),
    ("__Type", "interfaces"): type_field("interfaces"),
    ("__Type", "inputFields"): type_field("inputFields"),
    ("__Type", "ofType"): type_field("ofType"),
    ("__Field", "name"): named_field("name"),
    ("__Field", "isDeprecated"): named_field("isDeprecated"),
    ("__EnumValue", "name"): named_field("name"),
    ("__EnumValue", "isDeprecated"): named_field("isDeprecated"),
    ("__Schema", "queryType"): schema_field("queryType"),
    ("__Schema", "mutationType"): schema_field("mutationType"),
    ("__Schema", "types"): schema_field("types"),
    ("__Schema", "directives"): schema_field("directives"),
    ("GitObject", "oid"): lambda ex, obj, args: obj,
    ("GitObject", "id"): lambda ex, obj, args: obj,
}

for _f in ("login", "name", "email", "id", "databaseId"):
    RESOLVERS[("User", _f)] = user_field(_f)
for _f in TYPE_FIELDS["Repository"]:
    if ("Repository", _f) not in RESOLVERS:
        RESOLVERS[("Repository", _f)] = repo_field(_f)
RESOLVERS[("Repository", "issue")] = repo_issue
RESOLVERS[("Repository", "issues")] = repo_issues
RESOLVERS[("Repository", "pullRequest")] = repo_pull_request
RESOLVERS[("Repository", "pullRequests")] = repo_pull_requests
for _f in TYPE_FIELDS["Issue"]:
    RESOLVERS.setdefault(("Issue", _f), issue_field(_f))
for _f in TYPE_FIELDS["PullRequest"]:
    RESOLVERS.setdefault(("PullRequest", _f), pull_field(_f))
for _f in TYPE_FIELDS["IssueComment"]:
    RESOLVERS.setdefault(("IssueComment", _f), comment_field(_f))
for _type in ("IssueConnection", "PullRequestConnection", "PullRequestFileConnection", "LabelConnection"):
    for _f in ("nodes", "pageInfo", "totalCount", "edges"):
        RESOLVERS[(_type, _f)] = connection_field(_f)
for _f in ("hasNextPage", "hasPreviousPage", "startCursor", "endCursor"):
    RESOLVERS[("PageInfo", _f)] = pageinfo_field(_f)
for _f in ("path", "additions", "deletions", "changeType"):
    RESOLVERS[("PullRequestFile", _f)] = file_field(_f)
for _f in ("name", "prefix", "target", "id"):
    RESOLVERS[("Ref", _f)] = ref_field(_f)
for _f in ("id", "name", "color", "description"):
    RESOLVERS[("Label", _f)] = label_field(_f)
# Issue.labels returns a plain dict of nodes; give it a resolver pair.
RESOLVERS[("Issue", "labels")] = issue_field("labels")


def handle_graphql(store: Store, payload) -> tuple[int, object]:
    """Execute one GraphQL request body (single object or a batch list)."""
    if isinstance(payload, list):
        return 200, [_execute_one(store, item) for item in payload]
    return 200, _execute_one(store, payload)


def _execute_one(store: Store, item) -> dict:
    if not isinstance(item, dict) or not isinstance(item.get("query"), str):
        return {"errors": [{"message": "a GraphQL request needs a query string"}]}
    try:
        document = Parser(item["query"]).parse_document()
    except GqlError as exc:
        return {"errors": [{"message": f"syntax error: {exc}"}]}
    executor = Executor(store)
    try:
        return executor.execute(document, item.get("variables") or {}, item.get("operationName"))
    except ForgeError as exc:
        return {"data": None, "errors": [{"message": exc.message}]}
