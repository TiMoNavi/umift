from __future__ import annotations

import unittest

from umift_laptop_alignment.pipeline.alignment.runner import TimedRows, build_episodes


EMPTY = TimedRows(rows=[], timestamps=[])


def event(session_id: str, code: str, timestamp_ns: int) -> dict:
    return {
        "session_id": session_id,
        "aligned_monotonic_ns": timestamp_ns,
        "event": {"code": code},
    }


class AlignmentEpisodeGuardsTest(unittest.TestCase):
    def test_unmatched_start_is_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "unmatched recording events"):
            build_episodes(
                events=[event("session-a", "record_start", 100)],
                iphone=EMPTY,
                d435=EMPTY,
                coinft=EMPTY,
            )

    def test_stop_from_another_session_cannot_close_start(self) -> None:
        with self.assertRaisesRegex(SystemExit, "unmatched recording events"):
            build_episodes(
                events=[
                    event("session-a", "record_start", 100),
                    event("session-b", "record_stop", 200),
                ],
                iphone=EMPTY,
                d435=EMPTY,
                coinft=EMPTY,
            )

    def test_excessive_duration_is_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "invalid duration"):
            build_episodes(
                events=[
                    event("session-a", "record_start", 100),
                    event("session-a", "record_stop", 700_000_000_100),
                ],
                iphone=EMPTY,
                d435=EMPTY,
                coinft=EMPTY,
                max_episode_duration_s=600.0,
            )

    def test_duration_allows_record_stop_event_tolerance(self) -> None:
        episodes = build_episodes(
            events=[
                event("session-a", "record_start", 100),
                event("session-a", "record_stop", 600_500_000_100),
            ],
            iphone=EMPTY,
            d435=EMPTY,
            coinft=EMPTY,
            max_episode_duration_s=600.0,
        )

        self.assertEqual(len(episodes), 1)

    def test_overlapping_events_are_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "overlapping or out-of-order"):
            build_episodes(
                events=[
                    event("session-a", "record_start", 100),
                    event("session-a", "record_start", 150),
                    event("session-a", "record_stop", 200),
                    event("session-a", "record_stop", 250),
                ],
                iphone=EMPTY,
                d435=EMPTY,
                coinft=EMPTY,
            )

    def test_valid_session_produces_closed_episode(self) -> None:
        episodes = build_episodes(
            events=[
                event("session-a", "record_start", 100),
                event("session-a", "record_stop", 10_000_000_100),
            ],
            iphone=EMPTY,
            d435=EMPTY,
            coinft=EMPTY,
        )
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["session_id"], "session-a")
        self.assertIsNotNone(episodes[0]["stop_event"])


if __name__ == "__main__":
    unittest.main()
