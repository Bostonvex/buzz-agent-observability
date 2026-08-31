from __future__ import annotations

import unittest

from collector.schema import EventValidationError, validate_batch, validate_event
from tests.helpers import event


class EventSchemaTests(unittest.TestCase):
    def test_valid_event_is_normalized(self) -> None:
        submitted = event(observed_at="2026-08-31T08:00:00-04:00")
        validated = validate_event(submitted)
        self.assertEqual(validated["observed_at"], "2026-08-31T12:00:00.000Z")
        self.assertEqual(validated["schema_version"], 1)

    def test_unknown_top_level_field_is_rejected(self) -> None:
        submitted = event()
        submitted["surprise"] = "value"
        with self.assertRaisesRegex(EventValidationError, "unknown_field"):
            validate_event(submitted)

    def test_content_attribute_is_rejected(self) -> None:
        submitted = event(attributes={"content": "synthetic text"})
        with self.assertRaisesRegex(EventValidationError, "unknown_attribute"):
            validate_event(submitted)

    def test_secret_shaped_display_name_is_rejected(self) -> None:
        submitted = event(display_name="s" + "k-" + "x" * 30)
        with self.assertRaisesRegex(EventValidationError, "secret_like_value"):
            validate_event(submitted)

    def test_naive_timestamp_is_rejected(self) -> None:
        with self.assertRaisesRegex(EventValidationError, "timestamp_requires_timezone"):
            validate_event(event(observed_at="2026-08-31T12:00:00"))

    def test_event_attribute_allowlist_depends_on_event_type(self) -> None:
        submitted = event(attributes={"duration_ms": 10})
        with self.assertRaisesRegex(EventValidationError, "unknown_attribute"):
            validate_event(submitted)

    def test_cancellation_reason_is_strictly_enumerated(self) -> None:
        valid = validate_event(
            event("turn.cancelled", attributes={"cancellation_reason": "client_requested"})
        )
        self.assertEqual(valid["attributes"]["cancellation_reason"], "client_requested")
        with self.assertRaisesRegex(EventValidationError, "invalid_cancellation_reason"):
            validate_event(event("turn.cancelled", attributes={"cancellation_reason": "guess"}))

    def test_batch_is_bounded(self) -> None:
        with self.assertRaisesRegex(EventValidationError, "batch_too_large"):
            validate_batch([event() for _ in range(3)], maximum_events=2)


if __name__ == "__main__":
    unittest.main()
