"""Configuration: the file, the environment, and the precedence between them.

This is the layer that did not exist in the original — four `Config` dataclasses,
each reading its own slice of the environment, each with a `/var/lib` default and
an agenix path baked in. So these tests are less about regressions than about
pinning the contract a stranger's install depends on: what wins, what a missing
file means, and above all that *nothing* here can produce a settings object that
writes when it was not asked to.
"""

import pytest
from fakes import settings

from immich_clip.config import (
    ApiKey,
    ConfigError,
    Settings,
    load,
    parse_duration,
    parse_listen,
)

NOTHING = {}


def no_files(_path):
    """A `read_file` that finds nothing — keeps these tests off the real disk."""
    return None


def a_config(tmp_path, body):
    p = tmp_path / "config.toml"
    p.write_text(body)
    return str(p)


# ── the safe-by-default property ─────────────────────────────────────────────
def test_settings_from_nothing_at_all_cannot_write():
    cfg = load(env=NOTHING, read_file=no_files)
    assert cfg.apply is False          # the drainer reports and stops
    assert cfg.keys == ()              # nothing to file with even if it tried
    assert cfg.listen_addr == "127.0.0.1"   # not exposed to the network
    assert cfg.check_schema is True    # and it will refuse a schema it cannot use


def test_a_missing_config_file_is_not_an_error():
    # Every value has a default and an env override, so a pure `docker run -e`
    # deployment needs no file at all.
    cfg = load(path=None, env={"IMMICH_URL": "http://immich:2283"}, read_file=no_files)
    assert cfg.immich_url == "http://immich:2283"


def test_an_explicitly_named_config_file_that_is_absent_IS_an_error():
    # The other half of the rule above: if you named a file, a typo in the path
    # must not silently fall back to defaults and half-work.
    with pytest.raises(ConfigError):
        load(path="/nonexistent/config.toml", env=NOTHING, read_file=no_files)


def test_a_malformed_config_file_names_itself(tmp_path):
    path = a_config(tmp_path, "this is not = = toml")
    with pytest.raises(ConfigError) as e:
        load(path=path, env=NOTHING, read_file=no_files)
    assert "config.toml" in str(e.value)


# ── precedence ───────────────────────────────────────────────────────────────
def test_the_environment_beats_the_file(tmp_path):
    # Containers configure by environment, and overriding one value in a compose
    # file should not mean templating the whole TOML.
    path = a_config(tmp_path, '[immich]\nurl = "http://from-file:2283"\n')
    cfg = load(path=path, env={"IMMICH_URL": "http://from-env:2283"}, read_file=no_files)
    assert cfg.immich_url == "http://from-env:2283"


def test_the_file_beats_the_built_in_default(tmp_path):
    path = a_config(tmp_path, '[immich]\nurl = "http://from-file:2283"\n')
    assert load(path=path, env=NOTHING, read_file=no_files).immich_url == "http://from-file:2283"


def test_a_trailing_slash_on_the_url_is_dropped(tmp_path):
    # Otherwise every request path becomes `//api/...`, which some proxies 404.
    path = a_config(tmp_path, '[immich]\nurl = "http://immich:2283/"\n')
    assert load(path=path, env=NOTHING, read_file=no_files).immich_url == "http://immich:2283"


# ── derived paths ────────────────────────────────────────────────────────────
def test_one_state_dir_moves_all_three_paths():
    cfg = load(env={"IMMICH_CLIP_STATE_DIR": "/data"}, read_file=no_files)
    assert cfg.profile_dir == "/data/profiles"
    assert cfg.queue_db == "/data/pending.sqlite"
    assert cfg.stamp_path == "/data/drain-state.json"


def test_an_explicit_path_still_wins_over_the_derived_one():
    cfg = load(
        env={"IMMICH_CLIP_STATE_DIR": "/data", "IMMICH_CLIP_QUEUE_DB": "/elsewhere/q.db"},
        read_file=no_files,
    )
    assert cfg.queue_db == "/elsewhere/q.db"
    assert cfg.profile_dir == "/data/profiles"   # the other is still derived


# ── durations and listen addresses ───────────────────────────────────────────
@pytest.mark.parametrize(
    "text,seconds",
    [("30s", 30), ("15m", 900), ("1h", 3600), ("2d", 172800), ("90", 90), (900, 900)],
)
def test_durations_are_written_as_durations(text, seconds):
    # `requeue_every = 3600` is a number nobody can read back.
    assert parse_duration(text) == seconds


def test_a_nonsense_duration_is_refused_rather_than_defaulted():
    with pytest.raises(ConfigError):
        parse_duration("every fortnight")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0.0.0.0:8351", ("0.0.0.0", 8351)),
        (":9000", ("127.0.0.1", 9000)),
        ("9000", ("127.0.0.1", 9000)),
        ("", ("127.0.0.1", 8351)),
        ("::1:8351", ("::1", 8351)),
    ],
)
def test_listen_addresses_parse_the_ways_people_write_them(text, expected):
    assert parse_listen(text) == expected


def test_the_granular_listen_variables_still_work():
    # A lot of copy-pasted systemd units and compose files already set these.
    cfg = load(env={"LISTEN_ADDR": "0.0.0.0", "LISTEN_PORT": "9999"}, read_file=no_files)
    assert (cfg.listen_addr, cfg.listen_port) == ("0.0.0.0", 9999)


def test_a_garbled_integer_falls_back_instead_of_crashing():
    # A typo in a compose file should not kill the container before it can log
    # which variable was wrong.
    assert load(env={"LISTEN_PORT": "eight-thousand"}, read_file=no_files).listen_port == 8351


# ── API keys ─────────────────────────────────────────────────────────────────
def test_the_single_key_shorthand_is_the_default_key(tmp_path):
    path = a_config(tmp_path, '[immich]\napi_key = "abc"\n')
    cfg = load(path=path, env=NOTHING, read_file=no_files)
    assert cfg.api_key == "abc"
    assert cfg.key_for("anyone@example.com") == "abc"


def test_a_key_can_come_from_a_file(tmp_path):
    # Every secret manager worth using — agenix, Docker secrets, Kubernetes —
    # presents a secret as a file, not as a string in a config file that gets
    # copied around.
    secret = tmp_path / "key"
    secret.write_text("from-a-file\n")   # note the newline every tool leaves
    path = a_config(tmp_path, f'[immich]\napi_key_file = "{secret}"\n')
    from immich_clip.config import read_secret_file

    assert load(path=path, env=NOTHING, read_file=read_secret_file).api_key == "from-a-file"


def test_per_owner_keys_are_matched_by_email(tmp_path):
    path = a_config(tmp_path, """
[immich]
[[immich.keys]]
owner = "nico@example.com"
key   = "nico-key"
[[immich.keys]]
owner = "alfie@example.com"
key   = "alfie-key"
""")
    cfg = load(path=path, env=NOTHING, read_file=no_files)
    assert cfg.key_for("nico@example.com") == "nico-key"
    assert cfg.key_for("alfie@example.com") == "alfie-key"


def test_owner_matching_ignores_case():
    # Immich stores the email as typed; a config file will not match its capitals.
    cfg = settings(keys=[ApiKey("Nico@Example.com", "k")])
    assert cfg.key_for("nico@example.com") == "k"


def test_an_owner_with_no_key_gets_nothing_rather_than_someone_elses(tmp_path):
    # The whole point. Handing back another user's key would file the photo into
    # the wrong library, which is worse than not filing it.
    path = a_config(tmp_path, """
[immich]
[[immich.keys]]
owner = "nico@example.com"
key   = "nico-key"
""")
    cfg = load(path=path, env=NOTHING, read_file=no_files)
    assert cfg.key_for("stranger@example.com") == ""


def test_a_single_NAMED_key_does_not_quietly_serve_everyone_else():
    # If you wrote an owner, you meant it. The single-user install writes the
    # `api_key` shorthand instead, which becomes a `*` entry and does serve
    # everyone — see the two tests above and below.
    cfg = settings(keys=[ApiKey("nico@example.com", "only-key")])
    assert cfg.key_for("someone-else@example.com") == ""
    # ...but a call with no particular owner (the CLI tools) still works.
    assert cfg.api_key == "only-key"


def test_a_star_entry_is_the_explicit_fallback():
    cfg = settings(keys=[ApiKey("nico@example.com", "nico"), ApiKey("*", "fallback")])
    assert cfg.key_for("nico@example.com") == "nico"
    assert cfg.key_for("stranger@example.com") == "fallback"


def test_an_environment_key_replaces_the_files_default_but_keeps_named_owners(tmp_path):
    path = a_config(tmp_path, """
[immich]
api_key = "from-file"
[[immich.keys]]
owner = "alfie@example.com"
key   = "alfie-key"
""")
    cfg = load(path=path, env={"IMMICH_API_KEY": "from-env"}, read_file=no_files)
    assert cfg.api_key == "from-env"
    assert cfg.key_for("alfie@example.com") == "alfie-key"


def test_a_key_table_that_is_not_a_table_is_refused(tmp_path):
    path = a_config(tmp_path, '[immich]\nkeys = ["just-a-string"]\n')
    with pytest.raises(ConfigError):
        load(path=path, env=NOTHING, read_file=no_files)


# ── the database ─────────────────────────────────────────────────────────────
def test_a_dsn_is_carried_through_verbatim(tmp_path):
    # The only form that works across a network, and what a container gets.
    path = a_config(tmp_path, '[immich]\ndb_url = "postgresql://ro@db/immich"\n')
    assert load(path=path, env=NOTHING, read_file=no_files).db_url == "postgresql://ro@db/immich"


def test_with_no_dsn_the_parts_describe_a_unix_socket():
    # An empty host is what makes psycopg2 use the socket and Postgres apply
    # `local all all peer` — a same-host install with no password and no new role.
    cfg = load(env=NOTHING, read_file=no_files)
    assert cfg.db_url == ""
    assert cfg.pg["host"] == ""
    assert cfg.pg["dbname"] == "immich"


# ── the drain switch ─────────────────────────────────────────────────────────
def test_apply_can_be_turned_on_from_the_file_or_the_environment(tmp_path):
    path = a_config(tmp_path, "[drain]\napply = true\n")
    assert load(path=path, env=NOTHING, read_file=no_files).apply is True
    assert load(env={"IMMICH_CLIP_DRAIN_APPLY": "yes"}, read_file=no_files).apply is True
    assert load(env={"IMMICH_CLIP_DRAIN_APPLY": "0"}, read_file=no_files).apply is False


def test_drain_intervals_accept_durations(tmp_path):
    path = a_config(tmp_path, '[drain]\nrequeue_every = "2h"\nmax_age_days = 7\n')
    cfg = load(path=path, env=NOTHING, read_file=no_files)
    assert cfg.requeue_every == 7200
    assert cfg.max_age_days == 7


def test_settings_are_frozen_so_nothing_can_mutate_them_mid_request():
    cfg = Settings()
    with pytest.raises(Exception):
        cfg.apply = True


def test_with_returns_a_copy_rather_than_mutating():
    cfg = Settings()
    assert cfg.with_(apply=True).apply is True
    assert cfg.apply is False
