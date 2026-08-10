"""One settings object for all four entry points, from one TOML file.

The nic-os original carried four near-identical `Config` dataclasses, each
reading its own overlapping slice of the environment, each defaulting to a
`/var/lib` path and an agenix secret path that only existed on one host. That is
the single biggest thing standing between "runs here" and "installable".

So: **one** frozen `Settings`, loaded from a TOML file, with every value
overridable by an environment variable. Nothing is read at import time — `load()`
takes `env` and `path` as parameters, so a Settings built from an empty
environment is a valid, *safe* object (dry-run drain, no keys, loopback listen)
rather than a crash or a live-fire default.

    settings = load()                       # $IMMICH_CLIP_CONFIG or the defaults
    settings = load("/etc/immich-clip.toml")
    settings = load(env={"IMMICH_URL": "..."})   # tests never touch os.environ

Precedence, highest first: environment variable, TOML file, built-in default.
The environment wins because containers configure that way, and because a
compose file overriding one value should not have to template the whole TOML.
"""

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

try:  # 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 and older
    tomllib = None

DEFAULT_CONFIG_PATHS = (
    "/etc/immich-clip/config.toml",
    "/etc/immich-clip.toml",
)
DEFAULT_STATE_DIR = "/var/lib/immich-clip"

DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.I)
_MULTIPLIER = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


class ConfigError(Exception):
    """The config file is unreadable, malformed, or says something impossible."""


def parse_duration(value, default=None):
    """`"15m"` / `"1h"` / `"90"` -> seconds.

    Durations are written as durations in the TOML because `requeue_every = 3600`
    is a number nobody can read back. Bare integers stay valid so an environment
    variable can keep being a plain number.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return int(value)
    m = DURATION_RE.match(str(value))
    if not m:
        raise ConfigError(f"not a duration: {value!r} (try 30s, 15m, 1h, 2d)")
    return int(m.group(1)) * _MULTIPLIER[m.group(2).lower()]


def parse_listen(value, default_host="127.0.0.1", default_port=8351):
    """`"0.0.0.0:8351"` / `":8351"` / `"8351"` -> (host, port)."""
    raw = str(value or "").strip()
    if not raw:
        return default_host, default_port
    if ":" in raw:
        host, _, port = raw.rpartition(":")
        return (host or default_host), int(port or default_port)
    if raw.isdigit():
        return default_host, int(raw)
    return raw, default_port


@dataclass(frozen=True)
class ApiKey:
    """One Immich API key, and whose assets it can act on.

    `owner` is matched against the asset's owner — by user id, or by email, since
    a human writing a config file knows the email and not the UUID. The literal
    `"*"` is the fallback key, which is what a single-user install writes.

    Why per-owner at all: a workflow is always owned by whoever created it
    (`ownerId: auth.user.id`, no impersonation), and an API key can only put its
    own owner's assets into its own owner's album. With one key, a second user's
    photos match the rule and then quietly fail to file with `no_permission`. That
    was a real half-working deployment, not a hypothetical.
    """

    owner: str = "*"
    key: str = ""

    @property
    def is_default(self):
        return self.owner in ("*", "")


@dataclass(frozen=True)
class Settings:
    # ── Immich ───────────────────────────────────────────────────────────────
    immich_url: str = "http://127.0.0.1:2283"
    keys: tuple = ()
    #: A libpq DSN. Empty means "use the pg_* parts below", which is how a
    #: unix-socket/peer-auth deployment (the nic-os original) still works.
    db_url: str = ""
    pg: dict = field(default_factory=lambda: {"dbname": "immich"})
    #: Both blank by default on purpose: `api.clip_model` / `api.ml_url` then ask
    #: Immich itself, so there is nothing to keep in step by hand.
    model: str = ""
    ml_url: str = ""

    # ── Sidecar ──────────────────────────────────────────────────────────────
    listen_addr: str = "127.0.0.1"
    listen_port: int = 8351
    state_dir: str = DEFAULT_STATE_DIR
    profile_dir: str = ""
    queue_db: str = ""
    #: Server-side ceiling on a step's `waitSec`. Waiting only helps while a
    #: SmartSearch job is actually in flight; when the ML server is down no amount
    #: of waiting helps, and a long wait pins one of the workflow queue's slots.
    max_wait_sec: int = 15
    poll_sec: int = 2
    #: Refuse to serve if Immich's schema is not the shape the SQL expects.
    #: On by default: failing at startup with a named column beats failing per
    #: asset, months later, as photos quietly stop being filed.
    check_schema: bool = True

    # ── Drain ────────────────────────────────────────────────────────────────
    max_age_days: int = 30
    requeue_every: int = 3600
    #: SAFE default. A Settings built from an empty environment cannot write.
    apply: bool = False

    def __post_init__(self):
        # Derived rather than defaulted, so `state_dir = "/data"` in one line of
        # TOML moves all three.
        if not self.profile_dir:
            object.__setattr__(self, "profile_dir", f"{self.state_dir}/profiles")
        if not self.queue_db:
            object.__setattr__(self, "queue_db", f"{self.state_dir}/pending.sqlite")

    # ── Derived views ────────────────────────────────────────────────────────
    @property
    def stamp_path(self):
        return f"{self.state_dir}/drain-state.json"

    @property
    def api_key(self):
        """The default key — what single-user installs and the CLI tools use."""
        return self.key_for(None)

    def key_for(self, owner):
        """The key to act as for this asset owner, or "" when none is configured.

        A NAMED owner resolves to their own key or to the explicit `*` fallback,
        and to nothing else. Handing back some other user's key would file the
        photo into the wrong person's library, which is worse than not filing it
        — so an owner you did not configure is an error, not a near miss.

        With no owner (the CLI tools, a `system-config` read) any key will do,
        because the call is not acting on a particular person's assets.
        """
        if owner:
            for k in self.keys:
                if k.owner and k.owner.lower() == str(owner).lower():
                    return k.key
            for k in self.keys:
                if k.is_default:
                    return k.key
            return ""
        for k in self.keys:
            if k.is_default:
                return k.key
        return self.keys[0].key if self.keys else ""

    def owners(self):
        return tuple(k.owner for k in self.keys)

    def with_(self, **kw):
        """A copy with fields replaced — for tests and for `--apply` on the CLI."""
        return replace(self, **kw)


# ── loading ──────────────────────────────────────────────────────────────────
def _read_toml(path):
    if tomllib is None:  # pragma: no cover
        raise ConfigError("reading a config file needs Python 3.11+ (tomllib)")
    try:
        return tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        raise ConfigError(f"no config file at {path}")
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}")


def _keys_from_toml(section, read_file):
    """`[[immich.keys]]` entries, each `key` or `key_file`.

    `key_file` exists because every secret manager worth using — agenix, Docker
    secrets, Kubernetes — presents a secret as a file and not as a string in a
    config file that gets copied around.
    """
    out = []
    for entry in section.get("keys") or []:
        if not isinstance(entry, dict):
            raise ConfigError("[[immich.keys]] entries must be tables")
        value = entry.get("key") or ""
        if not value and entry.get("key_file"):
            value = read_file(entry["key_file"]) or ""
        out.append(ApiKey(owner=str(entry.get("owner") or "*"), key=value))
    # The one-key shorthand, so a single-user config is two lines and not a table
    # array. Listed last so an explicit `*` entry wins.
    single = section.get("api_key") or ""
    if not single and section.get("api_key_file"):
        single = read_file(section["api_key_file"]) or ""
    if single:
        out.append(ApiKey(owner="*", key=single))
    return out


def read_secret_file(path):
    """Read a secret file, stripping the trailing newline every tool leaves."""
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _env(env, name, default=""):
    return (os.environ if env is None else env).get(name, default)


def _env_bool(env, name, default=False):
    raw = _env(env, name, "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "on")


def _env_int(env, name, default):
    """Unset/blank/garbage all fall back — a typo in a compose file should not
    crash the container before it can log which variable was wrong."""
    raw = _env(env, name, "").strip()
    try:
        return int(raw)
    except ValueError:
        return default


def config_path(env=None):
    """Where the config file is, or None when there is none to read.

    A missing file is not an error: every value has a default and an environment
    override, so a pure-`docker run -e` deployment needs no file at all.
    """
    explicit = _env(env, "IMMICH_CLIP_CONFIG")
    if explicit:
        return explicit
    for candidate in DEFAULT_CONFIG_PATHS:
        if Path(candidate).exists():
            return candidate
    return None


def load(path=None, env=None, read_file=read_secret_file):
    """Build Settings from (defaults <- TOML <- environment)."""
    path = path or config_path(env)
    doc = _read_toml(path) if path else {}

    immich = doc.get("immich") or {}
    sidecar = doc.get("sidecar") or {}
    drain = doc.get("drain") or {}

    keys = _keys_from_toml(immich, read_file)
    # An env key wins outright: it is the most specific thing the operator said.
    env_key = _env(env, "IMMICH_API_KEY") or (
        read_file(_env(env, "IMMICH_API_KEY_FILE")) or ""
        if _env(env, "IMMICH_API_KEY_FILE")
        else ""
    )
    if env_key:
        keys = [ApiKey("*", env_key)] + [k for k in keys if not k.is_default]

    state_dir = _env(env, "IMMICH_CLIP_STATE_DIR") or sidecar.get("state_dir") or DEFAULT_STATE_DIR
    listen_host, listen_port = parse_listen(
        _env(env, "IMMICH_CLIP_LISTEN") or sidecar.get("listen") or "",
        default_host="127.0.0.1",
        default_port=8351,
    )
    # The two granular variables still work; they are what the systemd unit and a
    # lot of copy-pasted compose files already set.
    listen_host = _env(env, "LISTEN_ADDR") or listen_host
    listen_port = _env_int(env, "LISTEN_PORT", listen_port)

    pg = {
        "dbname": _env(env, "IMMICH_PG_DB") or immich.get("db_name") or "immich",
        "host": _env(env, "IMMICH_PG_HOST") or immich.get("db_host") or "",
        "port": str(_env(env, "IMMICH_PG_PORT") or immich.get("db_port") or ""),
        "user": _env(env, "IMMICH_PG_USER") or immich.get("db_user") or "",
        "password": _env(env, "IMMICH_PG_PASSWORD") or immich.get("db_password") or "",
    }

    return Settings(
        immich_url=(_env(env, "IMMICH_URL") or immich.get("url") or "http://127.0.0.1:2283").rstrip("/"),
        keys=tuple(keys),
        db_url=_env(env, "IMMICH_DB_URL") or immich.get("db_url") or "",
        pg=pg,
        model=_env(env, "IMMICH_CLIP_MODEL") or immich.get("clip_model") or "",
        ml_url=(_env(env, "IMMICH_ML_URL") or immich.get("ml_url") or "").rstrip("/"),
        listen_addr=listen_host,
        listen_port=listen_port,
        state_dir=state_dir,
        profile_dir=_env(env, "IMMICH_CLIP_PROFILE_DIR") or sidecar.get("profile_dir") or "",
        queue_db=_env(env, "IMMICH_CLIP_QUEUE_DB") or sidecar.get("queue_db") or "",
        max_wait_sec=_env_int(env, "IMMICH_CLIP_MAX_WAIT", parse_duration(sidecar.get("max_wait"), 15)),
        poll_sec=_env_int(env, "IMMICH_CLIP_POLL_SEC", parse_duration(sidecar.get("poll"), 2)),
        check_schema=_env_bool(env, "IMMICH_CLIP_CHECK_SCHEMA", bool(sidecar.get("check_schema", True))),
        max_age_days=_env_int(env, "IMMICH_CLIP_MAX_AGE_DAYS", int(drain.get("max_age_days", 30))),
        requeue_every=_env_int(
            env, "IMMICH_CLIP_REQUEUE_EVERY", parse_duration(drain.get("requeue_every"), 3600)
        ),
        apply=_env_bool(env, "IMMICH_CLIP_DRAIN_APPLY", bool(drain.get("apply", False))),
    )
