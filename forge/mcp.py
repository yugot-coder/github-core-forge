"""MCP surface at /mcp: JSON-RPC 2.0 over Streamable HTTP.

Speaks the current revision (2026-07-28, via server/discover) and the legacy
handshake (initialize + notifications/initialized). The six tools act on the
same store as REST and GraphQL, with github-mcp-server argument names.
"""

from __future__ import annotations

import json

from . import rest as rest_mod
from .store import ForgeError, Store

SERVER_INFO = {"name": "makefun-forge", "version": "1.0.0"}
SUPPORTED_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

TOOLS = [
    {
        "name": "create_repo",
        "description": "Create a new repository owned by the authenticated user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The repository name."},
                "description": {"type": "string", "description": "A short description of the repository."},
                "private": {"type": "boolean", "description": "Whether the repository is private."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_repo",
        "description": "Get a repository by owner and name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "The repository owner login."},
                "repo": {"type": "string", "description": "The repository name."},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "create_issue",
        "description": "Create an issue in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "The repository owner login."},
                "repo": {"type": "string", "description": "The repository name."},
                "title": {"type": "string", "description": "The issue title."},
                "body": {"type": "string", "description": "The issue body."},
            },
            "required": ["owner", "repo", "title"],
        },
    },
    {
        "name": "list_issues",
        "description": "List issues in a repository, optionally filtered by state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "The repository owner login."},
                "repo": {"type": "string", "description": "The repository name."},
                "state": {"type": "string", "description": "open, closed or all (default open)."},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "create_pull_request",
        "description": "Create a pull request from a head branch into a base branch.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "The repository owner login."},
                "repo": {"type": "string", "description": "The repository name."},
                "title": {"type": "string", "description": "The pull request title."},
                "body": {"type": "string", "description": "The pull request body."},
                "head": {"type": "string", "description": "The head branch name."},
                "base": {"type": "string", "description": "The base branch name."},
            },
            "required": ["owner", "repo", "title", "head", "base"],
        },
    },
    {
        "name": "merge_pull_request",
        "description": "Merge a pull request by number.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "The repository owner login."},
                "repo": {"type": "string", "description": "The repository name."},
                "pull_number": {"type": "number", "description": "The pull request number."},
            },
            "required": ["owner", "repo", "pull_number"],
        },
    },
]

TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def _result(request_id, value) -> tuple[int, dict]:
    return 200, {"jsonrpc": "2.0", "id": request_id, "result": value}


def _error(request_id, code: int, message: str) -> tuple[int, dict]:
    return 200, {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_result(payload) -> dict:
    text = json.dumps(payload)
    return {"content": [{"type": "text", "text": text}], "structuredContent": payload, "isError": False}


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def handle_mcp(store: Store, raw_body: bytes) -> tuple[int, object, str]:
    """Returns (http_status, payload_or_None, content_type)."""
    try:
        request = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return 400, {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "Parse error"}}, "application/json"
    if isinstance(request, list):
        responses = []
        for item in request:
            status, payload = _handle_one(store, item)
            if payload is not None:
                responses.append(payload)
        return 200, responses, "application/json"
    status, payload = _handle_one(store, request)
    if payload is None:
        return 202, None, "application/json"
    return status, payload, "application/json"


def _handle_one(store: Store, request) -> tuple[int, dict | None]:
    if not isinstance(request, dict):
        return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}
    if method == "server/discover":
        return _result(request_id, {
            "resultType": "complete",
            "supportedVersions": list(SUPPORTED_VERSIONS),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": dict(SERVER_INFO),
        })
    if method == "initialize":
        offered = params.get("protocolVersion")
        version = offered if offered in SUPPORTED_VERSIONS else "2025-11-25"
        return _result(request_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": dict(SERVER_INFO),
        })
    if method == "ping":
        return _result(request_id, {})
    if isinstance(method, str) and method.startswith("notifications/"):
        return 202, None
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        return _call_tool(store, request_id, params)
    return _error(request_id, -32601, f"Method not found: {method}")


def _call_tool(store: Store, request_id, params: dict) -> tuple[int, dict]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    tool = TOOLS_BY_NAME.get(name) if isinstance(name, str) else None
    if tool is None:
        return _error(request_id, -32602, f"Unknown tool: {name}")
    required = tool["inputSchema"].get("required", [])
    missing = [key for key in required if arguments.get(key) is None]
    if missing:
        return _error(request_id, -32602, f"Missing required argument(s): {', '.join(missing)}")
    try:
        payload = _run_tool(store, name, arguments)
    except ForgeError as exc:
        return _result(request_id, _tool_error(exc.message))
    except Exception as exc:  # a tool failure, never a transport failure
        return _result(request_id, _tool_error(str(exc)))
    return _result(request_id, _tool_result(payload))


def _run_tool(store: Store, name: str, args: dict):
    viewer = store.viewer["login"]
    if name == "create_repo":
        repo = store.create_repo(str(args["name"]), str(args.get("description") or ""), bool(args.get("private", False)))
        return rest_mod.repo_json(store, repo)
    if name == "get_repo":
        repo = store.require_repo(str(args["owner"]), str(args["repo"]))
        return rest_mod.repo_json(store, repo)
    if name == "create_issue":
        repo = store.require_repo(str(args["owner"]), str(args["repo"]))
        issue = store.create_issue(repo, str(args["title"]), str(args.get("body") or ""))
        return rest_mod.issue_json(store, repo, issue)
    if name == "list_issues":
        repo = store.require_repo(str(args["owner"]), str(args["repo"]))
        state = str(args.get("state") or "open")
        issues = [issue for issue in repo.issues.values() if state == "all" or issue.state == state]
        issues.sort(key=lambda issue: (issue.created_tick, issue.number), reverse=True)
        return [rest_mod.issue_json(store, repo, issue) for issue in issues]
    if name == "create_pull_request":
        repo = store.require_repo(str(args["owner"]), str(args["repo"]))
        pull = store.create_pull(repo, str(args["title"]), str(args.get("body") or ""),
                                 str(args["head"]), str(args["base"]))
        return rest_mod.pull_json(store, repo, pull)
    if name == "merge_pull_request":
        repo = store.require_repo(str(args["owner"]), str(args["repo"]))
        number = args["pull_number"]
        pull = repo.pulls.get(int(number)) if number is not None else None
        if pull is None:
            raise ForgeError(404, "Not Found")
        store.merge_pull(repo, pull, "squash")
        return rest_mod.pull_json(store, repo, pull)
    raise ForgeError(404, f"Unknown tool: {name}")
