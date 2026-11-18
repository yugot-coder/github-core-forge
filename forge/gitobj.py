"""Git object model: hashing, pack read/write, tree/commit/tag handling.

Pure standard library. Objects are stored as ``{sha_hex: (type_name, data)}``
where data is the raw object payload (for a commit, the exact bytes after the
``commit <len>\\0`` header). Everything the forge serves or accepts goes
through these helpers so byte-exactness is preserved end to end.
"""

from __future__ import annotations

import hashlib
import zlib

ZERO_OID = "0" * 40

TYPE_IDS = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
ID_TYPES = {v: k for k, v in TYPE_IDS.items()}


def hash_object(type_name: str, data: bytes) -> str:
    header = f"{type_name} {len(data)}".encode("ascii") + b"\0"
    return hashlib.sha1(header + data).hexdigest()


def pack_bytes(objects: list[tuple[str, str, bytes]]) -> bytes:
    """Build a version-2 pack holding (sha, type, data) entries, no deltas."""
    out = bytearray()
    out += b"PACK"
    out += (2).to_bytes(4, "big")
    out += len(objects).to_bytes(4, "big")
    for _sha, type_name, data in objects:
        size = len(data)
        type_id = TYPE_IDS[type_name]
        first = (type_id << 4) | (size & 0x0F)
        size >>= 4
        if size:
            first |= 0x80
        out.append(first)
        while size:
            chunk = size & 0x7F
            size >>= 7
            out.append(chunk | (0x80 if size else 0))
        out += zlib.compress(data)
    trailer = hashlib.sha1(bytes(out)).digest()
    out += trailer
    return bytes(out)


def _read_varint_size(data: bytes, pos: int) -> tuple[int, int, int]:
    """Read a pack object header at pos: (type_id, size, new_pos)."""
    byte = data[pos]
    pos += 1
    type_id = (byte >> 4) & 0x07
    size = byte & 0x0F
    shift = 4
    while byte & 0x80:
        byte = data[pos]
        pos += 1
        size |= (byte & 0x7F) << shift
        shift += 7
    return type_id, size, pos


def _read_offset(data: bytes, pos: int) -> tuple[int, int]:
    byte = data[pos]
    pos += 1
    offset = byte & 0x7F
    while byte & 0x80:
        byte = data[pos]
        pos += 1
        offset = ((offset + 1) << 7) | (byte & 0x7F)
    return offset, pos


def apply_delta(base: bytes, delta: bytes) -> bytes:
    """Apply a git delta buffer to base bytes."""
    pos = 0

    def read_size() -> int:
        nonlocal pos
        value = 0
        shift = 0
        while True:
            byte = delta[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not (byte & 0x80):
                return value

    source_size = read_size()
    if source_size != len(base):
        raise ValueError("delta base size mismatch")
    target_size = read_size()
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            offset = 0
            size = 0
            for i in range(4):
                if op & (1 << i):
                    offset |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (0x10 << i):
                    size |= delta[pos] << (8 * i)
                    pos += 1
            if size == 0:
                size = 0x10000
            out += base[offset:offset + size]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise ValueError("delta opcode 0")
    if len(out) != target_size:
        raise ValueError("delta target size mismatch")
    return bytes(out)


def unpack_pack(data: bytes) -> dict[str, tuple[str, bytes]]:
    """Parse a pack (possibly with ofs/ref deltas) into {sha: (type, data)}."""
    if len(data) < 12 + 20 or data[:4] != b"PACK":
        raise ValueError("not a pack")
    count = int.from_bytes(data[8:12], "big")
    pos = 12
    raw: list[tuple[int, str, bytes, int]] = []  # (start_offset, kind, payload, extra)
    objects: dict[str, tuple[str, bytes]] = {}
    by_offset: dict[int, str] = {}
    pending: list[tuple[str, object, bytes, int]] = []
    for _ in range(count):
        start = pos
        type_id, _size, pos = _read_varint_size(data, pos)
        if type_id == 6:  # ofs-delta
            offset, pos = _read_offset(data, pos)
            base_start = start - offset
            decomp = zlib.decompressobj()
            payload = decomp.decompress(data[pos:])
            pos += len(data[pos:]) - len(decomp.unused_data)
            pending.append(("ofs", base_start, payload, start))
            raw.append((start, "ofs", payload, base_start))
        elif type_id == 7:  # ref-delta
            base_sha = data[pos:pos + 20].hex()
            pos += 20
            decomp = zlib.decompressobj()
            payload = decomp.decompress(data[pos:])
            pos += len(data[pos:]) - len(decomp.unused_data)
            pending.append(("ref", base_sha, payload, start))
            raw.append((start, "ref", payload, base_sha))  # type: ignore[arg-type]
        else:
            type_name = ID_TYPES[type_id]
            decomp = zlib.decompressobj()
            payload = decomp.decompress(data[pos:])
            pos += len(data[pos:]) - len(decomp.unused_data)
            sha = hash_object(type_name, payload)
            objects[sha] = (type_name, payload)
            by_offset[start] = sha
            raw.append((start, type_name, payload, 0))
    # Resolve deltas (possibly chained).
    progress = True
    while pending and progress:
        progress = False
        remaining = []
        for kind, key, payload, start in pending:
            base_sha = by_offset.get(key) if kind == "ofs" else key  # type: ignore[arg-type]
            if base_sha is not None and base_sha in objects:
                base_type, base_data = objects[base_sha]  # type: ignore[index]
                resolved = apply_delta(base_data, payload)
                sha = hash_object(base_type, resolved)
                objects[sha] = (base_type, resolved)
                by_offset[start] = sha
                progress = True
            else:
                remaining.append((kind, key, payload, start))
        pending = remaining
    if pending:
        # A delta whose base lives outside the pack (thin pack against repo
        # objects) is resolved by the caller through resolve_thin().
        raise ThinPackError(objects, pending)
    return objects


class ThinPackError(Exception):
    def __init__(self, objects: dict[str, tuple[str, bytes]], pending: list):
        super().__init__("thin pack: delta base not in pack")
        self.objects = objects
        self.pending = pending


# --------------------------------------------------------------------------
# Trees
# --------------------------------------------------------------------------

def parse_tree(data: bytes) -> list[tuple[str, str, str]]:
    """Parse tree bytes into [(mode, name, sha_hex)] in stored order."""
    entries = []
    pos = 0
    while pos < len(data):
        space = data.index(b" ", pos)
        mode = data[pos:space].decode("ascii")
        nul = data.index(b"\0", space)
        name = data[space + 1:nul].decode("utf-8", "surrogateescape")
        sha = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, sha))
        pos = nul + 21
    return entries


def build_tree(entries: list[tuple[str, str, str]]) -> bytes:
    """Serialize [(mode, name, sha_hex)] with git's tree sort order."""
    def sort_key(entry: tuple[str, str, str]) -> bytes:
        mode, name, _sha = entry
        suffix = "/" if mode == "40000" else ""
        return (name + suffix).encode("utf-8", "surrogateescape")

    out = bytearray()
    for mode, name, sha in sorted(entries, key=sort_key):
        out += mode.encode("ascii") + b" " + name.encode("utf-8", "surrogateescape") + b"\0" + bytes.fromhex(sha)
    return bytes(out)


def flatten_tree(store: dict[str, tuple[str, bytes]], tree_sha: str, prefix: str = "") -> dict[str, tuple[str, str]]:
    """Flatten a tree into {path: (mode, blob_sha)} for files only."""
    flat: dict[str, tuple[str, str]] = {}
    if tree_sha is None or tree_sha not in store:
        return flat
    type_name, data = store[tree_sha]
    if type_name != "tree":
        return flat
    for mode, name, sha in parse_tree(data):
        path = f"{prefix}{name}"
        if mode == "40000":
            flat.update(flatten_tree(store, sha, path + "/"))
        else:
            flat[path] = (mode, sha)
    return flat


def tree_from_flat(store: dict[str, tuple[str, bytes]], flat: dict[str, tuple[str, str]]) -> str:
    """Build (and store) nested trees from {path: (mode, blob_sha)}; returns root tree sha."""
    # Build a nested dict first.
    root: dict = {}
    for path, (mode, sha) in flat.items():
        parts = path.split("/")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = (mode, sha)

    def build(node: dict) -> str:
        entries = []
        for name, value in node.items():
            if isinstance(value, dict):
                sub_sha = build(value)
                entries.append(("40000", name, sub_sha))
            else:
                mode, sha = value
                entries.append((mode, name, sha))
        data = build_tree(entries)
        sha = hash_object("tree", data)
        store[sha] = ("tree", data)
        return sha

    return build(root)


# --------------------------------------------------------------------------
# Commits and tags
# --------------------------------------------------------------------------

def parse_commit(data: bytes) -> dict:
    headers, _, message = data.partition(b"\n\n")
    result: dict = {"parents": [], "message": message.decode("utf-8", "surrogateescape")}
    for line in headers.decode("utf-8", "surrogateescape").split("\n"):
        if line.startswith("tree "):
            result["tree"] = line[5:].strip()
        elif line.startswith("parent "):
            result["parents"].append(line[7:].strip())
        elif line.startswith("author "):
            result["author"] = line[7:]
        elif line.startswith("committer "):
            result["committer"] = line[10:]
    return result


def build_commit(tree: str, parents: list[str], author: str, committer: str, message: str) -> bytes:
    lines = [f"tree {tree}"]
    for parent in parents:
        lines.append(f"parent {parent}")
    lines.append(f"author {author}")
    lines.append(f"committer {committer}")
    return ("\n".join(lines) + "\n\n" + message).encode("utf-8", "surrogateescape")


def parse_tag(data: bytes) -> dict:
    headers, _, message = data.partition(b"\n\n")
    result: dict = {"message": message.decode("utf-8", "surrogateescape")}
    for line in headers.decode("utf-8", "surrogateescape").split("\n"):
        if line.startswith("object "):
            result["object"] = line[7:].strip()
        elif line.startswith("type "):
            result["type"] = line[5:].strip()
        elif line.startswith("tag "):
            result["tag"] = line[4:].strip()
        elif line.startswith("tagger "):
            result["tagger"] = line[7:]
    return result


def peel_to_commit(store: dict[str, tuple[str, bytes]], sha: str) -> str | None:
    """Follow tag objects until a commit; None if the chain breaks."""
    seen = set()
    current = sha
    while current in store and current not in seen:
        seen.add(current)
        type_name, data = store[current]
        if type_name == "commit":
            return current
        if type_name == "tag":
            current = parse_tag(data).get("object", "")
            continue
        return None
    return None


def commit_ancestors(store: dict[str, tuple[str, bytes]], tip: str) -> dict[str, int]:
    """Map of commit sha -> generation distance from tip (tip = 0)."""
    distances: dict[str, int] = {}
    if tip not in store:
        return distances
    queue = [(tip, 0)]
    while queue:
        sha, dist = queue.pop(0)
        if sha in distances and distances[sha] <= dist:
            continue
        entry = store.get(sha)
        if entry is None or entry[0] != "commit":
            continue
        distances[sha] = dist
        for parent in parse_commit(entry[1])["parents"]:
            queue.append((parent, dist + 1))
    return distances


def is_ancestor(store: dict[str, tuple[str, bytes]], ancestor: str, tip: str) -> bool:
    return ancestor in commit_ancestors(store, tip)


def merge_base(store: dict[str, tuple[str, bytes]], a: str, b: str) -> str | None:
    """Best common ancestor of two commits (deterministic tie-break)."""
    anc_a = commit_ancestors(store, a)
    anc_b = commit_ancestors(store, b)
    common = set(anc_a) & set(anc_b)
    if not common:
        return None
    return min(common, key=lambda sha: (anc_a[sha] + anc_b[sha], sha))


def reachable_objects(
    store: dict[str, tuple[str, bytes]],
    tips: list[str],
    exclude_tips: list[str] | None = None,
    depth: int | None = None,
    exclude_shallow: set[str] | None = None,
    _cut: set[str] | None = None,
) -> tuple[set[str], set[str]]:
    """Objects reachable from tips, minus objects reachable from exclude_tips.

    Returns (object_shas, boundary_commits): boundary commits are included
    commits whose parents were cut off by the depth limit. exclude_shallow
    names commits the client holds only shallowly: the exclusion walk treats
    them as roots, since their ancestors are not held.
    """
    excluded: set[str] = set()
    if exclude_tips:
        excluded = _closure(store, exclude_tips, None, exclude_shallow or set())
    included: set[str] = set()
    boundary: set[str] = set()
    cut = _cut or set()
    # Commit walk with depth.
    commit_depth: dict[str, int] = {}
    queue: list[tuple[str, int]] = []
    for tip in tips:
        entry = store.get(tip)
        if entry is None:
            continue
        if entry[0] == "tag":
            # Tag objects are always included whole; their target is walked.
            if tip not in excluded:
                included.add(tip)
            target = parse_tag(entry[1]).get("object")
            if target:
                queue.append((target, 0))
        elif entry[0] == "commit":
            queue.append((tip, 0))
        else:
            if tip not in excluded:
                included.add(tip)
    while queue:
        sha, dist = queue.pop(0)
        if sha in commit_depth and commit_depth[sha] <= dist:
            continue
        entry = store.get(sha)
        if entry is None or entry[0] != "commit":
            continue
        commit_depth[sha] = dist
        parsed = parse_commit(entry[1])
        parents = parsed["parents"]
        if sha in cut:
            parents = []
        if depth is not None and dist + 1 >= depth and parents:
            boundary.add(sha)
        for parent in parents:
            if depth is None or dist + 1 < depth:
                queue.append((parent, dist + 1))
    commits = set(commit_depth)
    # Add trees/blobs for included commits.
    for sha in commits:
        if sha in excluded:
            continue
        included.add(sha)
        tree = parse_commit(store[sha][1])["tree"]
        stack = [tree]
        while stack:
            current = stack.pop()
            if current in included or current in excluded:
                continue
            entry = store.get(current)
            if entry is None:
                continue
            included.add(current)
            if entry[0] == "tree":
                for _mode, _name, child in parse_tree(entry[1]):
                    stack.append(child)
    included -= excluded
    boundary = {sha for sha in boundary if sha in included}
    return included, boundary


def _closure(store: dict[str, tuple[str, bytes]], tips: list[str], depth: int | None,
             cut: set[str] | None = None) -> set[str]:
    objects, _boundary = reachable_objects(store, tips, None, depth, _cut=cut or set())
    return objects


def three_way_merge(
    base_flat: dict[str, tuple[str, str]],
    ours_flat: dict[str, tuple[str, str]],
    theirs_flat: dict[str, tuple[str, str]],
) -> dict[str, tuple[str, str]] | None:
    """Merge flattened trees; None on conflict."""
    merged: dict[str, tuple[str, str]] = {}
    paths = set(base_flat) | set(ours_flat) | set(theirs_flat)
    for path in paths:
        base = base_flat.get(path)
        ours = ours_flat.get(path)
        theirs = theirs_flat.get(path)
        if ours == theirs:
            if ours is not None:
                merged[path] = ours
        elif base == ours:
            if theirs is not None:
                merged[path] = theirs
        elif base == theirs:
            if ours is not None:
                merged[path] = ours
        else:
            return None
    return merged
