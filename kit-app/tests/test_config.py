"""Config tests. The point of this module is that a typo fails loudly, so most of these
assert a *refusal* rather than a value."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config


def write(doc):
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(doc, fh)
    fh.close()
    return fh.name


class ConfigCase(unittest.TestCase):
    def tearDown(self):
        for path in getattr(self, "_paths", []):
            try:
                os.unlink(path)
            except OSError:
                pass

    def cfg_file(self, doc):
        path = write(doc)
        self._paths = getattr(self, "_paths", [])
        self._paths.append(path)
        return path

    def test_defaults_load_with_no_file(self):
        cfg = config.load_config(None, env={})
        self.assertEqual(cfg["policy"]["cooldown_days"], 21)
        self.assertTrue(cfg["screening"]["redact_coords"])

    def test_unknown_top_level_key_is_fatal(self):
        path = self.cfg_file({"polcy": {"cooldown_days": 3}})
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_config(path, env={})
        self.assertIn("polcy", str(ctx.exception))

    def test_unknown_nested_key_is_fatal(self):
        path = self.cfg_file({"policy": {"cooldown_dayz": 3}})
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_config(path, env={})
        self.assertIn("policy.cooldown_dayz", str(ctx.exception))

    def test_unknown_env_override_is_fatal(self):
        with self.assertRaises(config.ConfigError):
            config.load_config(None, env={"MELONKIT_POLICY_COOLDOWN_DAYZ": "3"})

    def test_token_env_var_is_not_mistaken_for_a_typo(self):
        """The token is deliberately outside the config surface, so it must not trip the
        unknown-override check that everything else does."""
        cfg = config.load_config(None, env={"MELONKIT_DISCORD_TOKEN": "x.y.z"})
        self.assertEqual(cfg["discord"]["token_env"], "MELONKIT_DISCORD_TOKEN")

    def test_env_overrides_are_typed(self):
        cfg = config.load_config(None, env={
            "MELONKIT_POLICY_COOLDOWN_DAYS": "7",
            "MELONKIT_VC_MIN_INTERVAL_S": "2.5",
            "MELONKIT_SCREENING_ENABLED": "false",
            "MELONKIT_DISCORD_GUILD_ID": "1234567890",
        })
        self.assertEqual(cfg["policy"]["cooldown_days"], 7)
        self.assertAlmostEqual(cfg["vc"]["min_interval_s"], 2.5)
        self.assertIs(cfg["screening"]["enabled"], False)
        self.assertEqual(cfg["discord"]["guild_id"], 1234567890)

    def test_non_numeric_env_override_is_fatal(self):
        with self.assertRaises(config.ConfigError):
            config.load_config(None, env={"MELONKIT_POLICY_COOLDOWN_DAYS": "soon"})

    def test_redact_coords_cannot_be_turned_off_while_screening(self):
        """The privacy guard. Chat excerpts carry third parties' base coordinates, so this
        combination must not be reachable by editing a config file."""
        path = self.cfg_file({"screening": {"enabled": True, "redact_coords": False}})
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_config(path, env={})
        self.assertIn("coordinates", str(ctx.exception))

    def test_user_agent_must_carry_a_contact(self):
        path = self.cfg_file({"vc": {"user_agent": "melon/1.0"}})
        with self.assertRaises(config.ConfigError):
            config.load_config(path, env={})

    def test_rate_limit_floor_is_enforced(self):
        path = self.cfg_file({"vc": {"min_interval_s": 0.05}})
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_config(path, env={})
        self.assertIn("globally", str(ctx.exception))

    def test_recent_chats_cannot_exceed_the_api_page_cap(self):
        path = self.cfg_file({"policy": {"recent_chats": 500}})
        with self.assertRaises(config.ConfigError):
            config.load_config(path, env={})

    def test_wrong_type_in_file_is_fatal(self):
        path = self.cfg_file({"policy": {"cooldown_days": "twenty one"}})
        with self.assertRaises(config.ConfigError):
            config.load_config(path, env={})

    def test_optional_keys_accept_null(self):
        path = self.cfg_file({"screening": {"lexicon_path": None},
                              "ops": {"log_path": None}})
        cfg = config.load_config(path, env={})
        self.assertIsNone(cfg["screening"]["lexicon_path"])

    def test_env_name_mapping(self):
        self.assertEqual(config.env_name("vc.min_interval_s"),
                         "MELONKIT_VC_MIN_INTERVAL_S")


if __name__ == "__main__":
    unittest.main()
