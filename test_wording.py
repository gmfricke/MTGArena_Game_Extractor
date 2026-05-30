import unittest

from mtga_extract_plays import (
    object_pronoun,
    phrase_concede_result,
    phrase_death,
    phrase_life_change,
    phrase_player_action,
    phrase_zone_change,
    subject_pronoun,
)


class WordingTests(unittest.TestCase):
    def test_me_as_subject_becomes_i(self):
        self.assertEqual(
            phrase_player_action("Me", "cast", "Giada, Font of Hope"),
            "I cast Giada, Font of Hope",
        )

    def test_me_as_object_becomes_me(self):
        self.assertEqual(object_pronoun("Me"), "me")
        self.assertEqual(
            phrase_player_action("Opponent", "attack", "me with Serra's Emissary"),
            "Opponent attacks me with Serra's Emissary",
        )

    def test_opponent_wording_stays_third_person(self):
        self.assertEqual(subject_pronoun("Opponent"), "Opponent")
        self.assertEqual(
            phrase_player_action("Opponent", "cast", "Arcane Signet"),
            "Opponent casts Arcane Signet",
        )

    def test_life_change_wording(self):
        self.assertEqual(phrase_life_change("Me", 4, 29), "I gain 4 life (29)")
        self.assertEqual(
            phrase_life_change("Opponent", -2, 18),
            "Opponent loses 2 life (18)",
        )

    def test_death_wording(self):
        self.assertEqual(
            phrase_death("Opponent", "Inspiring Overseer"),
            "Opponent's Inspiring Overseer dies",
        )
        self.assertEqual(phrase_death("Me", "Giada, Font of Hope"), "My Giada, Font of Hope dies")
        self.assertEqual(phrase_death(None, "Angel"), "Angel dies")

    def test_passive_zone_change_wording_examples(self):
        self.assertEqual(
            phrase_zone_change(None, "exile", "Giada, Font of Hope"),
            "Giada, Font of Hope is exiled",
        )
        self.assertEqual(
            phrase_zone_change(None, "destroy", "Reliquary Tower"),
            "Reliquary Tower is destroyed",
        )
        self.assertEqual(
            phrase_zone_change("Path to Exile", "exile", "Giada, Font of Hope"),
            "Path to Exile exiles Giada, Font of Hope",
        )

    def test_concede_result_wording(self):
        self.assertEqual(
            phrase_concede_result("Me", "game"),
            ["Opponent concedes", "Winner: Me"],
        )
        self.assertEqual(
            phrase_concede_result("Me", "match"),
            ["Match result: Opponent conceded", "Winner: Me"],
        )
        self.assertEqual(
            phrase_concede_result("Opponent", "game"),
            ["I concede", "Winner: Opponent"],
        )


if __name__ == "__main__":
    unittest.main()
