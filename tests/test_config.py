"""Tests for config.py (SPEC §9).

No network, no real chat text, no coordinates anywhere in this file.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402


class ConfigTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="chatcol-config-")
        # Strict env handling means a stray CHATCOL_* var from the operator's
        # shell would fail every test; snapshot and clear them.
        self._saved_env = dict(os.environ)
        for name in list(os.environ):
            if name.startswith(config.ENV_PREFIX):
                del os.environ[name]

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, obj) -> str:
        path = os.path.join(self.tmp, "collector.json")
        with open(path, "wb") as f:
            f.write(json.dumps(obj).encode("utf-8"))
        return path


class DefaultsTest(ConfigTestBase):
    def test_top_level_keys_match_spec_9(self) -> None:
        self.assertEqual(
            sorted(config.DEFAULTS),
            sorted(
                [
                    "data_dir",
                    "state_path",
                    "status_path",
                    "log_path",
                    "lock_path",
                    "user_agent",
                    "sse",
                    "backfill",
                    "log_tail",
                    "storage",
                    "dedupe",
                    "ops",
                ]
            ),
        )

    def test_section_keys_match_spec_9(self) -> None:
        expected = {
            "sse": [
                "enabled",
                "host",
                "path",
                "socket_read_timeout_s",
                "heartbeat_deadline_s",
                "initial_grace_s",
                "backoff_reset_s",
                "backoff_broken_max_s",
                "backoff_429_max_s",
            ],
            "backfill": [
                "enabled",
                "host",
                "path",
                "page_size",
                "min_interval_s",
                "request_timeout_s",
                "safety_lag_s",
                "second_pass_delay_s",
                "max_lookback_days",
                "backoff_429_max_s",
                "audit_enabled",
                "audit_interval_days",
                "audit_span_days",
                "audit_lag_days",
            ],
            "log_tail": [
                "enabled",
                "path",
                "poll_interval_s",
                "timezone",
                "schema_from_config_path",
            ],
            "storage": [
                "store_component",
                "fsync_interval_ms",
                "gzip_delay_s",
                "gzip_level",
                "max_file_bytes",
            ],
            "dedupe": ["window_s", "max_keys", "rehydrate_max_bytes"],
            "ops": [
                "status_interval_s",
                "alert_command",
                "log_level",
                "log_max_bytes",
                "log_backup_count",
            ],
        }
        for section in expected:
            self.assertEqual(
                sorted(config.DEFAULTS[section]), sorted(expected[section]), section
            )

    def test_spot_check_default_values(self) -> None:
        d = config.DEFAULTS
        self.assertEqual(d["data_dir"], "./corpus")
        self.assertEqual(d["state_path"], "./corpus/state.json")
        self.assertTrue(d["sse"]["enabled"])
        self.assertEqual(d["sse"]["host"], "api.2b2t.vc")
        self.assertEqual(d["sse"]["path"], "/feed/chats")
        self.assertEqual(d["sse"]["socket_read_timeout_s"], 20)
        self.assertEqual(d["sse"]["heartbeat_deadline_s"], 90)
        self.assertEqual(d["sse"]["initial_grace_s"], 120)
        self.assertEqual(d["backfill"]["page_size"], 100)
        self.assertEqual(d["backfill"]["min_interval_s"], 1.0)
        self.assertEqual(d["backfill"]["safety_lag_s"], 120)
        self.assertEqual(d["backfill"]["second_pass_delay_s"], 5400)
        self.assertEqual(d["backfill"]["max_lookback_days"], 7)
        self.assertTrue(d["backfill"]["audit_enabled"])
        self.assertFalse(d["log_tail"]["enabled"])  # SPEC §8: off by default
        self.assertIsNone(d["log_tail"]["timezone"])
        self.assertIsNone(d["log_tail"]["schema_from_config_path"])
        self.assertTrue(d["storage"]["store_component"])  # SPEC §11.1
        self.assertEqual(d["storage"]["fsync_interval_ms"], 2000)
        self.assertEqual(d["storage"]["gzip_delay_s"], 3600)
        self.assertEqual(d["storage"]["gzip_level"], 6)
        self.assertEqual(d["storage"]["max_file_bytes"], 0)
        self.assertEqual(d["dedupe"]["window_s"], 21600)
        self.assertEqual(d["dedupe"]["max_keys"], 400000)
        self.assertEqual(d["dedupe"]["rehydrate_max_bytes"], 268435456)
        self.assertEqual(d["ops"]["status_interval_s"], 15)
        self.assertIsNone(d["ops"]["alert_command"])
        self.assertEqual(d["ops"]["log_level"], "INFO")
        self.assertEqual(d["ops"]["log_max_bytes"], 10485760)
        self.assertEqual(d["ops"]["log_backup_count"], 5)

    def test_load_without_path_returns_a_deep_copy(self) -> None:
        cfg = config.load_config(None)
        self.assertEqual(cfg, config.DEFAULTS)
        cfg["sse"]["host"] = "127.0.0.1"
        cfg["dedupe"]["window_s"] = 1
        self.assertEqual(config.DEFAULTS["sse"]["host"], "api.2b2t.vc")
        self.assertEqual(config.DEFAULTS["dedupe"]["window_s"], 21600)

    def test_default_argument_is_optional(self) -> None:
        self.assertEqual(config.load_config(), config.DEFAULTS)


class FileMergeTest(ConfigTestBase):
    def test_file_overrides_only_named_keys(self) -> None:
        path = self.write_config(
            {"data_dir": "/srv/corpus", "sse": {"heartbeat_deadline_s": 45}}
        )
        cfg = config.load_config(path)
        self.assertEqual(cfg["data_dir"], "/srv/corpus")
        self.assertEqual(cfg["sse"]["heartbeat_deadline_s"], 45)
        self.assertEqual(cfg["sse"]["host"], "api.2b2t.vc")
        self.assertEqual(cfg["ops"]["log_level"], "INFO")

    def test_unknown_top_level_key_raises(self) -> None:
        path = self.write_config({"data_dirr": "/srv/corpus"})
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_config(path)
        self.assertIn("data_dirr", str(ctx.exception))

    def test_unknown_nested_key_raises(self) -> None:
        path = self.write_config({"backfill": {"min_intervall_s": 2.0}})
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_config(path)
        self.assertIn("backfill.min_intervall_s", str(ctx.exception))

    def test_unknown_deeply_nested_key_raises(self) -> None:
        path = self.write_config({"log_tail": {"dryrun": True}})
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_config(path)
        self.assertIn("log_tail.dryrun", str(ctx.exception))

    def test_bool_type_mismatch_raises(self) -> None:
        for bad in ("yes", 1, 0, None, 1.0):
            path = self.write_config({"sse": {"enabled": bad}})
            with self.assertRaises(config.ConfigError):
                config.load_config(path)

    def test_int_type_mismatch_raises(self) -> None:
        for bad in ("20", 20.5, True, None):
            path = self.write_config({"sse": {"socket_read_timeout_s": bad}})
            with self.assertRaises(config.ConfigError):
                config.load_config(path)

    def test_str_type_mismatch_raises(self) -> None:
        path = self.write_config({"sse": {"host": 1234}})
        with self.assertRaises(config.ConfigError):
            config.load_config(path)

    def test_section_must_be_an_object(self) -> None:
        path = self.write_config({"sse": 5})
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_config(path)
        self.assertIn("sse", str(ctx.exception))

    def test_int_is_accepted_for_a_float_default_and_widened(self) -> None:
        path = self.write_config({"backfill": {"min_interval_s": 3}})
        cfg = config.load_config(path)
        self.assertIsInstance(cfg["backfill"]["min_interval_s"], float)
        self.assertEqual(cfg["backfill"]["min_interval_s"], 3.0)

    def test_bool_is_rejected_for_a_float_default(self) -> None:
        path = self.write_config({"backfill": {"min_interval_s": True}})
        with self.assertRaises(config.ConfigError):
            config.load_config(path)

    def test_nullable_key_accepts_string_or_null(self) -> None:
        path = self.write_config(
            {"ops": {"alert_command": "/usr/local/bin/alert.sh"},
             "log_tail": {"timezone": None}}
        )
        cfg = config.load_config(path)
        self.assertEqual(cfg["ops"]["alert_command"], "/usr/local/bin/alert.sh")
        self.assertIsNone(cfg["log_tail"]["timezone"])

    def test_nullable_key_rejects_wrong_type(self) -> None:
        path = self.write_config({"ops": {"alert_command": 7}})
        with self.assertRaises(config.ConfigError):
            config.load_config(path)

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.load_config(os.path.join(self.tmp, "absent.json"))

    def test_malformed_json_raises(self) -> None:
        path = os.path.join(self.tmp, "collector.json")
        with open(path, "wb") as f:
            f.write(b'{"data_dir": ')
        with self.assertRaises(config.ConfigError):
            config.load_config(path)

    def test_non_object_json_raises(self) -> None:
        path = os.path.join(self.tmp, "collector.json")
        with open(path, "wb") as f:
            f.write(b"[]")
        with self.assertRaises(config.ConfigError):
            config.load_config(path)


class EnvOverrideTest(ConfigTestBase):
    def test_env_name_helper(self) -> None:
        self.assertEqual(
            config.env_name("backfill.min_interval_s"),
            "CHATCOL_BACKFILL_MIN_INTERVAL_S",
        )
        self.assertEqual(config.env_name("sse.enabled"), "CHATCOL_SSE_ENABLED")
        self.assertEqual(config.env_name("data_dir"), "CHATCOL_DATA_DIR")

    def test_nested_env_override_coerces_float(self) -> None:
        os.environ["CHATCOL_BACKFILL_MIN_INTERVAL_S"] = "3.5"
        cfg = config.load_config(None)
        self.assertIsInstance(cfg["backfill"]["min_interval_s"], float)
        self.assertEqual(cfg["backfill"]["min_interval_s"], 3.5)

    def test_nested_env_override_coerces_int(self) -> None:
        os.environ["CHATCOL_SSE_HEARTBEAT_DEADLINE_S"] = " 45 "
        cfg = config.load_config(None)
        self.assertIsInstance(cfg["sse"]["heartbeat_deadline_s"], int)
        self.assertEqual(cfg["sse"]["heartbeat_deadline_s"], 45)

    def test_env_override_coerces_str(self) -> None:
        os.environ["CHATCOL_DATA_DIR"] = "/srv/corpus"
        os.environ["CHATCOL_USER_AGENT"] = "chat-corpus-collector/1.0 (+contact: op)"
        cfg = config.load_config(None)
        self.assertEqual(cfg["data_dir"], "/srv/corpus")
        self.assertEqual(
            cfg["user_agent"], "chat-corpus-collector/1.0 (+contact: op)"
        )

    def test_bool_coercion_every_accepted_spelling(self) -> None:
        for raw in ("true", "TRUE", "True", "tRuE", "1", " 1 "):
            os.environ["CHATCOL_SSE_ENABLED"] = raw
            self.assertIs(config.load_config(None)["sse"]["enabled"], True, raw)
        for raw in ("false", "FALSE", "False", "fAlSe", "0", " 0 "):
            os.environ["CHATCOL_SSE_ENABLED"] = raw
            self.assertIs(config.load_config(None)["sse"]["enabled"], False, raw)

    def test_bool_coercion_rejects_anything_else(self) -> None:
        for raw in ("yes", "no", "on", "off", "", "2", "trueish"):
            os.environ["CHATCOL_LOG_TAIL_ENABLED"] = raw
            with self.assertRaises(config.ConfigError):
                config.load_config(None)

    def test_int_coercion_rejects_non_integers(self) -> None:
        for raw in ("abc", "1.5", "", " "):
            os.environ["CHATCOL_DEDUPE_WINDOW_S"] = raw
            with self.assertRaises(config.ConfigError):
                config.load_config(None)

    def test_float_coercion_rejects_non_numbers(self) -> None:
        os.environ["CHATCOL_BACKFILL_MIN_INTERVAL_S"] = "soon"
        with self.assertRaises(config.ConfigError):
            config.load_config(None)

    def test_empty_string_means_null_for_nullable_keys(self) -> None:
        os.environ["CHATCOL_OPS_ALERT_COMMAND"] = ""
        os.environ["CHATCOL_LOG_TAIL_TIMEZONE"] = "   "
        cfg = config.load_config(None)
        self.assertIsNone(cfg["ops"]["alert_command"])
        self.assertIsNone(cfg["log_tail"]["timezone"])

    def test_nullable_key_takes_a_string(self) -> None:
        os.environ["CHATCOL_LOG_TAIL_TIMEZONE"] = "Etc/UTC"
        cfg = config.load_config(None)
        self.assertEqual(cfg["log_tail"]["timezone"], "Etc/UTC")

    def test_env_overrides_file_which_overrides_defaults(self) -> None:
        path = self.write_config(
            {"dedupe": {"window_s": 7200}, "ops": {"log_level": "DEBUG"}}
        )
        os.environ["CHATCOL_DEDUPE_WINDOW_S"] = "10800"
        cfg = config.load_config(path)
        self.assertEqual(cfg["dedupe"]["window_s"], 10800)  # env wins
        self.assertEqual(cfg["ops"]["log_level"], "DEBUG")  # file wins over default
        self.assertEqual(cfg["dedupe"]["max_keys"], 400000)  # default survives

    def test_env_can_null_a_value_the_file_set(self) -> None:
        path = self.write_config({"ops": {"alert_command": "/usr/local/bin/a.sh"}})
        os.environ["CHATCOL_OPS_ALERT_COMMAND"] = ""
        cfg = config.load_config(path)
        self.assertIsNone(cfg["ops"]["alert_command"])

    def test_unknown_env_override_raises(self) -> None:
        os.environ["CHATCOL_BAKFILL_MIN_INTERVAL_S"] = "2.0"
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_config(None)
        self.assertIn("CHATCOL_BAKFILL_MIN_INTERVAL_S", str(ctx.exception))

    def test_non_prefixed_env_is_ignored(self) -> None:
        os.environ["DATA_DIR"] = "/nope"
        cfg = config.load_config(None)
        self.assertEqual(cfg["data_dir"], "./corpus")

    def test_every_leaf_key_has_a_unique_env_name(self) -> None:
        index = config._env_index()

        def leaves(node, prefix):
            count = 0
            for key in node:
                if isinstance(node[key], dict):
                    count += leaves(node[key], prefix + (key,))
                else:
                    count += 1
            return count

        self.assertEqual(len(index), leaves(config.DEFAULTS, ()))
        for name in index:
            self.assertTrue(name.startswith(config.ENV_PREFIX))


if __name__ == "__main__":
    unittest.main()
