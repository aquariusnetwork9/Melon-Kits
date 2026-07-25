"""Unit tests for record.py — SPEC §2, §2.1, §4.2, §12.2, §12.3, §12.4.

No real chat text and no real coordinates appear anywhere in this file (SPEC §11.4);
every payload is obviously synthetic.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import record  # noqa: E402


# Synthetic fixtures. 'component' is a JSON string containing JSON, per SPEC §2 key 6.
UUID_MIXED = "069A79F4-44E9-4726-A5BE-FCA90E38AAF5"
UUID_LOWER = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
NAME = "TestPlayerOne"
CHAT = "hello"
COMPONENT = '{"text":"","extra":[{"text":"hello","color":"white"}]}'
COMPONENT_TRICKY = '{"text":"a \\"quoted\\" b","insertion":"c:\\\\path\\\\d"}'
TS_STR = "2026-07-25T20:37:13.404045Z"
INGEST_US = 1785013433404045


def sse_obj(**over):
    obj = {
        "time": TS_STR,
        "chat": CHAT,
        "playerName": NAME,
        "playerUuid": UUID_MIXED,
        "component": COMPONENT,
    }
    obj.update(over)
    return obj


def window_obj(**over):
    obj = {
        "time": TS_STR,
        "chat": CHAT,
        "playerName": NAME,
        "uuid": UUID_MIXED,
    }
    obj.update(over)
    return obj


class TestKeys(unittest.TestCase):
    """SPEC §2 — all 12 keys on every line, in exactly one order."""

    def test_keys_tuple_is_spec_order(self):
        self.assertEqual(
            record.KEYS,
            (
                "ts",
                "ts_us",
                "player_uuid",
                "player_name",
                "chat",
                "component",
                "src",
                "precision",
                "row_id",
                "ingest_ts",
                "seq",
                "batch",
            ),
        )
        self.assertEqual(len(record.KEYS), 12)

    def test_every_normalizer_emits_all_keys_in_order(self):
        recs = [
            record.from_sse(sse_obj(), seq=1, batch="c1", ingest_us=INGEST_US),
            record.from_window(window_obj(), seq=2, batch="g000001", ingest_us=INGEST_US),
            record.from_logline(
                1785012433000000,
                NAME,
                CHAT,
                seq=3,
                batch="latest.log@4096",
                ingest_us=INGEST_US,
            ),
        ]
        for rec in recs:
            self.assertEqual(tuple(rec.keys()), record.KEYS)
            self.assertEqual(set(rec.keys()), set(record.KEYS))

    def test_null_uuid_and_component_keys_still_present(self):
        rec = record.from_sse(
            sse_obj(playerUuid=None, component=None),
            seq=7,
            batch="c1",
            ingest_us=INGEST_US,
        )
        self.assertIsNone(rec["player_uuid"])
        self.assertIsNone(rec["component"])
        self.assertEqual(tuple(rec.keys()), record.KEYS)


class TestSourceFieldNames(unittest.TestCase):
    """SPEC §2.1 — playerUuid vs uuid, per source, never interchangeable."""

    def test_sse_reads_playerUuid(self):
        rec = record.from_sse(sse_obj(), seq=1, batch="c1", ingest_us=INGEST_US)
        self.assertEqual(rec["player_uuid"], UUID_LOWER)

    def test_window_reads_uuid(self):
        rec = record.from_window(window_obj(), seq=1, batch="g1", ingest_us=INGEST_US)
        self.assertEqual(rec["player_uuid"], UUID_LOWER)

    def test_sse_rejects_window_spelling(self):
        obj = sse_obj()
        del obj["playerUuid"]
        obj["uuid"] = UUID_MIXED
        with self.assertRaises(record.RecordError):
            record.from_sse(obj, seq=1, batch="c1", ingest_us=INGEST_US)

    def test_window_rejects_sse_spelling(self):
        obj = window_obj()
        del obj["uuid"]
        obj["playerUuid"] = UUID_MIXED
        with self.assertRaises(record.RecordError):
            record.from_window(obj, seq=1, batch="g1", ingest_us=INGEST_US)

    def test_missing_required_fields_raise(self):
        for field in ("time", "chat", "playerName", "playerUuid", "component"):
            obj = sse_obj()
            del obj[field]
            with self.assertRaises(record.RecordError):
                record.from_sse(obj, seq=1, batch="c1", ingest_us=INGEST_US)
        for field in ("time", "chat", "playerName", "uuid"):
            obj = window_obj()
            del obj[field]
            with self.assertRaises(record.RecordError):
                record.from_window(obj, seq=1, batch="g1", ingest_us=INGEST_US)

    def test_window_never_picks_up_a_component(self):
        rec = record.from_window(
            window_obj(component=COMPONENT), seq=1, batch="g1", ingest_us=INGEST_US
        )
        self.assertIsNone(rec["component"])

    def test_unparseable_timestamp_raises_recorderror(self):
        with self.assertRaises(record.RecordError):
            record.from_sse(sse_obj(time="not-a-timestamp"), seq=1, batch="c1",
                            ingest_us=INGEST_US)

    def test_errors_never_contain_the_record_body(self):
        """SPEC §11.4 — metadata only in every exception message."""
        secret = "SYNTHETICBODYMARKER"
        obj = sse_obj(chat=secret, component=secret, playerName=secret)
        del obj["playerUuid"]
        try:
            record.from_sse(obj, seq=1, batch="c1", ingest_us=INGEST_US)
        except record.RecordError as exc:
            self.assertNotIn(secret, str(exc))
        else:
            self.fail("expected RecordError")

        obj2 = sse_obj(time=secret)
        try:
            record.from_sse(obj2, seq=1, batch="c1", ingest_us=INGEST_US)
        except record.RecordError as exc:
            self.assertNotIn(secret, str(exc))
            # The tsutil message is not chained in: a chained traceback would print it.
            self.assertIsNone(exc.__cause__)
            self.assertTrue(exc.__suppress_context__)
        else:
            self.fail("expected RecordError")


class TestSrcAndPrecision(unittest.TestCase):
    """SPEC §2, §12.2, §12.3."""

    def test_src_values(self):
        self.assertEqual(record.SRC_SSE, "sse")
        self.assertEqual(record.SRC_BACKFILL, "rest-backfill")
        self.assertEqual(record.SRC_LOG, "proxy-log")

    def test_precision_us_for_sse_and_backfill(self):
        self.assertEqual(
            record.from_sse(sse_obj(), seq=1, batch="c1", ingest_us=INGEST_US)["precision"],
            "us",
        )
        self.assertEqual(
            record.from_window(window_obj(), seq=1, batch="g1", ingest_us=INGEST_US)[
                "precision"
            ],
            "us",
        )

    def test_precision_s_for_proxy_log(self):
        rec = record.from_logline(
            1785012433404045, NAME, CHAT, seq=1, batch="latest.log@0", ingest_us=INGEST_US
        )
        self.assertEqual(rec["precision"], "s")
        self.assertEqual(rec["src"], "proxy-log")
        self.assertIsNone(rec["player_uuid"])
        self.assertIsNone(rec["component"])
        # ts_us floored to the whole second the source actually provides.
        self.assertEqual(rec["ts_us"] % 1000000, 0)
        self.assertEqual(rec["ts_us"], 1785012433000000)
        self.assertTrue(rec["ts"].endswith(".000000Z"))


class TestRowId(unittest.TestCase):
    """SPEC §4.2, §12.4."""

    def test_shape(self):
        rid = record.row_id(1785012433404045, UUID_LOWER, NAME, CHAT)
        self.assertEqual(len(rid), 32)
        self.assertEqual(rid, rid.lower())
        int(rid, 16)

    def test_matches_spec_formula(self):
        import hashlib

        ts_us = 1785012433404045
        blob = b"\x1f".join(
            (str(ts_us).encode(), UUID_LOWER.encode("utf-8"), CHAT.encode("utf-8"))
        )
        self.assertEqual(
            record.row_id(ts_us, UUID_LOWER, NAME, CHAT),
            hashlib.sha256(blob).hexdigest()[:32],
        )

    def test_uuid_case_insensitive(self):
        self.assertEqual(
            record.row_id(1, UUID_MIXED, NAME, CHAT),
            record.row_id(1, UUID_LOWER, NAME, CHAT),
        )

    def test_null_uuid_falls_back_to_name(self):
        import hashlib

        ts_us = 1785012433404045
        blob = b"\x1f".join(
            (
                str(ts_us).encode(),
                ("name:" + NAME).encode("utf-8"),
                CHAT.encode("utf-8"),
            )
        )
        self.assertEqual(
            record.row_id(ts_us, None, NAME, CHAT),
            hashlib.sha256(blob).hexdigest()[:32],
        )
        # ... and the name is only consulted when the uuid is absent.
        self.assertEqual(
            record.row_id(ts_us, UUID_LOWER, NAME, CHAT),
            record.row_id(ts_us, UUID_LOWER, "SomeOtherName", CHAT),
        )
        self.assertNotEqual(
            record.row_id(ts_us, None, NAME, CHAT),
            record.row_id(ts_us, None, "SomeOtherName", CHAT),
        )

    def test_null_uuid_record_uses_name_fallback(self):
        rec = record.from_window(
            window_obj(uuid=None), seq=1, batch="g1", ingest_us=INGEST_US
        )
        self.assertIsNone(rec["player_uuid"])
        self.assertEqual(rec["row_id"], record.row_id(rec["ts_us"], None, NAME, CHAT))

    def test_empty_uuid_string_is_normalized_to_null(self):
        rec = record.from_window(
            window_obj(uuid="   "), seq=1, batch="g1", ingest_us=INGEST_US
        )
        self.assertIsNone(rec["player_uuid"])
        self.assertEqual(rec["row_id"], record.row_id(rec["ts_us"], None, NAME, CHAT))

    def test_distinct_inputs_give_distinct_ids(self):
        ts = 1785012433404045
        base = record.row_id(ts, UUID_LOWER, NAME, CHAT)
        variants = [
            record.row_id(ts + 1, UUID_LOWER, NAME, CHAT),          # different ts
            record.row_id(ts, "aaaaaaaa-44e9-4726-a5be-fca90e38aaf5", NAME, CHAT),
            record.row_id(ts, UUID_LOWER, NAME, "gg"),              # different chat
            record.row_id(ts, None, NAME, CHAT),                    # uuid vs name ident
            record.row_id(ts, None, "OtherName", CHAT),             # different name
        ]
        self.assertEqual(len(set(variants + [base])), len(variants) + 1)

    def test_unit_separator_prevents_field_smearing(self):
        # Without the \x1f join, ("ab","c") and ("a","bc") would collide.
        self.assertNotEqual(
            record.row_id(1, None, "ab", "c"),
            record.row_id(1, None, "a", "bc"),
        )

    def test_identical_instant_different_fractional_widths_same_row_id(self):
        """SPEC §4.2 — '.11' from one endpoint, '.110000' from another."""
        sse = record.from_sse(
            sse_obj(time="2026-07-25T20:37:13.110000Z"),
            seq=1,
            batch="c1",
            ingest_us=INGEST_US,
        )
        win = record.from_window(
            window_obj(time="2026-07-25T20:37:13.11Z"),
            seq=2,
            batch="g1",
            ingest_us=INGEST_US + 5,
        )
        self.assertEqual(sse["ts_us"], win["ts_us"])
        self.assertEqual(sse["row_id"], win["row_id"])
        self.assertEqual(sse["ts"], win["ts"])
        # The two rows differ only in provenance, never in identity.
        self.assertNotEqual(sse["src"], win["src"])

    def test_log_row_id_prefixed_and_never_equals_sse(self):
        """SPEC §4.2 / §8.5 — separate namespace."""
        ts_us = 1785012433000000
        log = record.from_logline(
            ts_us, NAME, CHAT, seq=1, batch="latest.log@0", ingest_us=INGEST_US
        )
        self.assertTrue(log["row_id"].startswith("L"))
        self.assertEqual(len(log["row_id"]), 33)
        plain = record.row_id(ts_us, None, NAME, CHAT)
        self.assertEqual(log["row_id"], "L" + plain)

        sse = record.from_sse(
            sse_obj(time="2026-07-25T20:47:13.000000Z", playerUuid=None),
            seq=2,
            batch="c1",
            ingest_us=INGEST_US,
        )
        # Same instant, same name, same text, no uuid: the underlying hash matches,
        # and only the 'L' prefix keeps the two namespaces apart.
        equivalent = record.from_logline(
            sse["ts_us"], NAME, CHAT, seq=3, batch="latest.log@1", ingest_us=INGEST_US
        )
        self.assertEqual(equivalent["row_id"], "L" + sse["row_id"])
        self.assertNotEqual(equivalent["row_id"], sse["row_id"])
        # No 32-hex row_id can ever start with 'L'.
        self.assertNotIn("l", sse["row_id"])
        self.assertNotIn("L", sse["row_id"])


class TestEncode(unittest.TestCase):
    """SPEC §2, §5.5."""

    def test_single_trailing_newline(self):
        line = record.encode(
            record.from_sse(sse_obj(), seq=1, batch="c1", ingest_us=INGEST_US)
        )
        self.assertIsInstance(line, bytes)
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(line.count(b"\n"), 1)
        self.assertFalse(line.startswith(b"\xef\xbb\xbf"))

    def test_embedded_newline_in_chat_stays_one_line(self):
        line = record.encode(
            record.from_sse(
                sse_obj(chat="line one\nline two\r\n"),
                seq=1,
                batch="c1",
                ingest_us=INGEST_US,
            )
        )
        self.assertEqual(line.count(b"\n"), 1)
        self.assertEqual(line.count(b"\r"), 0)
        self.assertEqual(json.loads(line.decode("utf-8"))["chat"], "line one\nline two\r\n")

    def test_key_order_on_the_wire(self):
        rec = record.from_sse(sse_obj(), seq=1, batch="c1", ingest_us=INGEST_US)
        text = record.encode(rec).decode("utf-8")
        positions = [text.index('"%s":' % k) for k in record.KEYS]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(
            list(json.loads(text, object_pairs_hook=lambda p: p)),
            [(k, rec[k]) for k in record.KEYS],
        )

    def test_compact_separators(self):
        text = record.encode(
            record.from_window(window_obj(), seq=1, batch="g1", ingest_us=INGEST_US)
        ).decode("utf-8")
        self.assertNotIn(", ", text)
        self.assertNotIn('": ', text)

    def test_roundtrip_all_keys_and_non_ascii(self):
        chat = "\u3053\u3093\u306b\u3061\u306f \u00e9\u00e8 \u2620 gg"
        name = "Sp\u00e4tzle_1"
        rec = record.from_sse(
            sse_obj(chat=chat, playerName=name), seq=42, batch="c417", ingest_us=INGEST_US
        )
        raw = record.encode(rec)
        # ensure_ascii=False: the characters are literally present, not \uXXXX escaped.
        self.assertIn(chat.encode("utf-8"), raw)
        self.assertNotIn(b"\\u", raw)
        back = json.loads(raw.decode("utf-8"))
        self.assertEqual(back, rec)
        self.assertEqual(set(back.keys()), set(record.KEYS))
        self.assertEqual(back["chat"], chat)
        self.assertEqual(back["player_name"], name)
        self.assertEqual(back["seq"], 42)
        self.assertEqual(back["batch"], "c417")
        self.assertIsInstance(back["ts_us"], int)

    def test_component_passes_through_byte_identically(self):
        """SPEC §2 key 6 / §12.10 — verbatim, never decode-and-re-encode."""
        rec = record.from_sse(
            sse_obj(component=COMPONENT_TRICKY), seq=1, batch="c1", ingest_us=INGEST_US
        )
        self.assertEqual(rec["component"], COMPONENT_TRICKY)
        back = json.loads(record.encode(rec).decode("utf-8"))
        self.assertEqual(back["component"], COMPONENT_TRICKY)
        # The stored value is still an undecoded JSON *string*.
        self.assertIsInstance(back["component"], str)
        self.assertEqual(
            json.loads(back["component"])["insertion"], "c:\\path\\d"
        )

    def test_null_batch_is_emitted(self):
        rec = record.from_sse(sse_obj(), seq=1, batch=None, ingest_us=INGEST_US)
        back = json.loads(record.encode(rec).decode("utf-8"))
        self.assertIn("batch", back)
        self.assertIsNone(back["batch"])

    def test_missing_or_extra_keys_rejected(self):
        rec = record.from_sse(sse_obj(), seq=1, batch="c1", ingest_us=INGEST_US)
        broken = dict(rec)
        del broken["precision"]
        with self.assertRaises(record.RecordError):
            record.encode(broken)
        extra = dict(rec)
        extra["oops"] = 1
        with self.assertRaises(record.RecordError):
            record.encode(extra)

    def test_ts_and_ts_us_always_agree(self):
        import tsutil

        for rec in (
            record.from_sse(sse_obj(), seq=1, batch="c1", ingest_us=INGEST_US),
            record.from_window(window_obj(), seq=1, batch="g1", ingest_us=INGEST_US),
            record.from_logline(
                1785012433404045, NAME, CHAT, seq=1, batch="l@0", ingest_us=INGEST_US
            ),
        ):
            self.assertEqual(rec["ts"], tsutil.fmt_ts(rec["ts_us"]))
            self.assertEqual(rec["ts_us"], tsutil.parse_ts(rec["ts"]))
            self.assertEqual(rec["ingest_ts"], tsutil.fmt_ts(INGEST_US))
            self.assertEqual(len(rec["ts"]), 27)
            self.assertTrue(rec["ts"].endswith("Z"))


class TestBadCallerArgs(unittest.TestCase):
    def test_seq_and_ingest_must_be_ints(self):
        with self.assertRaises(record.RecordError):
            record.from_sse(sse_obj(), seq="1", batch="c1", ingest_us=INGEST_US)
        with self.assertRaises(record.RecordError):
            record.from_sse(sse_obj(), seq=1, batch="c1", ingest_us=float(INGEST_US))
        with self.assertRaises(record.RecordError):
            record.from_logline(
                1785012433000000.0, NAME, CHAT, seq=1, batch="l@0", ingest_us=INGEST_US
            )

    def test_wrong_types_in_payload(self):
        with self.assertRaises(record.RecordError):
            record.from_sse(sse_obj(chat=123), seq=1, batch="c1", ingest_us=INGEST_US)
        with self.assertRaises(record.RecordError):
            record.from_sse(sse_obj(playerName=None), seq=1, batch="c1",
                            ingest_us=INGEST_US)
        with self.assertRaises(record.RecordError):
            record.from_sse(sse_obj(playerUuid=17), seq=1, batch="c1",
                            ingest_us=INGEST_US)
        with self.assertRaises(record.RecordError):
            record.from_sse(sse_obj(component=[1, 2]), seq=1, batch="c1",
                            ingest_us=INGEST_US)
        with self.assertRaises(record.RecordError):
            record.from_sse([], seq=1, batch="c1", ingest_us=INGEST_US)
        with self.assertRaises(record.RecordError):
            record.from_window(None, seq=1, batch="g1", ingest_us=INGEST_US)


if __name__ == "__main__":
    unittest.main()
