import unittest

from mtga_extract_games import (
    active_effect_for_resolved_permanent,
    ability_object_label,
    copied_object_label,
    death_label_or_none,
    object_pronoun,
    phrase_commander_cast_note,
    phrase_commander_damage,
    phrase_concede_result,
    phrase_choice_value,
    phrase_death,
    phrase_grouped_deaths,
    phrase_life_change,
    phrase_library_count,
    phrase_mulligan,
    phrase_mill_summary,
    phrase_player_has_counter,
    phrase_player_counter_change,
    phrase_player_action,
    phrase_result,
    phrase_zone_change,
    resolve_stack_name,
    is_low_fidelity_update_without_turn,
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

    def test_grouped_identical_token_deaths(self):
        self.assertEqual(
            phrase_grouped_deaths("Opponent", "Human", 2),
            "2 opponent Human tokens die",
        )
        self.assertEqual(phrase_grouped_deaths("Me", "Plant", 1), "My Plant dies")

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

    def test_result_wording_avoids_duplicate_labels(self):
        self.assertEqual(phrase_result("Opponent", "game", "game"), ["Winner: Opponent"])
        self.assertEqual(
            phrase_result("Opponent", "match", "game"),
            ["Match winner: Opponent"],
        )

    def test_lethal_life_loss_is_kept_before_winner_line(self):
        lines = [phrase_life_change("Me", -18, -3)]
        lines.extend(phrase_result("Opponent", "game", "game"))
        self.assertEqual(lines, ["I lose 18 life (-3)", "Winner: Opponent"])

    def test_numeric_choice_fallback_explains_domain(self):
        self.assertEqual(
            phrase_choice_value("creature type", 19, None),
            "unknown creature type 19",
        )
        self.assertEqual(phrase_choice_value("creature type", 1, "Angel"), "Angel")
        self.assertEqual(phrase_choice_value("creature type", 25, "Elemental"), "Elemental")

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

    def test_library_count_wording(self):
        self.assertEqual(phrase_library_count("Me", 1), "Me: 1 card")
        self.assertEqual(phrase_library_count("Opponent", 42), "Opponent: 42 cards")
        self.assertEqual(phrase_library_count("Player 1", None), "Player 1: unknown")

    def test_mulligan_wording_does_not_assume_hand_size(self):
        self.assertEqual(phrase_mulligan("Me"), "I mulligan")
        self.assertEqual(phrase_mulligan("Me", 7), "I mulligan (kept 7 cards)")
        self.assertEqual(
            phrase_mulligan("Opponent", 1),
            "Opponent mulligans (kept 1 card)",
        )

    def test_low_fidelity_update_without_turn_context_is_delayed(self):
        self.assertTrue(is_low_fidelity_update_without_turn({"update": "GameStateUpdate_Send"}))
        self.assertFalse(
            is_low_fidelity_update_without_turn(
                {"update": "GameStateUpdate_Send", "turnInfo": {"turnNumber": 8}}
            )
        )
        self.assertFalse(is_low_fidelity_update_without_turn({"update": "GameStateUpdate_SendHiFi"}))

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
        self.assertEqual(copied_object_label("Mesmeric Orb", True), "A copy of Mesmeric Orb")

    def test_ability_object_wording_uses_source_card(self):
        self.assertEqual(ability_object_label("Mesmeric Orb", True), "Mesmeric Orb trigger")
        self.assertEqual(ability_object_label("Cavern of Souls", False), "Cavern of Souls ability")

    def test_grouped_mill_wording_is_source_aware(self):
        self.assertEqual(
            phrase_mill_summary("Mesmeric Orb", "Me", 13),
            "Mesmeric Orb triggers resolve; I mill 13 cards",
        )
        self.assertEqual(
            phrase_mill_summary("Mesmeric Orb", "Opponent", 1),
            "Mesmeric Orb trigger resolves; Opponent mills 1 card",
        )
        self.assertEqual(
            phrase_mill_summary(None, None, 3),
            "A source triggers resolve; a player mills 3 cards",
        )

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
