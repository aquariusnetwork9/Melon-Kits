"""Configuration surface and strict validation (SPEC §9).

:data:`DEFAULTS` is SPEC §9 verbatim -- same keys, same nesting, same values.
:func:`load_config` deep-merges ``DEFAULTS <- JSON file <- environment`` and
raises :class:`ConfigError` on *any* unknown key at *any* nesting level and on
any type mismatch.

The strictness is the point, not pedantry: a typo'd key that silently keeps its
default is how a months-long unattended run ends up misconfigured (SPEC §9), so
an unknown key is a startup failure, and so is an unknown ``CHATCOL_*``
environment variable.

Environment names are ``CHATCOL_`` + the nested path joined with ``_`` and
upper-cased, e.g. ``backfill.min_interval_s`` -> ``CHATCOL_BACKFILL_MIN_INTERVAL_S``
and ``sse.enabled`` -> ``CHATCOL_SSE_ENABLED``. Values are coerced to the type of
the corresponding default: booleans from ``true``/``false``/``1``/``0``
(case-insensitively), ints, floats, strings, and -- for the three keys whose
default is ``null`` -- the empty string means ``None``.

Nothing happens at import time beyond building the constant tables.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, Mapping, Optional, Tuple

ENV_PREFIX = "CHATCOL_"

#: SPEC §9, verbatim. Do not add keys here without adding them to the spec.
DEFAULTS: Dict[str, Any] = {
    "data_dir": "./corpus",
    "state_path": "./corpus/state.json",
    "status_path": "./corpus/status.json",
    "log_path": "./corpus/collector.log",
    "lock_path": "./corpus/collector.lock",
    "user_agent": "chat-corpus-collector/1.0 (+contact: <operator>)",
    "sse": {
        "enabled": True,
        "host": "api.2b2t.vc",
        "path": "/feed/chats",
        "socket_read_timeout_s": 20,
        "heartbeat_deadline_s": 90,
        "initial_grace_s": 120,
        "backoff_reset_s": 60,
        "backoff_broken_max_s": 60,
        "backoff_429_max_s": 300,
    },
    "backfill": {
        "enabled": True,
        "host": "api.2b2t.vc",
        "path": "/chats/window",
        "page_size": 100,
        "min_interval_s": 1.0,
        "request_timeout_s": 30,
        "safety_lag_s": 120,
        "second_pass_delay_s": 5400,
        "max_lookback_days": 7,
        "backoff_429_max_s": 300,
        "audit_enabled": True,
        "audit_interval_days": 7,
        "audit_span_days": 7,
        "audit_lag_days": 1,
    },
    "log_tail": {
        "enabled": False,
        "path": "C:/path/to/bot/log/latest.log",
        "poll_interval_s": 1.0,
        "timezone": None,
        "schema_from_config_path": None,
    },
    "storage": {
        "store_component": True,
        "fsync_interval_ms": 2000,
        "gzip_delay_s": 3600,
        "gzip_level": 6,
        "max_file_bytes": 0,
    },
    "dedupe": {
        "window_s": 21600,
        "max_keys": 400000,
        "rehydrate_max_bytes": 268435456,
    },
    "ops": {
        "status_interval_s": 15,
        "alert_command": None,
        "log_level": "INFO",
        "log_max_bytes": 10485760,
        "log_backup_count": 5,
    },
}

_TRUE_WORDS = frozenset(("true", "1"))
_FALSE_WORDS = frozenset(("false", "0"))


class ConfigError(ValueError):
    """Configuration is unknown, mistyped, or unreadable. Always fatal at startup."""


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Return the effective configuration: DEFAULTS <- file <- env (SPEC §9).

    `path` may be ``None`` (defaults + environment only). A named file that does
    not exist is a :class:`ConfigError`, not a silent fall-through to defaults --
    an operator who passed ``--config`` meant it.

    Raises :class:`ConfigError` on an unknown key at any nesting level, an
    unknown ``CHATCOL_*`` environment variable, a type mismatch against the
    default's type, or an env value that will not coerce.
    """
    cfg = copy.deepcopy(DEFAULTS)
    if path is not None:
        _merge_mapping(cfg, _read_json_file(path), DEFAULTS, ())
    _apply_env(cfg, os.environ)
    return cfg


def env_name(dotted_path: str) -> str:
    """Environment variable name for a dotted config path (SPEC §9).

    e.g. ``'backfill.min_interval_s'`` -> ``'CHATCOL_BACKFILL_MIN_INTERVAL_S'``.
    """
    return ENV_PREFIX + dotted_path.replace(".", "_").upper()


# --------------------------------------------------------------------------- #
# file merge
# --------------------------------------------------------------------------- #


def _read_json_file(path: str) -> Dict[str, Any]:
    """Load and shape-check the JSON config file (SPEC §9)."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        raise ConfigError("config file not found: %s" % (path,)) from None
    except OSError as exc:
        raise ConfigError(
            "config file unreadable: %s (%s)" % (path, type(exc).__name__)
        ) from None

    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            "config file %s is not valid JSON at line %d column %d"
            % (path, exc.lineno, exc.colno)
        ) from None
    except ValueError as exc:
        raise ConfigError(
            "config file %s is not valid JSON (%s)" % (path, type(exc).__name__)
        ) from None

    if not isinstance(obj, dict):
        raise ConfigError(
            "config file %s must contain a JSON object, got %s"
            % (path, _kind_of_value(obj))
        )
    return obj


def _merge_mapping(
    dst: Dict[str, Any],
    src: Mapping[str, Any],
    spec: Mapping[str, Any],
    prefix: Tuple[str, ...],
) -> None:
    """Recursively merge `src` into `dst`, validating against `spec` (SPEC §9).

    `spec` is always the corresponding *DEFAULTS* subtree, never the partially
    merged result -- the default is what defines the permitted type, including
    for the nullable keys whose default is ``None``.
    """
    for key in src:
        if not isinstance(key, str) or key not in spec:
            raise ConfigError(
                "unknown config key: %s" % (_dotted(prefix + (str(key),)),)
            )
        default = spec[key]
        value = src[key]
        dotted = _dotted(prefix + (key,))
        if isinstance(default, dict):
            if not isinstance(value, dict):
                raise ConfigError(
                    "config key %s: expected an object, got %s"
                    % (dotted, _kind_of_value(value))
                )
            _merge_mapping(dst[key], value, default, prefix + (key,))
        else:
            dst[key] = _coerce_json_value(value, default, dotted)


def _coerce_json_value(value: Any, default: Any, dotted: str) -> Any:
    """Type-check a JSON-sourced value against its default (SPEC §9)."""
    if default is None:
        # The three nullable keys of SPEC §9 are all nullable *strings*.
        if value is None or isinstance(value, str):
            return value
        raise ConfigError(
            "config key %s: expected a string or null, got %s"
            % (dotted, _kind_of_value(value))
        )
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        raise ConfigError(
            "config key %s: expected a boolean, got %s"
            % (dotted, _kind_of_value(value))
        )
    if isinstance(default, int):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        raise ConfigError(
            "config key %s: expected an integer, got %s"
            % (dotted, _kind_of_value(value))
        )
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(
                "config key %s: expected a number, got %s"
                % (dotted, _kind_of_value(value))
            )
        return float(value)
    if isinstance(default, str):
        if isinstance(value, str):
            return value
        raise ConfigError(
            "config key %s: expected a string, got %s"
            % (dotted, _kind_of_value(value))
        )
    raise ConfigError("config key %s: unsupported default type" % (dotted,))


# --------------------------------------------------------------------------- #
# environment overlay
# --------------------------------------------------------------------------- #


def _env_index() -> Dict[str, Tuple[str, ...]]:
    """Map every ``CHATCOL_*`` name to its config path (SPEC §9)."""
    index: Dict[str, Tuple[str, ...]] = {}

    def walk(node: Mapping[str, Any], prefix: Tuple[str, ...]) -> None:
        for key in node:
            value = node[key]
            path = prefix + (key,)
            if isinstance(value, dict):
                walk(value, path)
                continue
            name = ENV_PREFIX + "_".join(path).upper()
            if name in index:
                raise ConfigError(
                    "environment name collision for %s: %s and %s"
                    % (name, _dotted(index[name]), _dotted(path))
                )
            index[name] = path

    walk(DEFAULTS, ())
    return index


def _apply_env(cfg: Dict[str, Any], env: Mapping[str, str]) -> None:
    """Overlay ``CHATCOL_*`` variables onto `cfg` in place (SPEC §9).

    An unrecognised ``CHATCOL_*`` name is a :class:`ConfigError` for the same
    reason an unknown file key is: a typo that silently does nothing is the
    failure mode this validation exists to prevent.
    """
    index = _env_index()
    for name in sorted(env):
        if not name.startswith(ENV_PREFIX):
            continue
        path = index.get(name)
        if path is None:
            raise ConfigError("unknown environment override: %s" % (name,))
        default = _default_at(path)
        value = _coerce_env_value(env[name], default, name)
        node = cfg
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value


def _default_at(path: Tuple[str, ...]) -> Any:
    node: Any = DEFAULTS
    for key in path:
        node = node[key]
    return node


def _coerce_env_value(raw: str, default: Any, name: str) -> Any:
    """Coerce an environment string to the type of `default` (SPEC §9)."""
    if default is None:
        return None if raw.strip() == "" else raw
    if isinstance(default, bool):
        token = raw.strip().lower()
        if token in _TRUE_WORDS:
            return True
        if token in _FALSE_WORDS:
            return False
        raise ConfigError(
            "%s: expected a boolean (true/false/1/0), got a %d-character value"
            % (name, len(raw))
        )
    if isinstance(default, int):
        token = raw.strip()
        try:
            return int(token, 10)
        except ValueError:
            raise ConfigError(
                "%s: expected an integer, got a %d-character value" % (name, len(raw))
            ) from None
    if isinstance(default, float):
        token = raw.strip()
        try:
            return float(token)
        except ValueError:
            raise ConfigError(
                "%s: expected a number, got a %d-character value" % (name, len(raw))
            ) from None
    if isinstance(default, str):
        return raw
    raise ConfigError("%s: unsupported default type" % (name,))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _dotted(path: Tuple[str, ...]) -> str:
    return ".".join(path)


def _kind_of_value(value: Any) -> str:
    """Name the *type* of an offending value. Never renders the value itself."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return type(value).__name__
