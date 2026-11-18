"""Git smart-HTTP: advertisements, protocol-v2 ls-refs/fetch, v0 receive-pack.

The suite's client speaks protocol v2 for upload-pack (advertisement,
ls-refs, fetch) and v0 for receive-pack. Packs carry full objects only.
"""

from __future__ import annotations

from . import gitobj
from .gitobj import ZERO_OID
from .store import Repo

AGENT = "makefun-forge/1.0"
SIDEBAND_CHUNK = 32768


# --------------------------------------------------------------------------
# pkt-lines
# --------------------------------------------------------------------------

def pkt(payload: bytes) -> bytes:
    return f"{len(payload) + 4:04x}".encode("ascii") + payload


def pkt_line(text: str) -> bytes:
    return pkt(text.encode("utf-8") + b"\n")


FLUSH = b"0000"
DELIM = b"0001"


def parse_packets(data: bytes, pos: int = 0):
    """Yield (kind, payload, next_pos) for pkt-lines starting at pos.

    kind is 'data', 'flush', 'delim' or 'end'. Stops at the first non-pkt
    byte run (e.g. a pack following a flush in receive-pack).
    """
    while pos + 4 <= len(data):
        header = data[pos:pos + 4]
        try:
            length = int(header.decode("ascii"), 16)
        except (ValueError, UnicodeDecodeError):
            return
        if length == 0:
            yield ("flush", b"", pos + 4)
            pos += 4
            continue
        if length == 1:
            yield ("delim", b"", pos + 4)
            pos += 4
            continue
        if length == 2:
            yield ("end", b"", pos + 4)
            pos += 4
            continue
        if length < 4 or pos + length > len(data):
            return
        yield ("data", data[pos + 4:pos + length], pos + length)
        pos += length


def read_command_stream(data: bytes):
    """Split a v2 command request into (header_lines, arg_lines, rest_offset)."""
    headers: list[str] = []
    args: list[str] = []
    section = headers
    pos = 0
    for kind, payload, next_pos in parse_packets(data):
        pos = next_pos
        if kind == "delim":
            section = args
            continue
        if kind in ("flush", "end"):
            break
        section.append(payload.decode("utf-8", "replace").rstrip("\n"))
    return headers, args, pos


def sideband_packets(pack: bytes) -> bytes:
    out = bytearray()
    for start in range(0, len(pack), SIDEBAND_CHUNK):
        out += pkt(b"\x01" + pack[start:start + SIDEBAND_CHUNK])
    if not pack:
        out += pkt(b"\x01")
    return bytes(out)


# --------------------------------------------------------------------------
# Advertisements
# --------------------------------------------------------------------------

def v2_advertisement() -> bytes:
    lines = [
        "version 2",
        f"agent={AGENT}",
        "ls-refs=unborn",
        "fetch=shallow",
        "object-format=sha1",
    ]
    return b"".join(pkt_line(line) for line in lines) + FLUSH


def _ref_lines(repo: Repo, capabilities: str) -> bytes:
    refs = repo.refs
    head_target = repo.head_ref()
    head_oid = refs.get(head_target, ZERO_OID)
    out = bytearray()
    if not refs:
        out += pkt(f"{ZERO_OID} capabilities^{{}}\0{capabilities}".encode() + b"\n")
        return bytes(out) + FLUSH
    out += pkt(f"{head_oid} HEAD\0{capabilities}".encode() + b"\n")
    for name in sorted(refs):
        oid = refs[name]
        out += pkt_line(f"{oid} {name}")
        peeled = gitobj.peel_to_commit(repo.objects, oid)
        if name.startswith("refs/tags/") and peeled and peeled != oid:
            out += pkt_line(f"{peeled} {name}^{{}}")
    return bytes(out) + FLUSH


def v0_advertisement(repo: Repo, service: str) -> bytes:
    if service == "git-upload-pack":
        caps = (f"multi_ack_detailed side-band-64k thin-pack ofs-delta shallow "
                f"symref=HEAD:{repo.head_ref()} agent={AGENT}")
    else:
        caps = f"report-status side-band-64k delete-refs ofs-delta agent={AGENT}"
    preamble = pkt_line(f"# service={service}")
    return preamble + FLUSH + _ref_lines(repo, caps)


# --------------------------------------------------------------------------
# ls-refs (protocol v2)
# --------------------------------------------------------------------------

def ls_refs(repo: Repo, args: list[str]) -> bytes:
    peel = "peel" in args
    symrefs = "symrefs" in args
    unborn = "unborn" in args
    prefixes = [arg[len("ref-prefix "):] for arg in args if arg.startswith("ref-prefix ")]

    def wanted(name: str) -> bool:
        if not prefixes:
            return True
        return any(name.startswith(prefix) for prefix in prefixes)

    lines: list[str] = []
    head_target = repo.head_ref()
    head_oid = repo.refs.get(head_target)
    if wanted("HEAD"):
        if head_oid is not None:
            line = f"{head_oid} HEAD"
            if symrefs:
                line += f" symref-target:{head_target}"
            lines.append(line)
        elif unborn:
            line = "unborn HEAD"
            if symrefs:
                line += f" symref-target:{head_target}"
            lines.append(line)
    for name in sorted(repo.refs):
        if not wanted(name):
            continue
        oid = repo.refs[name]
        line = f"{oid} {name}"
        if peel:
            peeled = gitobj.peel_to_commit(repo.objects, oid)
            if peeled and peeled != oid:
                line += f" peeled:{peeled}"
        lines.append(line)
    return b"".join(pkt_line(line) for line in lines) + FLUSH


# --------------------------------------------------------------------------
# fetch (protocol v2, plus a minimal v0 fallback)
# --------------------------------------------------------------------------

def _pack_for(repo: Repo, wants: list[str], haves: list[str], depth: int | None,
              client_shallow: list[str] | None = None) -> tuple[bytes, set[str]]:
    included, boundary = gitobj.reachable_objects(repo.objects, wants, haves, depth,
                                                  exclude_shallow=set(client_shallow or []))
    order = {"commit": 0, "tree": 1, "blob": 2, "tag": 3}
    entries = sorted(
        ((sha, repo.objects[sha][0], repo.objects[sha][1]) for sha in included),
        key=lambda entry: (order[entry[1]], entry[0]),
    )
    return gitobj.pack_bytes(entries), boundary


def fetch_v2(repo: Repo, args: list[str]) -> bytes:
    wants: list[str] = []
    haves: list[str] = []
    client_shallow: list[str] = []
    depth: int | None = None
    for arg in args:
        if arg.startswith("want "):
            wants.append(arg[5:].strip())
        elif arg.startswith("have "):
            haves.append(arg[5:].strip())
        elif arg.startswith("shallow "):
            client_shallow.append(arg[8:].strip())
        elif arg.startswith("deepen "):
            try:
                depth = int(arg[7:].strip())
            except ValueError:
                depth = None
    pack, boundary = _pack_for(repo, wants, haves, depth, client_shallow)
    out = bytearray()
    sections: list[bytes] = []
    if haves:
        ack_lines = []
        known = next((have for have in haves if have in repo.objects), None)
        ack_lines.append(f"ACK {known}" if known else "NAK")
        sections.append(pkt_line("acknowledgments") + b"".join(pkt_line(line) for line in ack_lines))
    shallow_lines = [f"shallow {sha}" for sha in sorted(boundary)]
    if shallow_lines:
        sections.append(pkt_line("shallow-info") + b"".join(pkt_line(line) for line in shallow_lines))
    sections.append(pkt_line("packfile") + sideband_packets(pack))
    for index, section in enumerate(sections):
        if index:
            out += DELIM
        out += section
    out += FLUSH
    return bytes(out)


def fetch_v0(repo: Repo, body: bytes) -> bytes:
    wants: list[str] = []
    haves: list[str] = []
    client_shallow: list[str] = []
    depth: int | None = None
    state = "wants"
    for kind, payload, _next in parse_packets(body):
        if kind == "flush":
            if state == "wants":
                state = "haves"
            continue
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace").rstrip("\n")
        if line.startswith("want "):
            wants.append(line[5:].split(" ")[0].strip())
        elif line.startswith("have "):
            haves.append(line[5:].strip())
        elif line.startswith("shallow "):
            client_shallow.append(line[8:].strip())
        elif line.startswith("deepen "):
            try:
                depth = int(line[7:].strip())
            except ValueError:
                depth = None
        elif line == "done":
            break
    pack, boundary = _pack_for(repo, wants, haves, depth, client_shallow)
    out = bytearray()
    for sha in sorted(boundary):
        out += pkt_line(f"shallow {sha}")
    out += pkt_line("NAK")
    out += sideband_packets(pack)
    out += FLUSH
    return bytes(out)


def upload_pack(repo: Repo, headers: dict, body: bytes) -> tuple[int, str, bytes]:
    """Dispatch a POST to git-upload-pack: v2 command or v0 fallback."""
    first_lines: list[str] = []
    pos = 0
    for kind, payload, next_pos in parse_packets(body):
        pos = next_pos
        if kind == "data":
            first_lines.append(payload.decode("utf-8", "replace").rstrip("\n"))
            break
        break
    first = first_lines[0] if first_lines else ""
    if first.startswith("command="):
        command = first[len("command="):]
        hdrs, args, _rest = read_command_stream(body)
        if command == "ls-refs":
            return 200, "application/x-git-upload-pack-result", ls_refs(repo, args)
        if command == "fetch":
            return 200, "application/x-git-upload-pack-result", fetch_v2(repo, args)
        return 400, "text/plain", b"unknown command\n"
    if first.startswith("want "):
        return 200, "application/x-git-upload-pack-result", fetch_v0(repo, body)
    return 400, "text/plain", b"bad request\n"


# --------------------------------------------------------------------------
# receive-pack (protocol v0)
# --------------------------------------------------------------------------

def receive_pack(repo: Repo, body: bytes) -> tuple[int, str, bytes]:
    commands: list[tuple[str, str, str]] = []
    capabilities: list[str] = []
    pos = 0
    for kind, payload, next_pos in parse_packets(body):
        pos = next_pos
        if kind == "flush":
            break
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace").rstrip("\n")
        if "\0" in line:
            line, caps = line.split("\0", 1)
            capabilities = caps.split(" ")
        parts = line.split(" ")
        if len(parts) == 3:
            commands.append((parts[0], parts[1], parts[2]))
    pack_data = body[pos:]
    new_objects: dict[str, tuple[str, bytes]] = {}
    unpack_ok = True
    unpack_detail = "ok"
    if pack_data:
        try:
            new_objects = gitobj.unpack_pack(pack_data)
        except gitobj.ThinPackError as thin:
            new_objects = thin.objects
            # Resolve any delta whose base the repository already holds.
            merged = dict(repo.objects)
            merged.update(new_objects)
            pending = list(thin.pending)
            progressed = True
            while pending and progressed:
                progressed = False
                rest = []
                for kind, key, payload, _start in pending:
                    base = None
                    if kind == "ref":
                        base = merged.get(key)  # type: ignore[arg-type]
                    if base is not None:
                        resolved = gitobj.apply_delta(base[1], payload)
                        sha = gitobj.hash_object(base[0], resolved)
                        new_objects[sha] = (base[0], resolved)
                        merged[sha] = (base[0], resolved)
                        progressed = True
                    else:
                        rest.append((kind, key, payload, _start))
                pending = rest
            if pending:
                unpack_ok = False
                unpack_detail = "thin pack base missing"
        except Exception as exc:  # malformed pack
            unpack_ok = False
            unpack_detail = f"pack parse error: {exc}"
    if unpack_ok:
        repo.objects.update(new_objects)
    results: list[tuple[str, bool, str]] = []
    merged_view = dict(repo.objects)
    for old, new, ref in commands:
        if not unpack_ok:
            results.append((ref, False, "unpack failed"))
            continue
        ok, reason = _apply_ref_update(repo, merged_view, old, new, ref)
        results.append((ref, ok, reason))
    lines = [f"unpack {unpack_detail}"]
    for ref, ok, reason in results:
        lines.append(f"ok {ref}" if ok else f"ng {ref} {reason}")
    report = b"".join(pkt_line(line) for line in lines)
    if "side-band-64k" in capabilities:
        body_out = bytearray()
        for start in range(0, len(report), SIDEBAND_CHUNK):
            body_out += pkt(b"\x01" + report[start:start + SIDEBAND_CHUNK])
        body_out += FLUSH
        return 200, "application/x-git-receive-pack-result", bytes(body_out)
    return 200, "application/x-git-receive-pack-result", report + FLUSH


def _apply_ref_update(repo: Repo, view: dict, old: str, new: str, ref: str) -> tuple[bool, str]:
    if not (ref.startswith("refs/heads/") or ref.startswith("refs/tags/")):
        return False, "invalid ref"
    current = repo.refs.get(ref)
    if new == ZERO_OID:
        # Delete.
        if current is None:
            return False, "reference does not exist"
        del repo.refs[ref]
        return True, ""
    entry = view.get(new)
    if entry is None:
        return False, "unknown object"
    if ref.startswith("refs/heads/") and entry[0] != "commit":
        return False, "invalid object"
    if ref.startswith("refs/tags/") and entry[0] not in ("commit", "tag"):
        return False, "invalid object"
    effective_old = current if old == ZERO_OID else old
    if effective_old is not None and effective_old != ZERO_OID:
        if current is not None and current != effective_old:
            return False, "stale ref"
        if ref.startswith("refs/heads/") and effective_old != new:
            if not gitobj.is_ancestor(view, effective_old, new):
                return False, "non-fast-forward"
    repo.refs[ref] = new
    return True, ""
