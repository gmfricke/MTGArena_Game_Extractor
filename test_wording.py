import unittest
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from argparse import Namespace
from contextlib import redirect_stdout
from unittest.mock import patch
from collections import Counter
from pathlib import Path

from mtga_extract_games import (
    active_effect_for_resolved_permanent,
    activated_ability_lock_types,
    annotation_details,
    action_mana_payment_options,
    apply_authoritative_library_count,
    attached_to_player_summary_parts,
    ability_source_instance_id,
    ability_object_label,
    attack_phrase,
    attachment_summary_parts,
    append_target_phrase,
    archive_seen_games,
    archived_transcript_matches,
    adventure_exile_line,
    available_resource_lines,
    base_cast_name,
    build_choice_value_names,
    build_card_name_colors,
    build_card_name_pattern,
    card_is_land,
    card_is_nonland_permanent,
    colorize_transcript_line,
    clean_localized_enum_name,
    concise_engine_keyword_labels,
    combine_adjacent_attack_lines,
    combine_duplicate_transcript_lines,
    combine_repeated_damage_life_sequences,
    compact_counted_name,
    candidate_windows_paths,
    counter_summary_suffix,
    counter_summary_parts,
    capped_power_toughness_counter_amount,
    copied_object_label,
    death_label_or_none,
    damage_mark_suffix,
    default_archive_db_path,
    disambiguated_object_label,
    emblem_summary_line,
    engine_keyword_summary,
    extract_game_plays,
    effect_power_toughness_modifier,
    find_card_database,
    is_hidden_arena_object,
    find_player_logs,
    load_ability_texts,
    load_enum_value_names,
    load_grp_id_to_metadata,
    load_keyword_value_names,
    land_mana_options,
    library_delta_for_zone_transfer,
    library_movement_description,
    life_change_group_source,
    mana_sources_from_option,
    mana_cost_amount,
    mana_cost_text_amount,
    merge_archive_databases,
    mana_options_from_ability_texts,
    mana_pool_count,
    mana_restriction_from_ability_texts,
    mana_spec_types_from_option,
    match_room_player_names,
    move_copy_resolves_after_revealed_returns,
    non_mana_tap_events,
    object_pronoun,
    object_matches_any_card_type,
    ownership_summary_part,
    parse_carddb_int_list,
    phase_section_label,
    power_toughness_summary,
    player_log_paths_for_reading,
    phrase_commander_cast_note,
    phrase_concede_result,
    phrase_damage,
    phrase_choice_value,
    phrase_death,
    phrase_enters_attacking,
    phrase_draw,
    phrase_grouped_deaths,
    phrase_incomplete_game_notice,
    phrase_life_change,
    phrase_life_change_group,
    phrase_life_change_summary,
    phrase_library_count,
    phrase_keyword_choice_value,
    phrase_keyword_choice_values,
    phrase_mulligan,
    phrase_mana_tap,
    phrase_power_toughness_mod,
    phrase_move_to_zone,
    phrase_mill_summary,
    preferred_zone_transfer_name,
    reconcile_zone_transfer_objects,
    phrase_player_has_counter,
    phrase_player_counter_change,
    phrase_player_action,
    phrase_result,
    phrase_sacrifice,
    phrase_scry,
    phrase_shuffle,
    phrase_source_action,
    phrase_zone_change,
    find_target_like_paths,
    format_available_mana_summary,
    format_cast_mana_note,
    format_card_mana_cost,
    format_mana_cost_entries,
    format_mana_pool,
    format_path_diagnostics,
    format_game_type,
    format_target_phrase,
    game_has_commanders,
    grouped_name_phrase,
    has_live_selection_conflict,
    is_placeholder_transcript_lines,
    modifier_summary_suffix,
    remove_redundant_match_winner_lines,
    remove_near_duplicate_cast_lines,
    remove_repeated_cast_blocks,
    resolve_stack_name,
    replace_base_cast_name,
    resolve_input_paths,
    resource_mechanics_for_zone,
    resource_mechanics_from_text,
    scaled_power_toughness_counter,
    select_transcript_matches,
    should_group_life_change_source,
    is_low_fidelity_update_without_turn,
    should_infer_missing_cast_before_resolve,
    should_carry_cast_marker,
    should_emit_resolve_line,
    state_player_heading,
    state_player_label,
    state_zone_label,
    subject_pronoun,
    token_board_name,
    colorize_land_names,
    should_color_output,
    transcript_line_perspective,
    transcript_diagnostics,
    transcript_line_style,
)


class WordingTests(unittest.TestCase):
    def test_me_as_subject_becomes_i(self):
        """Check me as subject becomes i."""
        self.assertEqual(state_player_label("Me"), "I")
        self.assertEqual(
            phrase_player_action("Me", "cast", "Giada, Font of Hope"),
            "I cast Giada, Font of Hope",
        )

    def test_transcript_line_color_helpers(self):
        """Check transcript line color helpers."""
        self.assertEqual(transcript_line_perspective("I cast Giada, Font of Hope"), "me")
        self.assertEqual(transcript_line_perspective("My hand: Plains"), "me")
        self.assertEqual(transcript_line_perspective("My board:"), "me")
        self.assertIsNone(transcript_line_perspective("  Hand: Plains"))
        self.assertEqual(
            transcript_line_perspective("Opponent casts Arcane Signet"),
            "opponent",
        )
        self.assertEqual(transcript_line_perspective("Opponent's hand: unknown card"), "opponent")
        self.assertIsNone(transcript_line_perspective("Game type: Constructed Duel"))
        self.assertEqual(transcript_line_style("===== GAME 1: MATCH abc ====="), "game_header")
        self.assertEqual(transcript_line_style("Game type: Constructed Duel"), "metadata")
        self.assertEqual(transcript_line_style("-- Combat damage --"), "metadata")
        self.assertEqual(transcript_line_style("=== Turn 1: Me ==="), "me_header")
        self.assertEqual(transcript_line_style("=== Turn 2: Opponent ==="), "opponent_header")
        self.assertEqual(transcript_line_style("  Hand: Plains"), "state_detail")
        self.assertEqual(transcript_line_style("Current State:"), "state")
        self.assertEqual(
            transcript_line_style("Bishop of Wings trigger: I gain 4 life (28)"),
            "state",
        )
        self.assertEqual(transcript_line_style("Winner: Me"), "result_me")
        self.assertEqual(transcript_line_style("Winner: Opponent"), "result_opponent")
        self.assertFalse(should_color_output("never", True))
        self.assertFalse(should_color_output("auto", False))
        self.assertTrue(should_color_output("auto", True))
        self.assertTrue(should_color_output("always", False))
        self.assertEqual(phase_section_label({"phase": "Phase_Main1"}), "Pre-combat main")
        self.assertEqual(
            phase_section_label({"phase": "Phase_Beginning", "step": "Step_Untap"}),
            "Untap",
        )
        self.assertEqual(
            phase_section_label({"phase": "Phase_Combat", "step": "Step_DeclareBlock"}),
            "Declare blockers",
        )
        self.assertEqual(
            phase_section_label({"phase": "Phase_Ending", "step": "Step_Cleanup"}),
            "Cleanup",
        )
        self.assertIn("\033[36m", colorize_transcript_line("I cast Giada", True))
        self.assertNotIn(
            "\033[1;34mTurn",
            colorize_transcript_line("=== Turn 1: Me ===", True),
        )
        self.assertIn("\033[1;37mPlains", colorize_land_names("I play Plains", True))
        self.assertIn("\033[90mSwamp", colorize_land_names("Opponent plays Swamp", True))
        self.assertIn(
            "\033[37mNykthos, Shrine to Nyx",
            colorize_land_names("I play Nykthos, Shrine to Nyx", True),
        )
        wind_crag = colorize_land_names("Opponent plays Wind-Scarred Crag", True)
        self.assertIn("\033[1;35mWind-Scarred Crag", wind_crag)
        self.assertIn(
            "\033[1;32mSnow-Covered Forest",
            colorize_land_names("I play Snow-Covered Forest", True),
        )
        self.assertEqual(colorize_transcript_line("I cast Giada", False), "I cast Giada")

    def test_card_name_color_helpers_use_card_metadata(self):
        """Check card name color helpers use card metadata."""
        metadata = {
            1: {
                "name": "Giada, Font of Hope",
                "type_numbers": {2},
                "colors": {1},
                "color_identity": {1},
                "frame_colors": {1},
            },
            2: {
                "name": "Empyrean Eagle",
                "type_numbers": {2},
                "colors": {1, 2},
                "color_identity": {1, 2},
                "frame_colors": {1, 2},
            },
            3: {
                "name": "Ornithopter",
                "type_numbers": {1, 2},
                "colors": set(),
                "color_identity": set(),
                "frame_colors": set(),
            },
            4: {
                "name": "Wind-Scarred Crag",
                "type_numbers": {5},
                "colors": set(),
                "color_identity": {1, 4},
                "frame_colors": {1, 4},
            },
            5: {
                "name": "Goldvein Pick",
                "type_numbers": {1},
                "colors": set(),
                "color_identity": set(),
                "frame_colors": set(),
            },
            6: {
                "name": "Valorous Stance",
                "type_numbers": {4},
                "colors": {1},
                "color_identity": {1},
                "frame_colors": {1},
            },
        }
        name_colors = build_card_name_colors(metadata)
        name_pattern = build_card_name_pattern(name_colors)
        line = colorize_transcript_line(
            "I attack Opponent with Giada, Font of Hope and Empyrean Eagle",
            True,
            name_colors,
            name_pattern,
        )
        self.assertIn("\033[1;37mGiada, Font of Hope", line)
        self.assertIn("\033[1;36mEmpyrean Eagle", line)
        self.assertIn("\033[37mand\033[0m", line)
        semicolon_line = colorize_transcript_line(
            "I attack Opponent with Giada, Font of Hope; Healer's Hawk; and Empyrean Eagle",
            True,
            name_colors,
            name_pattern,
        )
        self.assertIn("\033[37mand\033[0m", semicolon_line)
        self.assertIn(
            "\033[37mOrnithopter",
            colorize_transcript_line("Opponent casts Ornithopter", True, name_colors, name_pattern),
        )
        wind_crag = colorize_transcript_line(
            "Opponent plays Wind-Scarred Crag",
            True,
            name_colors,
            name_pattern,
        )
        self.assertIn("\033[1;35mWind-Scarred Crag", wind_crag)
        self.assertIn(
            "\033[37mGoldvein Pick",
            colorize_transcript_line("Opponent casts Goldvein Pick", True, name_colors, name_pattern),
        )
        self.assertIn(
            "\033[1;37mValorous Stance",
            colorize_transcript_line(
                "Opponent casts Valorous Stance targeting Giada, Font of Hope",
                True,
                name_colors,
                name_pattern,
            ),
        )

    def test_turn_state_uses_possessive_labels(self):
        """Check turn state uses possessive labels."""
        self.assertEqual(state_zone_label("Me", "board"), "My board")
        self.assertEqual(state_zone_label("Opponent", "hand"), "Opponent's hand")
        self.assertEqual(state_zone_label("Player 1", "library"), "Player 1's library")
        self.assertEqual(state_player_heading("Me"), "My board")
        self.assertEqual(state_player_heading("Opponent"), "Opponent's board")
        self.assertEqual(state_player_heading("Player 1"), "Player 1's board")

    def test_zone_transfer_name_prefers_annotation_grpid(self):
        """Check zone transfer name prefers annotation grpid."""
        self.assertEqual(
            preferred_zone_transfer_name("Blind Obedience", "Grand Abolisher"),
            "Blind Obedience",
        )
        self.assertEqual(
            preferred_zone_transfer_name("grpId 12345", "Grand Abolisher"),
            "Grand Abolisher",
        )
        self.assertEqual(
            replace_base_cast_name(
                "Grand Abolisher from command zone; commander tax 2",
                "Blind Obedience",
            ),
            "Blind Obedience from command zone; commander tax 2",
        )

    def test_zone_transfer_annotations_reconcile_cached_object_zones(self):
        """Annotation-only moves update cached zones in event order."""
        objects = {17: {"instanceId": 17, "zoneId": 3}, 18: {"instanceId": 18, "zoneId": 3}}
        annotations = [
            {
                "type": ["AnnotationType_ZoneTransfer"],
                "affectedIds": [17],
                "details": [{"key": "zone_dest", "valueInt32": [7]}],
            },
            {
                "type": ["AnnotationType_ZoneTransfer"],
                "affectedIds": [17],
                "details": [{"key": "zone_dest", "valueInt32": [9]}],
            },
        ]

        self.assertEqual(annotation_details(annotations[0]["details"])["zone_dest"], 7)
        known_zones = reconcile_zone_transfer_objects(objects, annotations)

        self.assertEqual(objects[17]["zoneId"], 9)
        self.assertEqual(objects[18]["zoneId"], 3)
        objects[17] = {"instanceId": 17, "zoneId": 3}
        reconcile_zone_transfer_objects(objects, [], known_zones)
        self.assertEqual(objects[17]["zoneId"], 9)

    def test_combat_labels_number_identical_objects(self):
        """Same-name combatants get stable instance-order numbers."""
        objects = {
            12: {"zoneId": 4, "controllerSeatId": 2},
            10: {"zoneId": 4, "controllerSeatId": 2},
            14: {"zoneId": 4, "controllerSeatId": 1},
        }
        labels = {10: "Human", 12: "Human", 14: "Human"}

        self.assertEqual(disambiguated_object_label(10, objects, labels.get), "Human #1")
        self.assertEqual(disambiguated_object_label(12, objects, labels.get), "Human #2")
        self.assertEqual(disambiguated_object_label(14, objects, labels.get), "Human")

    def test_non_mana_tap_events_exclude_mana_and_combat_taps(self):
        """Only sourced effect taps survive mana/combat noise filtering."""
        annotations = [
            {
                "type": ["AnnotationType_TappedUntappedPermanent"],
                "affectorId": 90,
                "affectedIds": [20],
                "details": [{"key": "tapped", "valueInt32": [1]}],
            },
            {"type": ["AnnotationType_ManaPaid"], "affectorId": 20, "affectedIds": [99]},
            {
                "type": ["AnnotationType_TappedUntappedPermanent"],
                "affectorId": 91,
                "affectedIds": [21],
                "details": [{"key": "tapped", "valueInt32": [1]}],
            },
            {
                "type": ["AnnotationType_TappedUntappedPermanent"],
                "affectedIds": [22],
                "details": [{"key": "tapped", "valueInt32": [1]}],
            },
        ]

        self.assertEqual(non_mana_tap_events(annotations), [(91, 21, True)])

    def test_adjacent_duplicate_transcript_lines_are_combined(self):
        """Check adjacent duplicate transcript lines are combined."""
        lines = [
            "=== Turn 4: Opponent ===",
            "Opponent attacks me with Tentacle",
            "Opponent attacks me with Tentacle",
            "Opponent attacks me with Tentacle",
            "Jerren, Corrupted Bishop blocks Tentacle",
            "Opponent discards Island",
            "Opponent discards Island",
            "Winner: Opponent",
            "Winner: Opponent",
        ]
        self.assertEqual(
            combine_duplicate_transcript_lines(lines),
            [
                "=== Turn 4: Opponent ===",
                "3x Opponent attacks me with Tentacle",
                "Jerren, Corrupted Bishop blocks Tentacle",
                "2x Opponent discards Island",
                "Winner: Opponent",
                "Winner: Opponent",
            ],
        )

    def test_near_duplicate_cast_lines_are_removed(self):
        """Check repeated cast echoes around phase headings are suppressed."""
        self.assertEqual(
            remove_near_duplicate_cast_lines(
                [
                    "I cast Steel Seraph",
                    "",
                    "-- Beginning of combat --",
                    "I cast Steel Seraph",
                    "I choose flying for Steel Seraph (keyword)",
                ]
            ),
            [
                "I cast Steel Seraph",
                "",
                "-- Beginning of combat --",
                "I choose flying for Steel Seraph (keyword)",
            ],
        )

    def test_repeated_cast_blocks_are_removed(self):
        """Check adjacent repeated cast blocks from low-fidelity updates are suppressed."""
        self.assertEqual(
            remove_repeated_cast_blocks(
                [
                    "I draw Forest",
                    "I cast Sarkhan, Soul Aflame",
                    "I cast Ziatora, the Incinerator",
                    "I cast Sarkhan, Soul Aflame",
                    "I cast Ziatora, the Incinerator",
                    "Opponent concedes",
                ]
            ),
            [
                "I draw Forest",
                "I cast Sarkhan, Soul Aflame",
                "I cast Ziatora, the Incinerator",
                "Opponent concedes",
            ],
        )

    def test_placeholder_transcript_lines_are_detected(self):
        """Check Arena shell records are detected as placeholder transcripts."""
        self.assertTrue(
            is_placeholder_transcript_lines(
                [
                    "=== Turn 1: Seat None ===",
                    "Match winner: Team None",
                ]
            )
        )
        self.assertFalse(
            is_placeholder_transcript_lines(
                [
                    "=== Turn 1: Me ===",
                    "Opponent concedes",
                    "Winner: Me",
                ]
            )
        )

    def test_transcript_diagnostics_find_suspicious_artifacts(self):
        """Check diagnostics flag raw ids and near duplicate casts."""
        warnings = transcript_diagnostics(
            [
                "I cast Steel Seraph",
                "-- Beginning of combat --",
                "I cast Steel Seraph",
                "grpId 2 trigger exiles Blind Obedience",
                "Library: -1 cards",
            ]
        )
        self.assertTrue(any("near-duplicate cast" in warning for warning in warnings))
        self.assertTrue(any("raw grpId leaked" in warning for warning in warnings))
        self.assertTrue(any("negative library count" in warning for warning in warnings))

    def test_repeated_damage_life_sequences_are_combined(self):
        """Check repeated damage life sequences are combined."""
        lines = [
            "A copy of Shock deals 2 damage to me",
            "I lose 2 life (15)",
            "A copy of Shock resolves",
            "A copy of Shock deals 2 damage to me",
            "I lose 2 life (13)",
            "A copy of Shock resolves",
            "A copy of Shock deals 2 damage to me",
            "I lose 2 life (11)",
            "A copy of Shock resolves",
        ]
        self.assertEqual(
            combine_repeated_damage_life_sequences(lines),
            [
                "3x A copy of Shock deals 6 damage to me",
                "I lose 6 life (11)",
                "3x A copy of Shock resolves",
            ],
        )

    def test_copy_resolve_moves_after_revealed_return(self):
        """Check copy resolve moves after revealed return."""
        lines = [
            "A copy of Repulse resolves",
            "I reveal Resplendent Angel",
            "A copy of Repulse: I return Resplendent Angel to hand",
            "Opponent draws a card",
            "A copy of Repulse resolves",
        ]
        self.assertEqual(
            move_copy_resolves_after_revealed_returns(lines),
            [
                "I reveal Resplendent Angel",
                "A copy of Repulse: I return Resplendent Angel to hand",
                "Opponent draws a card",
                "A copy of Repulse resolves",
                "A copy of Repulse resolves",
            ],
        )

    def test_adjacent_same_target_attacks_are_combined(self):
        """Check adjacent same target attacks are combined."""
        lines = [
            "I play Temple of Enlightenment",
            "I attack Opponent with Healer's Hawk",
            "I attack Opponent with Giada, Font of Hope",
            "I attack Opponent with Inspiring Overseer",
            "I attack Opponent with Empyrean Eagle",
            "I attack Opponent with Youthful Valkyrie",
            "Opponent loses 15 life (-4)",
            "Opponent attacks me with Fanatical Firebrand",
            "Opponent attacks me with Crusader of Odric",
            "Opponent attacks Invasion of Zendikar with Frenzied Goblin",
        ]
        self.assertEqual(
            combine_adjacent_attack_lines(lines),
            [
                "I play Temple of Enlightenment",
                (
                    "I attack Opponent with Healer's Hawk; Giada, Font of Hope; "
                    "Inspiring Overseer; Empyrean Eagle; and Youthful Valkyrie"
                ),
                "Opponent loses 15 life (-4)",
                "Opponent attacks me with Fanatical Firebrand and Crusader of Odric",
                "Opponent attacks Invasion of Zendikar with Frenzied Goblin",
            ],
        )

    def test_redundant_match_winner_is_removed_after_same_game_winner(self):
        """Check redundant match winner is removed after same game winner."""
        self.assertEqual(
            remove_redundant_match_winner_lines(
                [
                    "Opponent loses 15 life (-4)",
                    "",
                    "Winner: Me",
                    "",
                    "Match winner: Me",
                ]
            ),
            [
                "Opponent loses 15 life (-4)",
                "",
                "Winner: Me",
            ],
        )
        self.assertEqual(
            remove_redundant_match_winner_lines(["Winner: Me", "", "Match winner: Opponent"]),
            ["Winner: Me", "", "Match winner: Opponent"],
        )

    def test_game_selection_modes(self):
        """Check game selection modes."""
        matches = [{"number": number} for number in range(1, 6)]
        self.assertEqual(
            [match["number"] for match in select_transcript_matches(matches)[0]],
            [1, 2, 3, 4, 5],
        )
        self.assertEqual(
            [match["number"] for match in select_transcript_matches(matches, nth_from_start=2)[0]],
            [2],
        )
        self.assertEqual(
            [match["number"] for match in select_transcript_matches(matches, nth_from_end=2)[0]],
            [4],
        )
        self.assertEqual(
            [match["number"] for match in select_transcript_matches(matches, first_games=2)[0]],
            [1, 2],
        )
        self.assertEqual(
            [match["number"] for match in select_transcript_matches(matches, last_games=2)[0]],
            [4, 5],
        )
        self.assertEqual(
            [match["number"] for match in select_transcript_matches(matches, game_range=(2, 4))[0]],
            [2, 3, 4],
        )

    def test_live_selection_conflict_ignores_default_all_false(self):
        """Check live selection conflict ignores default all false."""
        args = Namespace(
            all=False,
            select=None,
            nth_from_end=None,
            first=None,
            last=None,
            range=None,
        )
        self.assertFalse(has_live_selection_conflict(args))

        args.last = 1
        self.assertTrue(has_live_selection_conflict(args))

        args.last = None
        args.all = True
        self.assertTrue(has_live_selection_conflict(args))

    def test_live_state_prints_one_compact_snapshot_after_catch_up(self):
        """Check state-only live mode suppresses narration and emits a bounded snapshot."""
        root = {
            "greToClientEvent": {
                "greToClientMessages": [
                    {
                        "gameStateMessage": {
                            "gameStateId": 1,
                            "gameInfo": {"matchID": "test-match"},
                            "turnInfo": {
                                "turnNumber": 3,
                                "phase": "Phase_Main1",
                                "activePlayer": 1,
                                "decisionPlayer": 1,
                            },
                            "players": [
                                {"systemSeatNumber": 1, "lifeTotal": 20},
                                {"systemSeatNumber": 2, "lifeTotal": 17},
                            ],
                            "zones": [
                                {
                                    "zoneId": 10,
                                    "type": "ZoneType_Hand",
                                    "ownerSeatId": 1,
                                    "objectInstanceIds": [100],
                                },
                                {
                                    "zoneId": 11,
                                    "type": "ZoneType_Battlefield",
                                    "objectInstanceIds": [101],
                                },
                                {
                                    "zoneId": 12,
                                    "type": "ZoneType_Library",
                                    "ownerSeatId": 1,
                                    "objectInstanceIds": [201, 202],
                                },
                                {
                                    "zoneId": 13,
                                    "type": "ZoneType_Library",
                                    "ownerSeatId": 2,
                                    "objectInstanceIds": [301],
                                },
                            ],
                            "gameObjects": [
                                {
                                    "instanceId": 100,
                                    "grpId": 1,
                                    "zoneId": 10,
                                    "ownerSeatId": 1,
                                    "controllerSeatId": 1,
                                    "type": "GameObjectType_Card",
                                },
                                {
                                    "instanceId": 101,
                                    "grpId": 2,
                                    "zoneId": 11,
                                    "ownerSeatId": 1,
                                    "controllerSeatId": 1,
                                    "cardTypes": ["CardType_Land"],
                                    "type": "GameObjectType_Card",
                                },
                            ],
                        }
                    }
                ]
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "Player.log"
            log_path.write_text(json.dumps(root) + "\n", encoding="utf-8")
            output = io.StringIO()
            with patch("mtga_extract_games.time.sleep", side_effect=KeyboardInterrupt):
                with redirect_stdout(output):
                    extract_game_plays(
                        log_path,
                        {1: "Opt", 2: "Island"},
                        live=True,
                        live_state_only=True,
                        show_progress=False,
                    )

        rendered = output.getvalue()
        self.assertTrue(rendered.startswith("STATE game=1 turn=3 phase=pre_combat_main"))
        self.assertIn("ME life=20 hand=1 {Opt}", rendered)
        self.assertIn("land[u={Island}]", rendered)
        self.assertIn("OPP life=17", rendered)
        self.assertEqual(rendered.count("END_STATE"), 1)
        self.assertNotIn("===== GAME", rendered)
        self.assertNotIn("=== Turn", rendered)

    def test_match_room_player_names(self):
        """Check match-room records expose stable opponent labels by seat."""
        root = {
            "matchGameRoomStateChangedEvent": {
                "gameRoomInfo": {
                    "gameRoomConfig": {
                        "matchId": "match-names",
                        "reservedPlayers": [
                            {"systemSeatId": 1, "playerName": "MeName"},
                            {"systemSeatId": 2, "playerName": "OpponentName"},
                        ],
                    }
                }
            }
        }

        self.assertEqual(
            match_room_player_names(root),
            ("match-names", {1: "MeName", 2: "OpponentName"}),
        )

    def test_game_type_formatting(self):
        """Check game type formatting."""
        brawl_info = {
            "type": "GameType_Duel",
            "variant": "GameVariant_Brawl",
            "superFormat": "SuperFormat_Constructed",
        }
        normal_info = {
            "type": "GameType_Duel",
            "variant": "GameVariant_Normal",
            "superFormat": "SuperFormat_Constructed",
        }
        brawl_players = [{"startingLifeTotal": 25}, {"startingLifeTotal": 25}]
        normal_players = [{"startingLifeTotal": 20}, {"startingLifeTotal": 20}]

        self.assertEqual(
            format_game_type(brawl_info, brawl_players),
            "Game type: Constructed Brawl (25 starting life)",
        )
        self.assertEqual(
            format_game_type(normal_info, normal_players),
            "Game type: Constructed Duel (20 starting life)",
        )
        self.assertIsNone(format_game_type(None))
        self.assertTrue(
            game_has_commanders(
                {
                    "variant": "GameVariant_Normal",
                    "deckConstraintInfo": {"minCommanderSize": 1, "maxCommanderSize": 2},
                }
            )
        )
        self.assertTrue(game_has_commanders({"variant": "GameVariant_Brawl"}))
        self.assertFalse(
            game_has_commanders(
                {
                    "variant": "GameVariant_Normal",
                    "deckConstraintInfo": {"minDeckSize": 60, "maxSideboardSize": 15},
                }
            )
        )

    def test_path_resolution_uses_explicit_paths(self):
        """Check path resolution uses explicit paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            player_log = Path(tmpdir) / "Player.log"
            carddb = Path(tmpdir) / "Raw_CardDatabase_test.mtga"
            player_log.write_text("", encoding="utf-8")
            carddb.write_text("", encoding="utf-8")

            resolved_log, resolved_carddb, warning = resolve_input_paths(player_log, carddb)

            self.assertEqual(resolved_log, player_log)
            self.assertEqual(resolved_carddb, carddb)
            self.assertIsNone(warning)

    def test_player_prev_log_is_read_before_current_log(self):
        """Check player prev log is read before current log."""
        with tempfile.TemporaryDirectory() as tmpdir:
            player_log = Path(tmpdir) / "Player.log"
            previous_log = Path(tmpdir) / "Player-prev.log"
            player_log.write_text("", encoding="utf-8")
            previous_log.write_text("", encoding="utf-8")

            self.assertEqual(
                player_log_paths_for_reading(player_log),
                [previous_log, player_log],
            )
            self.assertEqual(player_log_paths_for_reading(player_log, live=True), [player_log])

    def test_archive_logs_are_read_before_player_logs(self):
        """Check archive logs are read before player logs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            player_log = Path(tmpdir) / "Player.log"
            previous_log = Path(tmpdir) / "Player-prev.log"
            archive_dir = Path(tmpdir) / "archives"
            archive_dir.mkdir()
            older_archive = archive_dir / "UTC_Log - older.log"
            newer_archive = archive_dir / "UTC_Log - newer.log"
            for path in (player_log, previous_log, older_archive, newer_archive):
                path.write_text("", encoding="utf-8")
            os.utime(older_archive, (100, 100))
            os.utime(newer_archive, (200, 200))

            with patch(
                "mtga_extract_games.arena_archive_log_paths",
                return_value=[older_archive, newer_archive],
            ):
                self.assertEqual(
                    player_log_paths_for_reading(player_log),
                    [older_archive, newer_archive, previous_log, player_log],
                )

    def test_seen_games_archive_is_keyed_by_match_id(self):
        """Check seen games archive is keyed by match id."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "seen.sqlite3"
            log_path = Path(tmpdir) / "Player.log"
            log_path.write_text("{}", encoding="utf-8")
            matches = [
                {
                    "number": 1,
                    "match_id": "match-1",
                    "lines": [
                        "===== GAME 1: MATCH match-1 =====",
                        "Game type: Constructed Duel (20 starting life)",
                        "I attack Opponent with A",
                        "I attack Opponent with B",
                        "Winner: Me",
                    ],
                    "has_result": True,
                }
            ]

            self.assertEqual(archive_seen_games(db_path, matches, [log_path]), (1, 0, 1))
            matches[0]["number"] = 99
            self.assertEqual(archive_seen_games(db_path, matches, [log_path]), (0, 1, 1))

            con = sqlite3.connect(db_path)
            row = con.execute(
                """
                SELECT
                    m.match_id,
                    m.archive_index,
                    g.game_archive_index,
                    g.game_index,
                    g.game_type,
                    g.has_result,
                    t.content
                FROM games g
                JOIN matches m ON m.id = g.match_row_id
                JOIN transcripts t ON t.game_id = g.id AND t.format = 'plain_text'
                """
            ).fetchone()
            schema_version = con.execute("PRAGMA user_version").fetchone()[0]
            source_count = con.execute("SELECT count(*) FROM log_sources").fetchone()[0]
            match_count = con.execute("SELECT count(*) FROM matches").fetchone()[0]
            game_count = con.execute("SELECT count(*) FROM games").fetchone()[0]
            con.close()

            self.assertEqual(schema_version, 3)
            self.assertEqual(source_count, 1)
            self.assertEqual(match_count, 1)
            self.assertEqual(game_count, 1)
            self.assertEqual(row[0], "match-1")
            self.assertEqual(row[1], 1)
            self.assertEqual(row[2], 1)
            self.assertEqual(row[3], 1)
            self.assertEqual(row[4], "Game type: Constructed Duel (20 starting life)")
            self.assertEqual(row[5], 1)
            self.assertIn("===== GAME 1: MATCH match-1 =====", row[6])
            self.assertIn("I attack Opponent with A and B", row[6])

            archived = archived_transcript_matches(db_path)
            self.assertEqual(archived[0]["number"], 1)
            self.assertEqual(archived[0]["match_id"], "match-1")
            self.assertEqual(archived[0]["match_number"], 1)
            self.assertEqual(archived[0]["game_index"], 1)
            self.assertIn("I attack Opponent with A and B", archived[0]["lines"])

    def test_archive_does_not_replace_full_state_with_reduced_transcript(self):
        """Check presentation flags cannot downgrade an archived full replay."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "seen.sqlite3"
            full = {
                "number": 1,
                "match_id": "match-full",
                "lines": [
                    "===== GAME 1: MATCH match-full =====",
                    "=== Turn 1: Me ===",
                    "My hand: Plains",
                    "My board:",
                    "  Lands: Untapped: Plains",
                    "Winner: Me",
                ],
                "has_result": True,
            }
            reduced = {
                **full,
                "lines": [
                    "===== GAME 1: MATCH match-full =====",
                    "=== Turn 1: Me ===",
                    "I play Plains",
                    "Winner: Me",
                ],
            }

            archive_seen_games(db_path, [full])
            archive_seen_games(db_path, [reduced])
            archived = archived_transcript_matches(db_path)[0]["lines"]

            self.assertIn("My board:", archived)
            self.assertNotIn("I play Plains", archived)

    def test_archive_appends_changed_transcript_as_immutable_version(self):
        """Check refreshed parser output creates history without overwriting its first row."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "seen.sqlite3"
            original = {
                "number": 1,
                "match_id": "match-versioned",
                "lines": [
                    "===== GAME 1: MATCH match-versioned =====",
                    "=== Turn 1: Me ===",
                    "My board:",
                    "  Lands: Untapped: Plains",
                    "Winner: Me",
                ],
                "has_result": True,
            }
            improved = {
                **original,
                "lines": [*original["lines"][:-1], "Current State:", "  1 energy", "Winner: Me"],
            }

            archive_seen_games(db_path, [original])
            con = sqlite3.connect(db_path)
            first_row = con.execute(
                "SELECT id, content, updated_at FROM transcripts"
            ).fetchone()
            con.close()

            archive_seen_games(db_path, [improved])
            con = sqlite3.connect(db_path)
            after_row = con.execute(
                "SELECT id, content, updated_at FROM transcripts"
            ).fetchone()
            version_count = con.execute(
                "SELECT count(*) FROM transcript_versions"
            ).fetchone()[0]
            with self.assertRaisesRegex(sqlite3.IntegrityError, "records are immutable"):
                con.execute("UPDATE transcripts SET content = 'damaged'")
            con.rollback()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "records are immutable"):
                con.execute("DELETE FROM transcript_versions")
            con.rollback()
            con.close()

            self.assertEqual(after_row, first_row)
            self.assertEqual(version_count, 2)
            self.assertIn(
                "1 energy",
                "\n".join(archived_transcript_matches(db_path)[0]["lines"]),
            )

    def test_archive_can_store_multiple_games_for_one_match(self):
        """Check archive can store multiple games for one match."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "seen.sqlite3"
            matches = [
                {
                    "number": 1,
                    "match_id": "match-bo3",
                    "game_index": 1,
                    "lines": [
                        "===== GAME 1: MATCH match-bo3 =====",
                        "Game type: Constructed Duel (20 starting life)",
                        "Winner: Me",
                    ],
                    "has_result": True,
                },
                {
                    "number": 2,
                    "match_id": "match-bo3",
                    "game_index": 2,
                    "lines": [
                        "===== GAME 2: MATCH match-bo3 =====",
                        "Game type: Constructed Duel (20 starting life)",
                        "Winner: Opponent",
                    ],
                    "has_result": True,
                },
            ]

            self.assertEqual(archive_seen_games(db_path, matches), (2, 0, 2))

            con = sqlite3.connect(db_path)
            self.assertEqual(con.execute("SELECT count(*) FROM matches").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT count(*) FROM games").fetchone()[0], 2)
            con.close()

            archived = archived_transcript_matches(db_path)
            self.assertEqual([match["number"] for match in archived], [1, 2])
            self.assertEqual([match["match_number"] for match in archived], [1, 1])
            self.assertEqual([match["game_index"] for match in archived], [1, 2])
            self.assertEqual([match["match_id"] for match in archived], ["match-bo3", "match-bo3"])

    def test_archive_ignores_placeholder_games(self):
        """Check Arena shell placeholder records are not archived."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "seen.sqlite3"
            matches = [
                {
                    "number": 1,
                    "match_id": "placeholder",
                    "lines": [
                        "===== GAME 1: MATCH placeholder =====",
                        "=== Turn 1: Seat None ===",
                        "Match winner: Team None",
                    ],
                    "has_result": True,
                },
                {
                    "number": 2,
                    "match_id": "real-match",
                    "lines": [
                        "===== GAME 2: MATCH real-match =====",
                        "=== Turn 1: Me ===",
                        "Opponent concedes",
                        "Winner: Me",
                    ],
                    "has_result": True,
                },
            ]

            self.assertEqual(archive_seen_games(db_path, matches), (1, 0, 1))
            archived = archived_transcript_matches(db_path)
            self.assertEqual([match["match_id"] for match in archived], ["real-match"])

    def test_archive_refresh_reorders_current_parse_to_end(self):
        """Check current parsed log order wins over stale archive insertion order."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "seen.sqlite3"
            latest = {
                "number": 1,
                "match_id": "latest-match",
                "lines": [
                    "===== GAME 1: MATCH latest-match =====",
                    "Game type: Constructed Duel (20 starting life)",
                    "Winner: Me",
                ],
                "has_result": True,
            }
            older = {
                "number": 1,
                "match_id": "older-match",
                "lines": [
                    "===== GAME 1: MATCH older-match =====",
                    "Game type: Constructed Duel (20 starting life)",
                    "Winner: Opponent",
                ],
                "has_result": True,
            }

            self.assertEqual(archive_seen_games(db_path, [latest]), (1, 0, 1))
            self.assertEqual(archive_seen_games(db_path, [older, latest]), (1, 1, 2))

            archived = archived_transcript_matches(db_path)
            self.assertEqual([match["match_id"] for match in archived], ["older-match", "latest-match"])
            self.assertEqual([match["number"] for match in archived], [1, 2])
            self.assertTrue(archived[1]["lines"][0].startswith("===== GAME 2: MATCH latest-match"))

    def test_archive_merge_imports_new_games_and_updates_existing_games(self):
        """Check archive merge dedupes by match id and game index."""
        with tempfile.TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir) / "dest.sqlite3"
            source = Path(tmpdir) / "source.sqlite3"
            source_log = Path(tmpdir) / "SourcePlayer.log"
            source_log.write_text("{}", encoding="utf-8")

            archive_seen_games(
                destination,
                [
                    {
                        "number": 1,
                        "match_id": "dest-only",
                        "lines": ["===== GAME 1: MATCH dest-only =====", "Winner: Me"],
                        "has_result": True,
                    },
                    {
                        "number": 2,
                        "match_id": "shared",
                        "game_index": 1,
                        "lines": ["===== GAME 2: MATCH shared =====", "Winner: Opponent"],
                        "has_result": True,
                    },
                ],
            )
            archive_seen_games(
                source,
                [
                    {
                        "number": 1,
                        "match_id": "shared",
                        "game_index": 1,
                        "lines": ["===== GAME 1: MATCH shared =====", "Winner: Me"],
                        "has_result": True,
                    },
                    {
                        "number": 2,
                        "match_id": "source-only",
                        "lines": ["===== GAME 2: MATCH source-only =====", "Winner: Me"],
                        "has_result": True,
                    },
                ],
                [source_log],
            )

            self.assertEqual(merge_archive_databases(destination, [source]), (1, 1, 3))

            archived = archived_transcript_matches(destination)
            self.assertEqual([match["match_id"] for match in archived], ["dest-only", "shared", "source-only"])
            shared = next(match for match in archived if match["match_id"] == "shared")
            self.assertIn("Winner: Me", shared["lines"])

            con = sqlite3.connect(destination)
            self.assertEqual(
                con.execute("SELECT count(*) FROM log_sources").fetchone()[0],
                1,
            )
            con.close()

    def test_import_db_cli_merges_into_archive_db(self):
        """Check --import-db imports a source archive into the selected archive DB."""
        with tempfile.TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir) / "dest.sqlite3"
            source = Path(tmpdir) / "source.sqlite3"
            archive_seen_games(
                source,
                [
                    {
                        "number": 1,
                        "match_id": "source-match",
                        "lines": ["===== GAME 1: MATCH source-match =====", "Winner: Me"],
                        "has_result": True,
                    },
                ],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "mtga_extract_games.py",
                    "--archive-db",
                    str(destination),
                    "--import-db",
                    str(source),
                ],
                text=True,
                capture_output=True,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Archive merge: imported 1 game(s) (1 new, 0 already archived)", result.stderr)
            archived = archived_transcript_matches(destination)
            self.assertEqual([match["match_id"] for match in archived], ["source-match"])

    def test_default_archive_db_path_uses_current_directory(self):
        """Check default archive db path uses current directory."""
        self.assertEqual(default_archive_db_path(), Path("mtga_games.sqlite3"))

    def test_archive_schema_migrates_legacy_transcript_table(self):
        """Check archive schema migrates legacy transcript table."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "legacy.sqlite3"
            con = sqlite3.connect(db_path)
            con.execute("""
                CREATE TABLE games (
                    match_id TEXT PRIMARY KEY,
                    archive_index INTEGER UNIQUE,
                    game_number INTEGER NOT NULL,
                    game_type TEXT,
                    has_result INTEGER NOT NULL,
                    line_count INTEGER NOT NULL,
                    transcript TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            con.execute(
                """
                INSERT INTO games (
                    match_id, archive_index, game_number, game_type,
                    has_result, line_count, transcript
                )
                VALUES ('legacy-match', 7, 7, 'Game type: Test', 1, 2, ?)
                """,
                ("===== GAME 7: MATCH legacy-match =====\nWinner: Me",),
            )
            con.commit()
            con.close()

            archived = archived_transcript_matches(db_path)

            self.assertEqual(archived[0]["number"], 7)
            self.assertEqual(archived[0]["match_id"], "legacy-match")
            self.assertIn("Winner: Me", archived[0]["lines"])

            con = sqlite3.connect(db_path)
            tables = {
                row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            self.assertIn("games_legacy_v0", tables)
            self.assertIn("matches", tables)
            self.assertIn("games", tables)
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(con.execute("SELECT count(*) FROM matches").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT count(*) FROM games").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT count(*) FROM transcripts").fetchone()[0], 1)
            self.assertEqual(
                con.execute("SELECT count(*) FROM transcript_versions").fetchone()[0],
                1,
            )
            con.close()

    def test_path_resolution_uses_environment_paths(self):
        """Check path resolution uses environment paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            player_log = Path(tmpdir) / "Player.log"
            carddb = Path(tmpdir) / "Raw_CardDatabase_test.mtga"
            player_log.write_text("", encoding="utf-8")
            carddb.write_text("", encoding="utf-8")

            with patch.dict(
                "os.environ",
                {"LOG": str(player_log), "CARDDB": str(carddb)},
                clear=False,
            ):
                resolved_log, resolved_carddb, warning = resolve_input_paths(None, None)

            self.assertEqual(resolved_log, player_log)
            self.assertEqual(resolved_carddb, carddb)
            self.assertIn("Using logs:", warning)
            self.assertIn(str(player_log), warning)

    def test_macos_discovery_finds_standard_paths(self):
        """Check macOS discovery finds standard Arena paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            player_log = home / "Library" / "Logs" / "Wizards Of The Coast" / "MTGA" / "Player.log"
            raw_dir = home / "Library" / "Application Support" / "com.wizards.mtga" / "Downloads" / "Raw"
            carddb = raw_dir / "Raw_CardDatabase_mac.mtga"
            player_log.parent.mkdir(parents=True)
            raw_dir.mkdir(parents=True)
            player_log.write_text("", encoding="utf-8")
            carddb.write_text("", encoding="utf-8")

            with patch.dict("os.environ", {"HOME": str(home), "USERPROFILE": str(home)}, clear=False):
                logs = find_player_logs("darwin")
                resolved_carddb = find_card_database("darwin")

            self.assertEqual(logs, [player_log])
            self.assertEqual(resolved_carddb, carddb)

    def test_windows_discovery_finds_player_log_and_previous_log(self):
        """Check Windows discovery finds Player.log and Player-prev.log."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            user = root / "Users" / "Alice"
            local_appdata = user / "AppData" / "Local"
            log_dir = user / "AppData" / "LocalLow" / "Wizards Of The Coast" / "MTGA"
            player_log = log_dir / "Player.log"
            previous_log = log_dir / "Player-prev.log"
            log_dir.mkdir(parents=True)
            player_log.write_text("", encoding="utf-8")
            previous_log.write_text("", encoding="utf-8")
            env = {
                "LOCALAPPDATA": str(local_appdata),
                "APPDATA": str(user / "AppData" / "Roaming"),
                "USERPROFILE": str(user),
            }

            paths = candidate_windows_paths(env)
            logs = find_player_logs("win32", env)

            self.assertIn(player_log, paths["player_logs"])
            self.assertIn(previous_log, paths["player_logs"])
            self.assertIn(player_log, logs)
            self.assertIn(previous_log, logs)

    def test_windows_carddb_discovery_prefers_newest_known_install_root(self):
        """Check Windows card database discovery chooses newest known-root match."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            program_files = root / "Program Files"
            raw_dir = (
                program_files
                / "Wizards of the Coast"
                / "MTGA"
                / "MTGA_Data"
                / "Downloads"
                / "Raw"
            )
            raw_dir.mkdir(parents=True)
            older = raw_dir / "Raw_CardDatabase_older.mtga"
            newer = raw_dir / "Raw_CardDatabase_newer.mtga"
            older.write_text("", encoding="utf-8")
            newer.write_text("", encoding="utf-8")
            os.utime(older, (100, 100))
            os.utime(newer, (200, 200))
            env = {
                "ProgramFiles": str(program_files),
                "ProgramFiles(x86)": str(root / "Program Files (x86)"),
                "LOCALAPPDATA": str(root / "Users" / "Alice" / "AppData" / "Local"),
            }

            self.assertEqual(find_card_database("win32", env), newer)

    def test_carddb_discovery_falls_back_to_recursive_windows_search(self):
        """Check Windows card database discovery falls back to recursive roots."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            local_appdata = root / "Users" / "Alice" / "AppData" / "Local"
            raw_dir = local_appdata / "MTGA" / "Nested" / "Downloads" / "Raw"
            carddb = raw_dir / "Raw_CardDatabase_recursive.mtga"
            raw_dir.mkdir(parents=True)
            carddb.write_text("", encoding="utf-8")
            env = {"LOCALAPPDATA": str(local_appdata)}

            self.assertEqual(find_card_database("win32", env), carddb)

    def test_path_resolution_environment_overrides_autodiscovery(self):
        """Check explicit environment paths override autodiscovery."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_log = root / "override" / "Player.log"
            env_carddb = root / "override" / "Raw_CardDatabase_override.mtga"
            auto_log = root / "Users" / "Alice" / "AppData" / "LocalLow" / "Wizards Of The Coast" / "MTGA" / "Player.log"
            auto_carddb = (
                root
                / "Program Files"
                / "Wizards of the Coast"
                / "MTGA"
                / "MTGA_Data"
                / "Downloads"
                / "Raw"
                / "Raw_CardDatabase_auto.mtga"
            )
            env_log.parent.mkdir(parents=True)
            env_carddb.parent.mkdir(parents=True, exist_ok=True)
            auto_log.parent.mkdir(parents=True)
            auto_carddb.parent.mkdir(parents=True)
            env_log.write_text("", encoding="utf-8")
            env_carddb.write_text("", encoding="utf-8")
            auto_log.write_text("", encoding="utf-8")
            auto_carddb.write_text("", encoding="utf-8")
            env = {
                "LOG": str(env_log),
                "CARDDB": str(env_carddb),
                "USERPROFILE": str(root / "Users" / "Alice"),
                "LOCALAPPDATA": str(root / "Users" / "Alice" / "AppData" / "Local"),
                "ProgramFiles": str(root / "Program Files"),
            }

            resolved_log, resolved_carddb, _warning = resolve_input_paths(
                None,
                None,
                platform_name="win32",
                env=env,
            )

            self.assertEqual(resolved_log, env_log)
            self.assertEqual(resolved_carddb, env_carddb)

    def test_missing_path_error_distinguishes_both_missing(self):
        """Check missing path error distinguishes both missing inputs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "USERPROFILE": str(root / "Users" / "Alice"),
                "LOCALAPPDATA": str(root / "Users" / "Alice" / "AppData" / "Local"),
                "ProgramFiles": str(root / "Program Files"),
            }

            with self.assertRaises(FileNotFoundError) as context:
                resolve_input_paths(None, None, platform_name="win32", env=env)

            message = str(context.exception)
            self.assertIn("Could not find Player.log and Raw_CardDatabase_*.mtga", message)
            self.assertIn("Player log paths checked:", message)
            self.assertIn("Card database paths checked:", message)

    def test_find_paths_diagnostics_reports_selected_paths(self):
        """Check path diagnostics report checked and selected paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            user = root / "Users" / "Alice"
            player_log = user / "AppData" / "LocalLow" / "Wizards Of The Coast" / "MTGA" / "Player.log"
            carddb = (
                root
                / "Program Files"
                / "Wizards of the Coast"
                / "MTGA"
                / "MTGA_Data"
                / "Downloads"
                / "Raw"
                / "Raw_CardDatabase_diag.mtga"
            )
            player_log.parent.mkdir(parents=True)
            carddb.parent.mkdir(parents=True)
            player_log.write_text("", encoding="utf-8")
            carddb.write_text("", encoding="utf-8")
            env = {
                "USERPROFILE": str(user),
                "LOCALAPPDATA": str(user / "AppData" / "Local"),
                "ProgramFiles": str(root / "Program Files"),
            }

            diagnostics = format_path_diagnostics(
                {
                    "os": "Windows",
                    "env": env,
                    "player_log": player_log,
                    "player_source": "autodiscovery",
                    "player_checked": [player_log],
                    "carddb": carddb,
                    "carddb_source": "autodiscovery",
                    "carddb_checked": [carddb.parent, carddb],
                }
            )

            self.assertIn("Operating system detected: Windows", diagnostics)
            self.assertIn(f"Selected player log: {player_log}", diagnostics)
            self.assertIn(f"Selected card database: {carddb}", diagnostics)

    def test_live_path_warning_only_lists_current_log(self):
        """Check live path warning only lists current log."""
        with tempfile.TemporaryDirectory() as tmpdir:
            player_log = Path(tmpdir) / "Player.log"
            previous_log = Path(tmpdir) / "Player-prev.log"
            carddb = Path(tmpdir) / "Raw_CardDatabase_test.mtga"
            player_log.write_text("", encoding="utf-8")
            previous_log.write_text("", encoding="utf-8")
            carddb.write_text("", encoding="utf-8")

            with patch.dict(
                "os.environ",
                {"LOG": str(player_log), "CARDDB": str(carddb)},
                clear=False,
            ):
                resolved_log, _resolved_carddb, warning = resolve_input_paths(
                    None,
                    None,
                    live=True,
                )

            self.assertEqual(resolved_log, player_log)
            self.assertIn(str(player_log), warning)
            self.assertNotIn(str(previous_log), warning)

    def test_path_resolution_error_explains_setup(self):
        """Check path resolution error explains setup."""
        missing_log = Path("/tmp/definitely-missing-mtga-player-log")
        missing_carddb = Path("/tmp/definitely-missing-mtga-carddb.mtga")
        with self.assertRaises(FileNotFoundError) as context:
            resolve_input_paths(missing_log, missing_carddb)

        message = str(context.exception)
        self.assertIn("Could not find Player.log", message)
        self.assertIn("export LOG=", message)
        self.assertIn("export CARDDB=", message)

    def test_me_as_object_becomes_me(self):
        """Check me as object becomes me."""
        self.assertEqual(object_pronoun("Me"), "me")
        self.assertEqual(
            phrase_player_action("Opponent", "attack", "me with Serra's Emissary"),
            "Opponent attacks me with Serra's Emissary",
        )

    def test_opponent_wording_stays_third_person(self):
        """Check opponent wording stays third person."""
        self.assertEqual(subject_pronoun("Opponent"), "Opponent")
        self.assertEqual(
            phrase_player_action("Opponent", "cast", "Arcane Signet"),
            "Opponent casts Arcane Signet",
        )

    def test_life_change_wording(self):
        """Check life change wording."""
        self.assertEqual(phrase_life_change("Me", 4, 29), "I gain 4 life (29)")
        self.assertEqual(
            phrase_life_change("Opponent", -2, 18),
            "Opponent loses 2 life (18)",
        )
        self.assertEqual(
            phrase_life_change_summary(
                "Authority of the Consuls trigger",
                "Opponent",
                1,
                9,
                122,
            ),
            "9x Authority of the Consuls trigger: Opponent gains 9 life (122)",
        )
        self.assertEqual(
            phrase_life_change_summary("Blood Artist trigger", "Me", -1, 1, 24),
            "Blood Artist trigger: I lose 1 life (24)",
        )
        self.assertEqual(
            phrase_life_change_group("Me", -1, 11, 1),
            "11x I lose 11 life (1)",
        )
        self.assertEqual(
            phrase_life_change_summary(None, "Me", -1, 11, 1),
            "11x I lose 11 life (1)",
        )
        self.assertTrue(should_group_life_change_source("Ayara, First of Locthwain ability"))
        self.assertTrue(should_group_life_change_source("A-Blood Artist trigger"))
        self.assertFalse(should_group_life_change_source(None))
        self.assertEqual(life_change_group_source("Vindictive Vampire"), "Vindictive Vampire ability")

    def test_death_wording(self):
        """Check death wording."""
        self.assertEqual(
            phrase_death("Opponent", "Inspiring Overseer"),
            "Opponent's Inspiring Overseer dies",
        )
        self.assertEqual(phrase_death("Me", "Giada, Font of Hope"), "My Giada, Font of Hope dies")
        self.assertEqual(phrase_death(None, "Angel"), "Angel dies")

    def test_extra_event_wording(self):
        """Check extra event wording."""
        self.assertEqual(phrase_draw("Me", "Plains", True), "I draw Plains")
        self.assertEqual(phrase_draw("Opponent", "Plains", False), "Opponent draws a card")
        self.assertEqual(
            phrase_sacrifice("Rottenmouth Viper trigger", "Me", "Warrior"),
            "Rottenmouth Viper trigger: I sacrifice Warrior",
        )
        self.assertEqual(
            phrase_move_to_zone("Tamiyo ability", "Opponent", "put", "Time Warp", "hand", "into"),
            "Tamiyo ability: Opponent puts Time Warp into hand",
        )
        self.assertEqual(phrase_scry("Me", ["Plains"], []), "I scry top: Plains")
        self.assertEqual(phrase_scry("Opponent", ["a card"], []), "Opponent scries top: a card")
        self.assertEqual(phrase_shuffle(None, "Me"), "I shuffle my library")
        self.assertEqual(
            phrase_source_action("Cultivate", "Opponent puts Forest into hand"),
            "Cultivate: Opponent puts Forest into hand",
        )

    def test_grouped_identical_token_deaths(self):
        """Check grouped identical token deaths."""
        self.assertEqual(
            phrase_grouped_deaths("Opponent", "Human", 2),
            "2 opponent Human tokens die",
        )
        self.assertEqual(phrase_grouped_deaths("Me", "Plant", 1), "My Plant dies")
        self.assertEqual(
            phrase_grouped_deaths("Me", "Shadowborn Apostle", 2),
            "2 of my Shadowborn Apostle tokens die",
        )

    def test_enters_attacking_wording(self):
        """Check enters attacking wording."""
        self.assertEqual(
            phrase_enters_attacking(
                "Raph & Mikey, Troublemakers trigger",
                "Krang, Utrom Warlord",
            ),
            "Raph & Mikey, Troublemakers trigger puts Krang, Utrom Warlord onto the battlefield attacking",
        )
        self.assertEqual(
            phrase_enters_attacking(None, "Robot token"),
            "Robot token enters the battlefield attacking",
        )

    def test_attack_phrase_suppresses_unknown_target(self):
        """Check attack phrase suppresses unknown target."""
        self.assertEqual(
            attack_phrase("Invasion of Zendikar", "Urza's Construction Drone"),
            "Invasion of Zendikar with Urza's Construction Drone",
        )
        self.assertEqual(
            attack_phrase(None, "Urza's Construction Drone"),
            "with Urza's Construction Drone",
        )

    def test_passive_zone_change_wording_examples(self):
        """Check passive zone change wording examples."""
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
        """Check concede result wording."""
        self.assertEqual(
            phrase_concede_result("Me", "game"),
            ["Opponent concedes", "Winner: Me"],
        )
        self.assertEqual(
            phrase_concede_result("Me", "match"),
            ["Match result: Opponent conceded", "Match winner: Me"],
        )
        self.assertEqual(
            phrase_concede_result("Opponent", "game"),
            ["I concede", "Winner: Opponent"],
        )

    def test_result_wording_avoids_duplicate_labels(self):
        """Check result wording avoids duplicate labels."""
        self.assertEqual(phrase_result("Opponent", "game", "game"), ["Winner: Opponent"])
        self.assertEqual(
            phrase_result("Opponent", "match", "game"),
            ["Match winner: Opponent"],
        )

    def test_lethal_life_loss_is_kept_before_winner_line(self):
        """Check lethal life loss is kept before winner line."""
        lines = [phrase_life_change("Me", -18, -3)]
        lines.extend(phrase_result("Opponent", "game", "game"))
        self.assertEqual(lines, ["I lose 18 life (-3)", "Winner: Opponent"])

    def test_numeric_choice_fallback_explains_domain(self):
        """Check numeric choice fallback explains domain."""
        self.assertEqual(
            phrase_choice_value("creature type", 19, None),
            "unknown creature type 19",
        )
        self.assertEqual(phrase_choice_value("creature type", 1, "Angel"), "Angel")
        self.assertEqual(phrase_choice_value("creature type", 25, "Elemental"), "Elemental")
        self.assertEqual(phrase_choice_value("mana value parity", 0, "even"), "even")
        self.assertEqual(phrase_keyword_choice_value(8, "flying"), "flying")
        self.assertEqual(phrase_keyword_choice_value(99, None), "unknown keyword 99")
        self.assertEqual(
            phrase_keyword_choice_values([8, 12], {8: "flying", 12: "lifelink"}),
            "flying; lifelink",
        )

    def test_card_type_choices_use_arena_enum_values(self):
        """Check card-type choices use Arena's CardType enum values."""
        choices = build_choice_value_names(
            {},
            {8: "Planeswalker", 10: "Sorcery", 14: "Battle"},
        )
        self.assertEqual(choices[4][8], "Planeswalker")
        self.assertEqual(choices[4][14], "Battle")

    def test_incomplete_game_notice_is_conservative(self):
        """Check incomplete game notice is conservative."""
        self.assertEqual(
            phrase_incomplete_game_notice(
                "Postgame course/event data includes a loss count after this match."
            ),
            [
                "Game appears to have ended, but no final Game Rules Engine (GRE) result was written to Player.log.",
                "Postgame course/event data includes a loss count after this match.",
                "Final life total is unavailable from the gameplay log.",
            ],
        )

    def test_enum_loader_reads_creature_types_from_card_database(self):
        """Check enum loader reads creature types from card database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            carddb = Path(tmpdir) / "carddb.mtga"
            con = sqlite3.connect(carddb)
            cur = con.cursor()
            cur.execute("CREATE TABLE Enums(Type TEXT, Value INT, LocId INT)")
            cur.execute("CREATE TABLE Localizations_enUS(LocId INT, Formatted INT, Loc TEXT)")
            cur.executemany(
                "INSERT INTO Enums VALUES (?, ?, ?)",
                [("SubType", 25, 100), ("SubType", 83, 101), ("Color", 25, 102)],
            )
            cur.executemany(
                "INSERT INTO Localizations_enUS VALUES (?, ?, ?)",
                [
                    (100, 1, "Elemental"),
                    (101, 1, "<nobr>Power-Plant</nobr>"),
                    (101, 2, "Power-Plant"),
                    (102, 1, "Not a subtype"),
                ],
            )
            con.commit()
            con.close()

            self.assertEqual(
                load_enum_value_names(carddb, "SubType"),
                {25: "Elemental", 83: "Power-Plant"},
            )
        self.assertEqual(clean_localized_enum_name("<nobr>Assembly-Worker</nobr>"), "Assembly-Worker")

    def test_keyword_loader_reads_intrinsic_ability_names(self):
        """Check StaticList_Keywords ids are decoded from intrinsic ability names."""
        with tempfile.TemporaryDirectory() as tmpdir:
            carddb = Path(tmpdir) / "carddb.mtga"
            con = sqlite3.connect(carddb)
            cur = con.cursor()
            cur.execute("CREATE TABLE Abilities(Id INT, TextId INT, IsIntrinsicAbility INT)")
            cur.execute("CREATE TABLE Localizations_enUS(LocId INT, Formatted INT, Loc TEXT)")
            cur.executemany(
                "INSERT INTO Abilities VALUES (?, ?, ?)",
                [(8, 100, 1), (12, 101, 1), (200, 102, 0)],
            )
            cur.executemany(
                "INSERT INTO Localizations_enUS VALUES (?, ?, ?)",
                [(100, 1, "Flying"), (101, 1, "Lifelink."), (102, 1, "Not a keyword")],
            )
            con.commit()
            con.close()

            names = load_keyword_value_names(carddb)
            self.assertEqual(names[8], "flying")
            self.assertEqual(names[12], "lifelink")
            self.assertNotEqual(names.get(200), "not a keyword")

    def test_keyword_loader_uses_category_when_intrinsic_flags_are_stale(self):
        """Check current CARDDB category rows work when intrinsic flags are false."""
        with tempfile.TemporaryDirectory() as tmpdir:
            carddb = Path(tmpdir) / "carddb.mtga"
            con = sqlite3.connect(carddb)
            cur = con.cursor()
            cur.execute(
                "CREATE TABLE Abilities("
                "Id INT, TextId INT, IsIntrinsicAbility INT, Category INT)"
            )
            cur.execute("CREATE TABLE Localizations_enUS(LocId INT, Formatted INT, Loc TEXT)")
            cur.executemany(
                "INSERT INTO Abilities VALUES (?, ?, ?, ?)",
                [(13, 100, 0, 3), (500, 101, 0, 7)],
            )
            cur.executemany(
                "INSERT INTO Localizations_enUS VALUES (?, ?, ?)",
                [(100, 1, "Reach"), (101, 1, "Not an ability")],
            )
            con.commit()
            con.close()

            names = load_keyword_value_names(carddb)
            self.assertEqual(names[13], "reach")
            self.assertNotIn(500, names)

    def test_card_metadata_loader_reads_types_and_resource_mechanics(self):
        """Check card metadata loader reads types and resource mechanics."""
        with tempfile.TemporaryDirectory() as tmpdir:
            carddb = Path(tmpdir) / "carddb.mtga"
            con = sqlite3.connect(carddb)
            cur = con.cursor()
            cur.execute(
                "CREATE TABLE Cards("
                "GrpId INT, TitleId INT, Types TEXT, AbilityIds TEXT, "
                "OldSchoolManaText TEXT, Order_CMCWithXLast INT)"
            )
            cur.execute("CREATE TABLE Abilities(Id INT, TextId INT)")
            cur.execute("CREATE TABLE Localizations_enUS(LocId INT, Formatted INT, Loc TEXT)")
            cur.executemany(
                "INSERT INTO Cards VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (100, 1, "5", "", "", None),
                    (200, 2, "10", "5301:3105", "oR", 1),
                    (300, 3, "2", "7000:1", "o1oGoU", 3),
                ],
            )
            cur.executemany(
                "INSERT INTO Abilities VALUES (?, ?)",
                [(5301, 10), (7000, 11)],
            )
            cur.executemany(
                "INSERT INTO Localizations_enUS VALUES (?, ?, ?)",
                [
                    (1, 1, "Forest"),
                    (2, 1, "Faithless Looting"),
                    (3, 1, "Uro, Titan of Nature's Wrath"),
                    (10, 1, "Flashback {o2oR}"),
                    (11, 1, "Escape-{oGoG}, Exile five other cards from your graveyard."),
                ],
            )
            con.commit()
            con.close()

            metadata = load_grp_id_to_metadata(carddb)

        self.assertEqual(metadata[100]["type_numbers"], {5})
        self.assertEqual(metadata[200]["mana_cost"], "oR")
        self.assertEqual(metadata[200]["mana_value"], 1)
        self.assertEqual(metadata[200]["play_mechanics"], ["flashback"])
        self.assertEqual(metadata[300]["play_mechanics"], ["escape"])

    def test_ability_text_loader_reads_modal_child_text(self):
        """Check ability text loader reads modal child text."""
        with tempfile.TemporaryDirectory() as tmpdir:
            carddb = Path(tmpdir) / "carddb.mtga"
            con = sqlite3.connect(carddb)
            cur = con.cursor()
            cur.execute("CREATE TABLE Abilities(Id INT, TextId INT)")
            cur.execute("CREATE TABLE Localizations_enUS(LocId INT, Formatted INT, Loc TEXT)")
            cur.executemany(
                "INSERT INTO Abilities VALUES (?, ?)",
                [(22657, 10), (101796, 11)],
            )
            cur.executemany(
                "INSERT INTO Localizations_enUS VALUES (?, ?, ?)",
                [
                    (10, 1, "Target creature gains indestructible until end of turn."),
                    (11, 1, "Choose one<nobr> —</nobr> Target creature gains indestructible."),
                ],
            )
            con.commit()
            con.close()

            self.assertEqual(
                load_ability_texts(carddb),
                {
                    22657: "Target creature gains indestructible until end of turn.",
                    101796: "Choose one — Target creature gains indestructible.",
                },
            )

    def test_available_resource_helpers(self):
        """Check available resource helpers."""
        land_meta = {"type_numbers": {5}}
        creature_meta = {"type_numbers": {2}}
        self.assertEqual(parse_carddb_int_list("6832:437616,165909:683428"), [6832, 165909])
        self.assertEqual(resource_mechanics_from_text(["Flashback {o2oR}", "Escape cost"]), ["escape", "flashback"])
        self.assertEqual(resource_mechanics_for_zone(["flashback", "adventure"], "graveyard"), ["flashback"])
        self.assertEqual(resource_mechanics_for_zone(["flashback", "adventure"], "exile"), ["adventure"])
        self.assertTrue(card_is_land({}, land_meta))
        self.assertFalse(card_is_land({"cardTypes": ["CardType_Creature"]}, creature_meta))
        self.assertTrue(card_is_nonland_permanent({}, creature_meta))
        self.assertFalse(card_is_nonland_permanent({}, land_meta))
        self.assertEqual(
            available_resource_lines(
                {
                    "potential_graveyard_exile_plays": [
                        "Faithless Looting [flashback from graveyard, cost not checked]"
                    ],
                }
            ),
            [
                "Available Resources:",
                "  Other playable cards:",
                "    Faithless Looting [flashback from graveyard, cost not checked]",
            ],
        )

    def test_mana_summary_helpers(self):
        """Check mana availability and floating mana helpers."""
        self.assertEqual(format_card_mana_cost("o7"), "7")
        self.assertEqual(format_card_mana_cost("o2oW"), "2W")
        self.assertEqual(format_card_mana_cost("o5oWoWoW"), "5WWW")
        spell_cost = [
            {"color": ["ManaColor_Generic"], "count": 1},
            {"color": ["ManaColor_Black"], "count": 1},
        ]
        self.assertEqual(format_mana_cost_entries(spell_cost), "1B")
        self.assertEqual(mana_cost_amount(spell_cost), 2)
        self.assertEqual(format_cast_mana_note("1B", 2, 5), " [spent 1B; remaining 3 mana]")
        self.assertEqual(format_cast_mana_note("1B", 2, None), " [spent 1B]")
        self.assertEqual(mana_cost_text_amount("3GU"), 5)
        self.assertEqual(mana_pool_count([{"color": "ManaColor_Green", "count": 2}]), 2)
        self.assertEqual(land_mana_options("Wooded Foothills"), set())
        self.assertEqual(land_mana_options("Battlefield Forge"), {1, 4})
        metadata = {
            "ability_texts": [
                "{oT}: Add {oG}. Spend this mana only to activate abilities.",
            ],
        }
        self.assertEqual(mana_options_from_ability_texts(metadata), {5})
        self.assertEqual(mana_restriction_from_ability_texts(metadata), "abilities only")
        self.assertEqual(
            format_available_mana_summary(3, [{1}, {2, 5}, {6}], 1),
            "Available mana: 3 total (colors: W/U/G; 1 colorless-only; 1 unknown/flexible)",
        )
        self.assertEqual(
            format_available_mana_summary(
                2,
                [{1}, {4}],
                restricted=["Cavern of Souls 1 W/U/B/R/G [creatures only]"],
            ),
            "Available mana: 2 general (colors: W/R; restricted: Cavern of Souls 1 W/U/B/R/G [creatures only])",
        )
        self.assertEqual(
            format_mana_pool(
                [
                    {"color": "ManaColor_Black", "count": 2},
                    {"color": "ManaColor_Green", "count": 1},
                ],
            ),
            "Floating mana: 2 B; 1 G",
        )
        self.assertEqual(
            format_available_mana_summary(1, [{6}], restricted=[]),
            "Available mana: 1 total (1 colorless-only)",
        )
        self.assertEqual(
            format_available_mana_summary(
                0,
                [],
                restricted=["Giada, Font of Hope 1 W [restricted spells only]"],
            ),
            "Available mana: only restricted sources (restricted: Giada, Font of Hope 1 W [restricted spells only])",
        )
        self.assertEqual(
            format_mana_pool(
                [
                    {
                        "color": "ManaColor_Green",
                        "count": 1,
                        "specs": [{"type": "ManaSpecType_FromSnow"}],
                    },
                    {
                        "color": "ManaColor_Green",
                        "count": 1,
                        "specs": [{"type": "ManaSpecType_FromSnow"}],
                    },
                ],
            ),
            "Floating mana: 2 G; source tags: 2x 1 G [Snow]",
        )
        clarion = {
            "ability_texts": [
                "Flying",
                "Activated abilities of artifacts, creatures, and planeswalkers can't be activated.",
            ],
        }
        self.assertEqual(
            activated_ability_lock_types(clarion),
            {"CardType_Artifact", "CardType_Creature", "CardType_Planeswalker"},
        )
        self.assertTrue(
            object_matches_any_card_type(
                {"cardTypes": ["CardType_Artifact"]},
                None,
                activated_ability_lock_types(clarion),
            )
        )
        self.assertFalse(
            object_matches_any_card_type(
                {"cardTypes": ["CardType_Land"]},
                None,
                activated_ability_lock_types(clarion),
            )
        )
        halfling_option = {
            "mana": [
                {
                    "color": "ManaColor_Green",
                    "count": 1,
                    "srcInstanceId": 743,
                    "specs": [
                        {"type": "ManaSpecType_Predictive"},
                        {"type": "ManaSpecType_Restricted"},
                        {"type": "ManaSpecType_AdditionalEffect"},
                        {"type": "ManaSpecType_CantBeCountered"},
                    ],
                }
            ]
        }
        action = {
            "actionType": "ActionType_Cast",
            "instanceId": 241,
            "autoTapSolution": {
                "autoTapActions": [{"manaPaymentOption": halfling_option}]
            },
        }
        self.assertEqual(action_mana_payment_options(action), [halfling_option])
        self.assertEqual(mana_sources_from_option(halfling_option), [743])
        self.assertIn("ManaSpecType_CantBeCountered", mana_spec_types_from_option(halfling_option))
        self.assertNotIn("ManaSpecType_CantBeCountered", mana_spec_types_from_option({"mana": []}))
        self.assertTrue(should_carry_cast_marker("ZoneType_Stack", "ZoneType_Battlefield"))
        self.assertTrue(should_carry_cast_marker("ZoneType_Stack", "ZoneType_Graveyard"))
        self.assertFalse(should_carry_cast_marker("ZoneType_Hand", "ZoneType_Stack"))
        self.assertFalse(should_carry_cast_marker("ZoneType_Battlefield", "ZoneType_Hand"))
        self.assertFalse(should_carry_cast_marker("ZoneType_Battlefield", "ZoneType_Command"))
        self.assertEqual(
            phrase_mana_tap(
                "Relic of Legends",
                "Opponent",
                "A-Acererak the Archlich",
                "B",
            ),
            "Relic of Legends ability: Opponent taps A-Acererak the Archlich for B",
        )

    def test_commander_wording(self):
        """Check commander wording."""
        self.assertEqual(
            phrase_commander_cast_note(2),
            "commander cast #2; next commander tax +4",
        )
        self.assertEqual(
            phrase_damage("Lyra Dawnbringer", 6, "Steel Seraph"),
            "Lyra Dawnbringer deals 6 damage to Steel Seraph",
        )
        self.assertEqual(damage_mark_suffix(6, {"value": 6}), " (6/6 damage)")
        self.assertEqual(damage_mark_suffix(2, None), " (2 damage marked)")
        self.assertEqual(object_pronoun("Me"), "me")

    def test_player_counter_wording(self):
        """Check player counter wording."""
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
        self.assertEqual(
            counter_summary_suffix(Counter({1: 2, 7: 1, 9: 0}), {1: "+1/+1"}),
            " (+2/+2 from counters; counter 7)",
        )
        self.assertEqual(scaled_power_toughness_counter("+1/+1", 38), "+38/+38")
        self.assertEqual(
            capped_power_toughness_counter_amount("+1/+1", 9, 5, 5, 1, 1),
            4,
        )
        self.assertEqual(
            counter_summary_parts(
                Counter({1: 9}),
                {1: "+1/+1"},
                current_power=5,
                current_toughness=5,
                printed_power=1,
                printed_toughness=1,
            ),
            ["+4/+4 from counters"],
        )
        self.assertEqual(
            effect_power_toughness_modifier(5, 5, 1, 1, ["+2/+2 from counters"]),
            "+2/+2 from effects",
        )
        self.assertIsNone(effect_power_toughness_modifier(3, 3, 1, 1, ["+2/+2 from counters"]))

    def test_engine_keyword_summary_uses_exposed_abilities(self):
        """Check current object keywords come from Arena ability ids."""
        self.assertEqual(
            engine_keyword_summary(
                [{"grpId": 8}, {"grpId": 12}, {"grpId": 999}],
                {8: "flying", 12: "lifelink", 999: "draw a card"},
            ),
            ["flying", "lifelink"],
        )

    def test_engine_keyword_summary_handles_current_carddb_and_rejects_sentences(self):
        """Check concise keywords render without misreading rules sentences."""
        self.assertEqual(
            engine_keyword_summary(
                [
                    {"grpId": 13},
                    {"grpId": 211},
                    {"grpId": 500},
                    {"grpId": 501},
                ],
                {
                    13: "reach",
                    211: "ward—pay 2 life",
                    500: "Creatures you control have flying.",
                    501: "Flying and vigilance",
                },
            ),
            ["reach", "ward—pay 2 life", "flying", "vigilance"],
        )
        self.assertEqual(
            concise_engine_keyword_labels("Protection from red."),
            ["protection from red"],
        )

    def test_board_token_and_power_toughness_wording(self):
        """Check board token labels and current power/toughness summaries."""
        token = {
            "type": "GameObjectType_Token",
            "cardTypes": ["CardType_Creature"],
            "power": {"value": 5},
            "toughness": {"value": 5},
        }
        card = {
            "type": "GameObjectType_Card",
            "cardTypes": ["CardType_Creature"],
            "power": {"value": 2},
            "toughness": {"value": 1},
        }
        land = {
            "type": "GameObjectType_Card",
            "cardTypes": ["CardType_Land"],
            "power": {"value": 2},
            "toughness": {"value": 2},
        }

        self.assertEqual(token_board_name("Angel", token), "Token Angel")
        self.assertEqual(token_board_name("Token Angel", token), "Token Angel")
        self.assertEqual(token_board_name("Speaker of the Heavens", card), "Speaker of the Heavens")
        self.assertEqual(power_toughness_summary(token), "5/5")
        self.assertEqual(power_toughness_summary(card), "2/1")
        self.assertIsNone(power_toughness_summary(land))

    def test_attachment_summary_wording(self):
        """Check attachment summary wording."""
        self.assertEqual(
            grouped_name_phrase(["Crystal Carapace", "Crystal Carapace"]),
            "2x Crystal Carapace",
        )
        self.assertEqual(
            attachment_summary_parts(
                {
                    "aura": ["Crystal Carapace"],
                    "equipment": ["Shadowspear"],
                    "other": ["Cursed Role"],
                }
            ),
            [
                "enchanted by Crystal Carapace",
                "equipped with Shadowspear",
                "attached to Cursed Role",
            ],
        )
        self.assertEqual(
            attached_to_player_summary_parts({"aura": ["Curse of Leeches"]}),
            ["Auras: Curse of Leeches"],
        )
        self.assertEqual(
            attached_to_player_summary_parts({"curse": ["Curse of Leeches"]}),
            ["Curses: Curse of Leeches"],
        )
        self.assertEqual(
            emblem_summary_line(["Koth emblem", "Koth emblem"]),
            "Emblems: 2x Koth emblem",
        )
        self.assertEqual(
            modifier_summary_suffix(
                ["+1/+1 from counters", "enchanted by Crystal Carapace"]
            ),
            " (+1/+1 from counters; enchanted by Crystal Carapace)",
        )

    def test_ownership_summary_wording(self):
        """Check ownership summary wording."""
        self.assertIsNone(ownership_summary_part("Me", "Me"))
        self.assertEqual(ownership_summary_part("Opponent", "Me"), "owned by me")
        self.assertEqual(ownership_summary_part("Me", "Opponent"), "owned by Opponent")

    def test_repeated_hidden_card_wording(self):
        """Check repeated hidden card wording."""
        self.assertEqual(compact_counted_name("unknown card", 1), "unknown card")
        self.assertEqual(compact_counted_name("unknown card", 7), "7 unknown cards")
        self.assertEqual(
            compact_counted_name("a face-down card", 2),
            "2 face-down cards",
        )
        self.assertEqual(compact_counted_name("Island", 2), "2x Island")

    def test_target_phrase_wording(self):
        """Check target phrase wording."""
        self.assertEqual(
            append_target_phrase("Into the Roil", ["K'rrik, Son of Yawgmoth"]),
            "Into the Roil targeting K'rrik, Son of Yawgmoth",
        )
        self.assertEqual(
            format_target_phrase(["Giada, Font of Hope", "Youthful Valkyrie"]),
            "Giada, Font of Hope; Youthful Valkyrie",
        )
        self.assertEqual(append_target_phrase("Arcane Signet", []), "Arcane Signet")
        self.assertEqual(
            base_cast_name("Tamiyo's Safekeeping targeting Ivy, Gleeful Spellthief"),
            "Tamiyo's Safekeeping",
        )
        self.assertEqual(
            base_cast_name("Ivy, Gleeful Spellthief from command zone; commander cast #1"),
            "Ivy, Gleeful Spellthief",
        )

    def test_target_debug_path_detection(self):
        """Check target debug path detection."""
        self.assertIn(
            "annotations[0].details[0].targetId",
            find_target_like_paths(
                {"annotations": [{"details": [{"targetId": 123}]}]}
            ),
        )

    def test_library_count_wording(self):
        """Check library count wording."""
        self.assertEqual(phrase_library_count("Me", 1), "Me: 1 card")
        self.assertEqual(phrase_library_count("Opponent", 42), "Opponent: 42 cards")
        self.assertEqual(phrase_library_count("Player 1", None), "Player 1: unknown")

    def test_library_zone_transfer_deltas(self):
        """Check inferred library deltas for common zone movements."""
        self.assertEqual(
            library_delta_for_zone_transfer(
                "Draw",
                "ZoneType_Library",
                "ZoneType_Hand",
                1,
            ),
            -1,
        )
        self.assertEqual(
            library_delta_for_zone_transfer(
                "Put",
                "ZoneType_Library",
                "ZoneType_Battlefield",
                3,
            ),
            -3,
        )
        self.assertEqual(
            library_delta_for_zone_transfer(
                "Seek",
                "ZoneType_Library",
                "ZoneType_Hand",
                2,
            ),
            -2,
        )
        self.assertEqual(
            library_delta_for_zone_transfer(
                "Conjure",
                None,
                "ZoneType_Library",
                1,
            ),
            1,
        )
        self.assertEqual(
            library_delta_for_zone_transfer(
                "Shuffle",
                "ZoneType_Library",
                "ZoneType_Library",
                10,
            ),
            0,
        )

    def test_library_debug_descriptions_and_authoritative_override(self):
        """Check library debug descriptions and Arena count override behavior."""
        self.assertEqual(
            library_movement_description(
                "Put",
                "ZoneType_Library",
                "ZoneType_Battlefield",
            ),
            "Put: library -> Battlefield",
        )
        self.assertEqual(apply_authoritative_library_count(40, 40), (40, False))
        self.assertEqual(apply_authoritative_library_count(43, 40), (40, True))
        self.assertEqual(apply_authoritative_library_count(None, 40), (40, False))

    def test_mulligan_wording_does_not_assume_hand_size(self):
        """Check mulligan wording does not assume hand size."""
        self.assertEqual(phrase_mulligan("Me"), "I mulligan")
        self.assertEqual(phrase_mulligan("Me", 7), "I mulligan (kept 7 cards)")
        self.assertEqual(
            phrase_mulligan("Opponent", 1),
            "Opponent mulligans (kept 1 card)",
        )

    def test_low_fidelity_update_without_turn_context_is_delayed(self):
        """Check low fidelity update without turn context is delayed."""
        self.assertTrue(is_low_fidelity_update_without_turn({"update": "GameStateUpdate_Send"}))
        self.assertFalse(
            is_low_fidelity_update_without_turn(
                {"update": "GameStateUpdate_Send", "turnInfo": {"turnNumber": 8}}
            )
        )
        self.assertFalse(is_low_fidelity_update_without_turn({"update": "GameStateUpdate_SendHiFi"}))

    def test_missing_cast_inference_only_for_named_card_spells(self):
        """Check missing cast inference only for named card spells."""
        emitted = {12}
        self.assertTrue(
            should_infer_missing_cast_before_resolve(
                "Consuming Corruption",
                1077,
                {"type": "GameObjectType_Card"},
                emitted,
            )
        )
        self.assertFalse(
            should_infer_missing_cast_before_resolve(
                "instance 1077",
                1077,
                {"type": "GameObjectType_Card"},
                emitted,
            )
        )
        self.assertFalse(
            should_infer_missing_cast_before_resolve(
                "Mesmeric Orb trigger",
                1078,
                {"type": "GameObjectType_Ability"},
                emitted,
            )
        )
        self.assertFalse(
            should_infer_missing_cast_before_resolve(
                "A copy of Lightning Bolt",
                1079,
                {"type": "GameObjectType_Card", "isCopy": True},
                emitted,
            )
        )
        self.assertFalse(
            should_infer_missing_cast_before_resolve(
                "Temple Garden",
                1080,
                {"type": "GameObjectType_Card", "cardTypes": ["CardType_Land"]},
                emitted,
            )
        )
        self.assertFalse(
            should_infer_missing_cast_before_resolve(
                "Dungeon of the Mad Mage",
                1081,
                {"type": "GameObjectType_Card", "cardTypes": ["CardType_Dungeon"]},
                emitted,
            )
        )
        self.assertFalse(
            should_infer_missing_cast_before_resolve(
                "Heartless Act",
                12,
                {"type": "GameObjectType_Card"},
                emitted,
            )
        )

    def test_anonymous_resolve_suppression(self):
        """Check anonymous resolve suppression."""
        self.assertFalse(should_emit_resolve_line("instance 729", 729))
        self.assertTrue(should_emit_resolve_line("Petty Theft", 729))

    def test_adventure_resolve_uses_stack_name(self):
        """Check adventure resolve uses stack name."""
        self.assertEqual(
            resolve_stack_name(561, "Brazen Borrower", {561: "Petty Theft"}),
            "Petty Theft",
        )
        self.assertEqual(resolve_stack_name(908, "Brazen Borrower", {}), "Brazen Borrower")
        self.assertEqual(
            adventure_exile_line("Virtue of Persistence", "Locthwain Scorn"),
            "Virtue of Persistence is exiled on an adventure",
        )
        self.assertIsNone(adventure_exile_line("Virtue of Persistence", "Virtue of Persistence"))

    def test_power_toughness_mod_wording(self):
        """Check generic spell/effect P/T modification wording."""
        self.assertEqual(
            phrase_power_toughness_mod(
                "Locthwain Scorn",
                "Giada, Font of Hope",
                -3,
                -3,
            ),
            "Locthwain Scorn: Giada, Font of Hope gets -3/-3",
        )
        self.assertEqual(
            phrase_power_toughness_mod(None, "Mossborn Hydra", 2, 2),
            "Mossborn Hydra gets +2/+2",
        )

    def test_countered_spell_copy_wording_is_distinct(self):
        """Check countered spell copy wording is distinct."""
        self.assertEqual(copied_object_label("Heartless Act", True), "A copy of Heartless Act")
        self.assertEqual(copied_object_label("Heartless Act", False), "Heartless Act")
        self.assertEqual(copied_object_label("Mesmeric Orb", True), "A copy of Mesmeric Orb")

    def test_ability_object_wording_uses_source_card(self):
        """Check ability object wording uses source card."""
        self.assertEqual(ability_object_label("Mesmeric Orb", True), "Mesmeric Orb trigger")
        self.assertEqual(ability_object_label("Cavern of Souls", False), "Cavern of Souls ability")

    def test_ability_source_instance_id_uses_parent(self):
        """Check ability source instance id uses parent."""
        self.assertEqual(
            ability_source_instance_id(
                {"type": "GameObjectType_Ability", "parentId": 1407}
            ),
            1407,
        )
        self.assertIsNone(ability_source_instance_id({"type": "GameObjectType_Card"}))

    def test_hidden_arena_object_detection(self):
        """Check hidden arena object detection."""
        self.assertTrue(is_hidden_arena_object({"isFacedown": True, "grpId": 3}))
        self.assertTrue(is_hidden_arena_object({"grpId": 3}))
        self.assertFalse(is_hidden_arena_object({"grpId": 90933}))

    def test_grouped_mill_wording_is_source_aware(self):
        """Check grouped mill wording is source aware."""
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
        """Check leyline active effect wording."""
        self.assertEqual(
            active_effect_for_resolved_permanent("Leyline of the Void", "Opponent"),
            "Leyline of the Void exiles opponents' cards that would go to graveyard",
        )

    def test_unidentified_death_is_suppressed(self):
        """Check unidentified death is suppressed."""
        self.assertIsNone(death_label_or_none("instance 913", 913))
        self.assertEqual(death_label_or_none("Spirit token", 913), "Spirit token")


if __name__ == "__main__":
    unittest.main()
