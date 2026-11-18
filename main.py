"""Entrypoint for the github-core-clone-01 forge.

Lifecycle (mirrors the kit's starter): read MAKEFUN_ENV_URL, bind an
ephemeral port, register it with the environment, serve until the
environment reports done, then exit 0.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from forge.httpd import serve  # noqa: E402
from forge.store import Store  # noqa: E402


def _post_json(url: str, payload: dict, timeout: float = 5.0) -> int:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _get_json(url: str, timeout: float = 5.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def register(env_url: str, port: int) -> bool:
    """POST {port} to $MAKEFUN_ENV_URL/register with backoff; False on a fatal refusal."""
    delay = 0.25
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        try:
            status = _post_json(env_url + "/register", {"port": port})
        except Exception:
            status = -1
        if status in (200, 409):
            return True
        if status in (400, 405):
            return False
        time.sleep(delay)
        delay = min(delay * 2, 2.0)
    return False


def main() -> int:
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    if not env_url:
        print("MAKEFUN_ENV_URL is not set", file=sys.stderr)
        return 1
    store = Store()
    server = serve(store, host="", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    if not register(env_url, port):
        print("the environment refused registration", file=sys.stderr)
        server.shutdown()
        return 1
    # Poll for done, then shut down cleanly.
    while True:
        answer = _get_json(env_url + "/done")
        if answer and answer.get("done") is True:
            break
        time.sleep(0.2)
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
