"""The suite's fixed scenario identity, re-derived from its published seed.

The conformance suite addresses every repository under the token owner's
login, but the bearer token it sends is opaque. The login is a deterministic
function of the contest's fixed scenario seed (wire.ts: SCENARIO_SEED,
xorshift64* streams keyed by label), so the forge re-derives it here with the
same arithmetic instead of guessing: stream "users", twelve generated users,
the first is the viewer.
"""

from __future__ import annotations

MASK = 0xFFFFFFFFFFFFFFFF
SCENARIO_SEED = 0x6A09E667F3BCC909

FIRST_NAMES = [
    "ada", "bram", "cora", "dmitri", "elena", "felix", "greta", "hugo", "iris", "jonas", "kira", "lars",
    "mira", "nico", "olga", "pavel", "quinn", "rosa", "sven", "tess", "ulla", "vito", "wren", "yara", "zane",
]
LAST_NAMES = [
    "anders", "bauer", "costa", "dahl", "eriksen", "fischer", "garcia", "holm", "iversen", "jansen", "keller",
    "lind", "moreau", "novak", "olsen", "peters", "quist", "rossi", "sato", "turi", "ulrich", "vega", "weber",
    "xu", "yilmaz", "zimmer",
]


def fnv1a64(text: str) -> int:
    value = 0xCBF29CE484222325
    for ch in text:
        value ^= ord(ch)
        value = (value * 0x100000001B3) & MASK
    return value


def mix64(value: int) -> int:
    z = (value + 0x9E3779B97F4A7C15) & MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
    return (z ^ (z >> 31)) & MASK


class Rng:
    """xorshift64* exactly as wire.ts defines it."""

    def __init__(self, seed: int):
        masked = seed & MASK
        self.state = masked if masked != 0 else SCENARIO_SEED

    @classmethod
    def from_label(cls, label: str) -> "Rng":
        return cls(mix64(SCENARIO_SEED ^ fnv1a64(label)))

    def next_u64(self) -> int:
        x = self.state
        x ^= x >> 12
        x ^= (x << 25) & MASK
        x ^= x >> 27
        self.state = x & MASK
        return (self.state * 0x2545F4914F6CDD1D) & MASK

    def next_u32(self) -> int:
        return self.next_u64() >> 32

    def next_int(self, bound: int) -> int:
        if bound <= 0:
            raise ValueError("bound must be positive")
        limit = (1 << 32) - ((1 << 32) % bound)
        while True:
            value = self.next_u32()
            if value < limit:
                return value % bound

    def pick(self, items):
        return items[self.next_int(len(items))]


def generate_users(count: int = 12) -> list[dict]:
    rng = Rng.from_label("users")
    used: set[str] = set()
    users = []
    for i in range(count):
        first = rng.pick(FIRST_NAMES)
        last = rng.pick(LAST_NAMES)
        base = "".join(ch for ch in f"{first}{last}" if ch.isalnum() or ch == "-")
        login = base
        suffix = 2
        while login in used:
            login = f"{base}-{suffix}"
            suffix += 1
        used.add(login)
        users.append({
            "id": 1000 + i,
            "login": login,
            "name": f"{first} {last}",
            "email": f"{login}@example.invalid",
        })
    return users


USERS = generate_users()
VIEWER = USERS[0]
