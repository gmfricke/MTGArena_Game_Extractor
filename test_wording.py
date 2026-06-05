import unittest
import os
import sqlite3
import tempfile
from argparse import Namespace
from unittest.mock import patch
from collections import Counter
from pathlib import Path

from mtga_extract_games import (
    active_effect_for_resolved_permanent,
    ability_source_instance_id,
    ability_object_label,
    attack_phrase,
    attachment_summary_parts,
    append_target_phrase,
    archive_seen_games,
    archived_transcript_matches,
    available_resource_lines,
    base_cast_name,
    build_card_name_colors,
    build_card_name_pattern,
    card_is_land,
    card_is_nonland_permanent,
    colorize_transcript_line,
    clean_localized_enum_name,
    combine_adjacent_attack_lines,
    combine_duplicate_transcript_lines,
    compact_counted_name,
    counter_summary_suffix,
    copied_object_label,
    death_label_or_none,
    default_archive_db_path,
    is_hidden_arena_object,
    load_ability_texts,
    load_enum_value_names,
    load_grp_id_to_metadata,
    life_change_group_source,
    object_pronoun,
    ownership_summary_part,
    parse_carddb_int_list,
    phase_section_label,
    player_log_paths_for_reading,
    phrase_commander_cast_note,
    phrase_commander_damage,
    phrase_concede_result,
    phrase_choice_value,
    phrase_death,
    phrase_enters_attacking,
    phrase_grouped_deaths,
    phrase_incomplete_game_notice,
    phrase_life_change,
    phrase_life_change_group,
    phrase_life_change_summary,
    phrase_library_count,
    phrase_mulligan,
    phrase_mill_summary,
    phrase_player_has_counter,
    phrase_player_counter_change,
    phrase_player_action,
    phrase_result,
    phrase_zone_change,
    find_target_like_paths,
    format_game_type,
    format_target_phrase,
    game_has_commanders,
    grouped_name_phrase,
    has_live_selection_conflict,
    modifier_summary_suffix,
    remove_redundant_match_winner_lines,
    resolve_stack_name,
    resolve_input_paths,
    resource_mechanics_for_zone,
    resource_mechanics_from_text,
    scaled_power_toughness_counter,
    select_transcript_matches,
    should_group_life_change_source,
    is_low_fidelity_update_without_turn,
    should_infer_missing_cast_before_resolve,
    should_emit_resolve_line,
    state_player_heading,
    state_player_label,
    state_zone_label,
    subject_pronoun,
    colorize_land_names,
    should_color_output,
    transcript_line_perspective,
    transcript_line_style,
)


class WordingTests(unittest.TestCase):
    def test_me_as_subject_becomes_i(self):
        self.assertEqual(state_player_label("Me"), "I")
        self.assertEqual(
            phrase_player_action("Me", "cast", "Giada, Font of Hope"),
            "I cast Giada, Font of Hope",
        )

    def test_transcript_line_color_helpers(self):
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
        self.assertEqual(transcript_line_style("-- Combat - damage --"), "metadata")
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
        self.assertIsNone(phase_section_label({"phase": "Phase_Main1"}))
        self.assertEqual(
            phase_section_label({"phase": "Phase_Combat", "step": "Step_DeclareBlock"}),
            "Combat - blockers",
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
        self.assertEqual(state_zone_label("Me", "board"), "My board")
        self.assertEqual(state_zone_label("Opponent", "hand"), "Opponent's hand")
        self.assertEqual(state_zone_label("Player 1", "library"), "Player 1's library")
        self.assertEqual(state_player_heading("Me"), "My board")
        self.assertEqual(state_player_heading("Opponent"), "Opponent's board")
        self.assertEqual(state_player_heading("Player 1"), "Player 1's board")

    def test_adjacent_duplicate_transcript_lines_are_combined(self):
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

    def test_adjacent_same_target_attacks_are_combined(self):
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

    def test_game_type_formatting(self):
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

            self.assertEqual(archive_seen_games(db_path, matches, [log_path]), (1, 0))
            matches[0]["number"] = 99
            self.assertEqual(archive_seen_games(db_path, matches, [log_path]), (0, 1))

            con = sqlite3.connect(db_path)
            row = con.execute(
                """
                SELECT g.match_id, g.archive_index, g.game_type, g.has_result, t.content
                FROM games g
                JOIN transcripts t ON t.game_id = g.id AND t.format = 'plain_text'
                """
            ).fetchone()
            schema_version = con.execute("PRAGMA user_version").fetchone()[0]
            source_count = con.execute("SELECT count(*) FROM log_sources").fetchone()[0]
            con.close()

            self.assertEqual(schema_version, 1)
            self.assertEqual(source_count, 1)
            self.assertEqual(row[0], "match-1")
            self.assertEqual(row[1], 1)
            self.assertEqual(row[2], "Game type: Constructed Duel (20 starting life)")
            self.assertEqual(row[3], 1)
            self.assertIn("===== GAME 1: MATCH match-1 =====", row[4])
            self.assertIn("I attack Opponent with A and B", row[4])

            archived = archived_transcript_matches(db_path)
            self.assertEqual(archived[0]["number"], 1)
            self.assertEqual(archived[0]["match_id"], "match-1")
            self.assertIn("I attack Opponent with A and B", archived[0]["lines"])

    def test_default_archive_db_path_uses_current_directory(self):
        self.assertEqual(default_archive_db_path(), Path("mtga_seen_games.sqlite3"))

    def test_archive_schema_migrates_legacy_transcript_table(self):
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
            self.assertEqual(con.execute("SELECT count(*) FROM transcripts").fetchone()[0], 1)
            con.close()

    def test_path_resolution_uses_environment_paths(self):
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

    def test_live_path_warning_only_lists_current_log(self):
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
        missing_log = Path("/tmp/definitely-missing-mtga-player-log")
        missing_carddb = Path("/tmp/definitely-missing-mtga-carddb.mtga")
        with self.assertRaises(FileNotFoundError) as context:
            resolve_input_paths(missing_log, missing_carddb)

        message = str(context.exception)
        self.assertIn("Could not find Player.log", message)
        self.assertIn("export LOG=", message)
        self.assertIn("export CARDDB=", message)

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
        self.assertEqual(
            phrase_grouped_deaths("Me", "Shadowborn Apostle", 2),
            "2 of my Shadowborn Apostle tokens die",
        )

    def test_enters_attacking_wording(self):
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
        self.assertEqual(
            attack_phrase("Invasion of Zendikar", "Urza's Construction Drone"),
            "Invasion of Zendikar with Urza's Construction Drone",
        )
        self.assertEqual(
            attack_phrase(None, "Urza's Construction Drone"),
            "with Urza's Construction Drone",
        )

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
            ["Match result: Opponent conceded", "Match winner: Me"],
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
        self.assertEqual(phrase_choice_value("mana value parity", 0, "even"), "even")

    def test_incomplete_game_notice_is_conservative(self):
        self.assertEqual(
            phrase_incomplete_game_notice(
                "Postgame course/event data includes a loss count after this match."
            ),
            [
                "Game appears to have ended, but no final GRE result was written to Player.log.",
                "Postgame course/event data includes a loss count after this match.",
                "Final life total is unavailable from the gameplay log.",
            ],
        )

    def test_enum_loader_reads_creature_types_from_card_database(self):
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

    def test_card_metadata_loader_reads_types_and_resource_mechanics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            carddb = Path(tmpdir) / "carddb.mtga"
            con = sqlite3.connect(carddb)
            cur = con.cursor()
            cur.execute("CREATE TABLE Cards(GrpId INT, TitleId INT, Types TEXT, AbilityIds TEXT)")
            cur.execute("CREATE TABLE Abilities(Id INT, TextId INT)")
            cur.execute("CREATE TABLE Localizations_enUS(LocId INT, Formatted INT, Loc TEXT)")
            cur.executemany(
                "INSERT INTO Cards VALUES (?, ?, ?, ?)",
                [
                    (100, 1, "5", ""),
                    (200, 2, "10", "5301:3105"),
                    (300, 3, "2", "7000:1"),
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
        self.assertEqual(metadata[200]["play_mechanics"], ["flashback"])
        self.assertEqual(metadata[300]["play_mechanics"], ["escape"])

    def test_ability_text_loader_reads_modal_child_text(self):
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
        self.assertEqual(object_pronoun("Me"), "me")

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
        self.assertEqual(
            counter_summary_suffix(Counter({1: 2, 7: 1, 9: 0}), {1: "+1/+1"}),
            " (+2/+2 from counters; counter 7)",
        )
        self.assertEqual(scaled_power_toughness_counter("+1/+1", 38), "+38/+38")

    def test_attachment_summary_wording(self):
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
            modifier_summary_suffix(
                ["+1/+1 from counters", "enchanted by Crystal Carapace"]
            ),
            " (+1/+1 from counters; enchanted by Crystal Carapace)",
        )

    def test_ownership_summary_wording(self):
        self.assertIsNone(ownership_summary_part("Me", "Me"))
        self.assertEqual(ownership_summary_part("Opponent", "Me"), "owned by me")
        self.assertEqual(ownership_summary_part("Me", "Opponent"), "owned by Opponent")

    def test_repeated_hidden_card_wording(self):
        self.assertEqual(compact_counted_name("unknown card", 1), "unknown card")
        self.assertEqual(compact_counted_name("unknown card", 7), "7 unknown cards")
        self.assertEqual(
            compact_counted_name("a face-down card", 2),
            "2 face-down cards",
        )
        self.assertEqual(compact_counted_name("Island", 2), "2x Island")

    def test_target_phrase_wording(self):
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
        self.assertIn(
            "annotations[0].details[0].targetId",
            find_target_like_paths(
                {"annotations": [{"details": [{"targetId": 123}]}]}
            ),
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

    def test_missing_cast_inference_only_for_named_card_spells(self):
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
                "Heartless Act",
                12,
                {"type": "GameObjectType_Card"},
                emitted,
            )
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
        self.assertEqual(copied_object_label("Mesmeric Orb", True), "A copy of Mesmeric Orb")

    def test_ability_object_wording_uses_source_card(self):
        self.assertEqual(ability_object_label("Mesmeric Orb", True), "Mesmeric Orb trigger")
        self.assertEqual(ability_object_label("Cavern of Souls", False), "Cavern of Souls ability")

    def test_ability_source_instance_id_uses_parent(self):
        self.assertEqual(
            ability_source_instance_id(
                {"type": "GameObjectType_Ability", "parentId": 1407}
            ),
            1407,
        )
        self.assertIsNone(ability_source_instance_id({"type": "GameObjectType_Card"}))

    def test_hidden_arena_object_detection(self):
        self.assertTrue(is_hidden_arena_object({"isFacedown": True, "grpId": 3}))
        self.assertTrue(is_hidden_arena_object({"grpId": 3}))
        self.assertFalse(is_hidden_arena_object({"grpId": 90933}))

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
