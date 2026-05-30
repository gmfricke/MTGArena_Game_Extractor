import unittest

from mtga_extract_games import (
    active_effect_for_resolved_permanent,
    copied_object_label,
    death_label_or_none,
    object_pronoun,
    phrase_commander_cast_note,
    phrase_commander_damage,
    phrase_concede_result,
    phrase_death,
    phrase_life_change,
    phrase_player_has_counter,
    phrase_player_counter_change,
    phrase_player_action,
    phrase_zone_change,
    resolve_stack_name,
    should_emit_resolve_line,
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

    def test_commander_wording(self):
        self.assertEqual(
            phrase_commander_cast_note(2),
            "commander cast #2; next commander tax +4",
        )
        self.assertEqual(
            phrase_commander_damage("Giada, Font of Hope", 4, "Opponent", 8),
            "Commander damage: Giada, Font of Hope deals 4 to Opponent (8 total)",
        )
        self.assertEqual(
            phrase_commander_damage("Zacama, Primal Calamity", 7, "Me", 7),
            "Commander damage: Zacama, Primal Calamity deals 7 to me (7 total)",
        )

    def test_player_counter_wording(self):
        self.assertEqual(
            phrase_player_counter_change("Me", "poison", 1, 1),
            "I get 1 poison counter (1 total)",
        )
        self.assertEqual(
            phrase_player_counter_change("Opponent", "energy", 2, 2),
            "Opponent gets 2 energy counters (2 total)",
        )
        self.assertEqual(
            phrase_player_counter_change("Me", "experience", -1, 0),
            "I lose 1 experience counter (0 total)",
        )
        self.assertEqual(
            phrase_player_has_counter("Me", "poison", 6),
            "I have 6 poison counters",
        )

    def test_anonymous_resolve_suppression(self):
        self.assertFalse(should_emit_resolve_line("instance 729", 729))
        self.assertTrue(should_emit_resolve_line("Petty Theft", 729))

    def test_adventure_resolve_uses_stack_name(self):
        self.assertEqual(
            resolve_stack_name(561, "Brazen Borrower", {561: "Petty Theft"}),
            "Petty Theft",
        )
        self.assertEqual(resolve_stack_name(908, "Brazen Borrower", {}), "Brazen Borrower")

    def test_countered_spell_copy_wording_is_distinct(self):
        self.assertEqual(copied_object_label("Heartless Act", True), "A copy of Heartless Act")
        self.assertEqual(copied_object_label("Heartless Act", False), "Heartless Act")

    def test_leyline_active_effect_wording(self):
        self.assertEqual(
            active_effect_for_resolved_permanent("Leyline of the Void", "Opponent"),
            "Leyline of the Void exiles opponents' cards that would go to graveyard",
        )

    def test_unidentified_death_is_suppressed(self):
        self.assertIsNone(death_label_or_none("instance 913", 913))
        self.assertEqual(death_label_or_none("Spirit token", 913), "Spirit token")


if __name__ == "__main__":
    unittest.main()
