#!/usr/bin/env python3
import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path


def load_grp_id_to_name(carddb_path: Path) -> dict[int, str]:
    """Load Arena GrpId -> English card name from Raw_CardDatabase_*.mtga."""
    if not carddb_path.exists():
        raise FileNotFoundError(f"Card database does not exist: {carddb_path}")
    if carddb_path.stat().st_size == 0:
        raise ValueError(f"Card database is empty; check the path: {carddb_path}")

    con = sqlite3.connect(carddb_path)
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    required_tables = {"Cards", "Localizations_enUS"}
    if not required_tables.issubset(tables):
        con.close()
        missing = ", ".join(sorted(required_tables - tables))
        raise ValueError(
            f"Card database is missing required table(s): {missing}. "
            f"Check that the carddb argument points to Raw_CardDatabase_*.mtga: {carddb_path}"
        )

    cur.execute("""
        SELECT c.GrpId, l.Loc, l.Formatted
        FROM Cards c
        JOIN Localizations_enUS l
          ON c.TitleId = l.LocId
        WHERE l.Formatted IN (0, 1)
        ORDER BY c.GrpId, l.Formatted
    """)
    mapping = {}
    for grp_id, name, _formatted in cur.fetchall():
        if name and int(grp_id) not in mapping:
            mapping[int(grp_id)] = name
    con.close()
    return mapping


def find_grp_ids_by_card_name(carddb_path: Path, card_name: str) -> dict[int, str]:
    """Find GrpIds whose English card title matches a user supplied name."""
    con = sqlite3.connect(carddb_path)
    cur = con.cursor()
    cur.execute("""
        SELECT c.GrpId, l.Loc, l.Formatted
        FROM Cards c
        JOIN Localizations_enUS l
          ON c.TitleId = l.LocId
        WHERE l.Formatted IN (0, 1)
        ORDER BY c.GrpId, l.Formatted
    """)
    by_grp = {}
    for grp_id, name, _formatted in cur.fetchall():
        if name and int(grp_id) not in by_grp:
            by_grp[int(grp_id)] = name
    con.close()

    needle = card_name.casefold()
    exact = {grp_id: name for grp_id, name in by_grp.items() if name.casefold() == needle}
    if exact:
        return exact
    return {grp_id: name for grp_id, name in by_grp.items() if needle in name.casefold()}


def subject_pronoun(label: str) -> str:
    return "I" if label == "Me" else label


def object_pronoun(label: str) -> str:
    return "me" if label == "Me" else label


def possessive_pronoun(label: str) -> str:
    if label == "Me":
        return "My"
    if label:
        return f"{label}'s"
    return ""


def present_tense_verb(label: str, base: str, third_person: str | None = None) -> str:
    if label == "Me":
        return base
    return third_person or f"{base}s"


def phrase_player_action(label: str, base_verb: str, rest: str, third_person=None) -> str:
    return (
        f"{subject_pronoun(label)} "
        f"{present_tense_verb(label, base_verb, third_person)} {rest}"
    )


def phrase_life_change(label: str, delta: int, total=None) -> str:
    verb = "gain" if delta > 0 else "lose"
    amount = abs(delta)
    suffix = f" ({total})" if total is not None else ""
    return phrase_player_action(label, verb, f"{amount} life{suffix}")


def phrase_death(controller_label: str | None, card_name: str) -> str:
    possessive = possessive_pronoun(controller_label or "")
    if not possessive:
        return f"{card_name} dies"
    return f"{possessive} {card_name} dies"


def phrase_zone_change(source: str | None, verb: str, target: str) -> str:
    passive = {
        "destroy": "destroyed",
        "exile": "exiled",
        "counter": "countered",
    }
    if source:
        return f"{source} {present_tense_verb('Opponent', verb)} {target}"
    return f"{target} is {passive.get(verb, verb)}"


def phrase_concede_result(winner: str, scope_text: str) -> list[str]:
    loser = "Opponent" if winner == "Me" else "Me" if winner == "Opponent" else None
    if loser == "Me":
        concession = "I concede" if scope_text == "game" else "Match result: I conceded"
    elif loser == "Opponent":
        concession = (
            "Opponent concedes"
            if scope_text == "game"
            else "Match result: Opponent conceded"
        )
    else:
        concession = (
            "Opponent concedes"
            if scope_text == "game"
            else "Match result: opponent conceded"
        )
    return [concession, f"Winner: {winner}"]


def extract_game_plays(
    player_log: Path,
    grp_to_name: dict[int, str],
    *,
    debug_annotations: bool = False,
    debug_grp_ids: set[int] | None = None,
    debug_choices: bool = False,
    select_game: int | None = None,
    last_games: int | None = None,
    show_progress: bool | None = None,
    show_resolves: bool = True,
    show_turn_state: bool = True,
) -> None:
    """Extract a readable play transcript from MTGA Player.log."""
    zones = {}
    objects = {}
    players = {}
    team_to_seats = {}
    seen_annotations = set()
    seen_combat = set()
    seen_life_changes = set()
    seen_mulligans = set()
    seen_results = set()
    seen_choices = set()
    seen_state_events = set()
    active_effects = {}
    current_match = None
    current_turn = None
    last_game_state_id = None
    known_local_seat = None
    current_match_number = 0
    current_match_lines = None
    current_match_record = None
    transcript_matches = []
    event_index = 0
    debug_grp_ids = debug_grp_ids or set()

    debug_counts = Counter()
    debug_samples = {}
    if show_progress is None:
        show_progress = (
            sys.stderr.isatty()
            and not sys.stdout.isatty()
            and player_log.exists()
            and player_log.stat().st_size >= 10 * 1024 * 1024
        )
    total_bytes = player_log.stat().st_size if player_log.exists() else 0
    read_bytes = 0
    last_progress_at = 0.0

    death_categories = {
        "SBA_Damage",
        "SBA_Deathtouch",
        "SBA_ZeroLoyalty",
        "SBA_ZeroToughness",
        "SBA_UnattachedAura",
    }
    choice_domain_names = {
        4: "card type",
        5: "creature type",
        6: "color",
    }
    choice_value_names = {
        # Observed in current GRE logs:
        # Serra's Emissary: domain 4, value 2 -> Creature.
        # Patchwork Banner / Vanquisher's Banner / Cavern of Souls:
        # domain 5, value 1 -> Angel.
        # Nyx Lotus / Nykthos: domain 6, value 1 -> White.
        4: {
            1: "Artifact",
            2: "Creature",
            3: "Enchantment",
            4: "Instant",
            5: "Land",
            6: "Planeswalker",
            7: "Sorcery",
            8: "Battle",
        },
        5: {
            1: "Angel",
        },
        6: {
            1: "White",
            2: "Blue",
            3: "Black",
            4: "Red",
            5: "Green",
            6: "Colorless",
        },
    }

    def emit(line=""):
        if current_match_lines is not None:
            current_match_lines.append(line)

    def add_active_effect(key, text, source_id=None, until=None):
        active_effects[key] = {
            "text": text,
            "source_id": source_id,
            "until": until,
        }

    def remove_active_effects_for_source(source_id):
        if source_id is None:
            return
        for key, effect in list(active_effects.items()):
            if effect.get("source_id") == source_id:
                active_effects.pop(key, None)

    def remove_active_effects_with_prefix(prefix):
        for key in list(active_effects):
            if isinstance(key, tuple) and key[: len(prefix)] == prefix:
                active_effects.pop(key, None)

    def detail_value(detail):
        if not detail:
            return None
        for key in (
            "valueString",
            "valueInt32",
            "valueBool",
            "valueFloat",
            "valueDouble",
        ):
            values = detail.get(key)
            if values:
                return values[0] if len(values) == 1 else values
        return None

    def detail_dict(details):
        return {d.get("key"): detail_value(d) for d in details or []}

    def normalize_for_key(value):
        if isinstance(value, dict):
            return tuple(
                (k, normalize_for_key(v))
                for k, v in sorted(value.items())
                if k not in {"timestamp"}
            )
        if isinstance(value, list):
            return tuple(normalize_for_key(v) for v in value)
        return value

    def annotation_key(ann, gsm, msg):
        ann_id = ann.get("id")
        if ann_id is not None:
            return ("id", current_match, ann_id)

        # Older/current logs may omit annotation ids. Use the enclosing game
        # state plus annotation payload so one missing id does not suppress all
        # later missing-id annotations.
        return (
            "synthetic",
            current_match,
            gsm.get("gameStateId"),
            msg.get("msgId"),
            tuple(ann.get("type") or []),
            ann.get("affectorId"),
            tuple(ann.get("affectedIds") or []),
            normalize_for_key(ann.get("details") or []),
        )

    def owner_label(seat):
        if known_local_seat is not None:
            if seat == known_local_seat:
                return "Me"
            if seat in (1, 2):
                return "Opponent"
        if seat == 1:
            return "Player 1"
        if seat == 2:
            return "Player 2"
        return f"Seat {seat}"

    def team_label(team_id):
        seats = team_to_seats.get(team_id) or []
        if len(seats) == 1:
            return owner_label(seats[0])
        if seats:
            return "Team " + "/".join(str(seat) for seat in seats)
        return f"Team {team_id}"

    def zone_owner(zone_id):
        zone = zones.get(zone_id, {})
        return zone.get("ownerSeatId")

    def object_owner(instance_id):
        obj = objects.get(instance_id, {})
        return obj.get("controllerSeatId") or obj.get("ownerSeatId")

    def card_name_from_grp(grp_id):
        if grp_id is None:
            return None
        try:
            grp_id = int(grp_id)
        except (TypeError, ValueError):
            return None
        return grp_to_name.get(grp_id, f"grpId {grp_id}")

    def card_label(instance_id, fallback_grp_id=None):
        obj = objects.get(instance_id, {})
        return (
            card_name_from_grp(obj.get("grpId"))
            or card_name_from_grp(obj.get("objectSourceGrpId"))
            or card_name_from_grp(fallback_grp_id)
            or f"instance {instance_id}"
        )

    def target_label(target_id):
        if target_id in (1, 2):
            return object_pronoun(owner_label(target_id))
        return card_label(target_id)

    def source_label(instance_id):
        if instance_id is None:
            return None
        label = card_label(instance_id)
        if label == f"instance {instance_id}" or label.startswith("grpId "):
            return None
        return label

    def event_owner(ann, details):
        src_owner = zone_owner(details.get("zone_src"))
        dst_owner = zone_owner(details.get("zone_dest"))
        affected = ann.get("affectedIds") or []
        affected_owner = object_owner(affected[0]) if affected else None
        affector_owner = object_owner(ann.get("affectorId"))
        return owner_label(src_owner or dst_owner or affected_owner or affector_owner)

    def is_command_zone(zone_id):
        return zones.get(zone_id, {}).get("type") == "ZoneType_Command"

    def zone_ids_by_type(zone_type):
        return {
            zone_id
            for zone_id, zone in zones.items()
            if zone.get("type") == zone_type
        }

    def compact_names(names, unknown_label="unknown card"):
        counts = Counter(name or unknown_label for name in names)
        if not counts:
            return "(empty)"
        parts = []
        for name in sorted(counts):
            count = counts[name]
            parts.append(f"{count}x {name}" if count > 1 else name)
        return "; ".join(parts)

    def card_label_for_snapshot(instance_id):
        obj = objects.get(instance_id, {})
        if not obj.get("grpId") and not obj.get("objectSourceGrpId"):
            return "unknown card"
        return card_label(instance_id)

    def battlefield_names(seat):
        battlefield_zone_ids = zone_ids_by_type("ZoneType_Battlefield")
        names = []
        for obj in objects.values():
            if obj.get("zoneId") not in battlefield_zone_ids:
                continue
            if obj.get("type") not in {"GameObjectType_Card", "GameObjectType_Token"}:
                continue
            controller = obj.get("controllerSeatId") or obj.get("ownerSeatId")
            if controller == seat:
                names.append(card_label_for_snapshot(obj.get("instanceId")))
        return names

    def zone_names(zone_type, seat=None):
        names = []
        for zone in zones.values():
            if zone.get("type") != zone_type:
                continue
            if seat is not None and zone.get("ownerSeatId") != seat:
                continue
            for instance_id in zone.get("objectInstanceIds") or []:
                names.append(card_label_for_snapshot(instance_id))
        return names

    def hand_names(seat):
        names = []
        for zone in zones.values():
            if zone.get("type") != "ZoneType_Hand":
                continue
            if zone.get("ownerSeatId") != seat:
                continue
            for instance_id in zone.get("objectInstanceIds") or []:
                names.append(card_label_for_snapshot(instance_id))
        return names

    def infer_local_seat():
        candidates = []
        for seat in (1, 2):
            names = hand_names(seat)
            if names and "unknown card" not in names:
                candidates.append(seat)
        return candidates[0] if len(candidates) == 1 else None

    def emit_turn_state():
        if not show_turn_state:
            return
        emit("Board:")
        emit(f"  {owner_label(1)}: {compact_names(battlefield_names(1))}")
        emit(f"  {owner_label(2)}: {compact_names(battlefield_names(2))}")
        emit("Hands:")
        emit(f"  {owner_label(1)}: {compact_names(hand_names(1))}")
        emit(f"  {owner_label(2)}: {compact_names(hand_names(2))}")
        emit("Command:")
        emit(f"  {owner_label(1)}: {compact_names(zone_names('ZoneType_Command', 1))}")
        emit(f"  {owner_label(2)}: {compact_names(zone_names('ZoneType_Command', 2))}")
        emit("Graveyard:")
        emit(f"  {owner_label(1)}: {compact_names(zone_names('ZoneType_Graveyard', 1))}")
        emit(f"  {owner_label(2)}: {compact_names(zone_names('ZoneType_Graveyard', 2))}")
        emit("Exile:")
        emit(f"  {owner_label(1)}: {compact_names(zone_names('ZoneType_Exile', 1))}")
        emit(f"  {owner_label(2)}: {compact_names(zone_names('ZoneType_Exile', 2))}")
        if active_effects:
            emit("Active Effects:")
            for effect in sorted(active_effects.values(), key=lambda item: item["text"]):
                emit(f"  {effect['text']}")

    def emit_zone_transfer(ann):
        details = detail_dict(ann.get("details"))
        category = details.get("category")
        affected = ann.get("affectedIds") or []
        if not category or not affected:
            return

        iid = affected[0]
        owner = event_owner(ann, details)
        name = card_label(iid, details.get("grpid"))
        from_command = is_command_zone(details.get("zone_src"))

        if category == "PlayLand":
            emit(phrase_player_action(owner, "play", name))
        elif category == "CastSpell":
            suffix = " from command zone" if from_command else ""
            emit(phrase_player_action(owner, "cast", f"{name}{suffix}"))
        elif category == "Resolve":
            if show_resolves:
                emit(f"{name} resolves")
        elif category in death_categories:
            controller = object_owner(iid)
            emit(phrase_death(owner_label(controller) if controller else None, name))
            remove_active_effects_for_source(iid)
        elif category in {"Destroy", "DestroyNoRegenerate"}:
            source = source_label(ann.get("affectorId"))
            if source and ann.get("affectorId") != iid and source != name:
                emit(phrase_zone_change(source, "destroy", name))
            else:
                emit(phrase_zone_change(None, "destroy", name))
            remove_active_effects_for_source(iid)
        elif category == "Exile":
            source = source_label(ann.get("affectorId"))
            if source and ann.get("affectorId") != iid and source != name:
                emit(phrase_zone_change(source, "exile", name))
            else:
                emit(phrase_zone_change(None, "exile", name))
            remove_active_effects_for_source(iid)
        elif category == "Countered":
            emit(phrase_zone_change(None, "counter", name))
            remove_active_effects_for_source(iid)
        elif category == "Discard":
            emit(phrase_player_action(owner, "discard", name))
        elif category == "Sacrifice":
            emit(phrase_player_action(owner, "sacrifice", name))
            remove_active_effects_for_source(iid)

    def emit_life_change(ann, gsm):
        details = detail_dict(ann.get("details"))
        delta = details.get("life")
        affected = ann.get("affectedIds") or []
        if not affected or not isinstance(delta, int) or delta == 0:
            return

        seat = affected[0]
        key = (current_match, gsm.get("gameStateId"), ann.get("id"), seat, delta)
        if key in seen_life_changes:
            return
        seen_life_changes.add(key)

        total = players.get(seat, {}).get("lifeTotal")
        emit(phrase_life_change(owner_label(seat), delta, total))

    def emit_choice_result(ann, gsm):
        details = detail_dict(ann.get("details"))
        domain = details.get("Choice_Domain")
        value = details.get("Choice_Value")
        if not isinstance(domain, int) or not isinstance(value, int):
            return

        source_id = ann.get("affectorId")
        source = card_label(source_id)
        seat = object_owner(source_id)
        affected = ann.get("affectedIds") or []
        chooser = owner_label(seat or (affected[0] if affected else None))
        domain_text = choice_domain_names.get(domain, f"choice domain {domain}")
        value_text = choice_value_names.get(domain, {}).get(value, f"value {value}")

        key = (
            current_match,
            gsm.get("gameStateId"),
            ann.get("id"),
            source_id,
            domain,
            value,
        )
        if key in seen_choices:
            return
        seen_choices.add(key)

        emit(
            phrase_player_action(
                chooser,
                "choose",
                f"{value_text} for {source} ({domain_text})",
                third_person="chooses",
            )
        )
        if source == "Serra's Emissary" and domain == 4:
            effect_text = (
                f"{subject_pronoun(chooser)} "
                f"{present_tense_verb(chooser, 'have', 'has')} "
                f"protection from {value_text.lower()}s via {source}"
            )
            state_key = ("serra_protection", source_id)
            add_active_effect(state_key, effect_text, source_id=source_id)
            emit(effect_text)

    def emit_combat_events(gsm):
        if gsm.get("turnInfo", {}).get("phase") != "Phase_Combat":
            return

        for obj in gsm.get("gameObjects", []):
            iid = obj.get("instanceId")
            if iid is None:
                continue

            if obj.get("attackState") == "AttackState_Attacking":
                target_id = (obj.get("attackInfo") or {}).get("targetId")
                key = (current_match, current_turn, "attack", iid, target_id)
                if key not in seen_combat:
                    seen_combat.add(key)
                    emit(
                        phrase_player_action(
                            owner_label(obj.get("controllerSeatId")),
                            "attack",
                            f"{target_label(target_id)} with {card_label(iid)}",
                        )
                    )

            if obj.get("blockState") in {"BlockState_Declared", "BlockState_Blocking"}:
                attacker_ids = (obj.get("blockInfo") or {}).get("attackerIds") or []
                for attacker_id in attacker_ids:
                    key = (current_match, current_turn, "block", iid, attacker_id)
                    if key not in seen_combat:
                        seen_combat.add(key)
                        emit(f"{card_label(iid)} blocks {card_label(attacker_id)}")

    def emit_continuous_effect_events(gsm):
        annotations = gsm.get("annotations") or []
        if not annotations:
            return

        teferi_sources = {
            ann.get("affectorId")
            for ann in annotations
            if "AnnotationType_PhasedOut" in set(ann.get("type") or [])
            and card_label(ann.get("affectorId")) == "Teferi's Protection"
        }
        if teferi_sources:
            phased_counts = Counter()
            for ann in annotations:
                if "AnnotationType_PhasedOut" not in set(ann.get("type") or []):
                    continue
                for affected_id in ann.get("affectedIds") or []:
                    owner = object_owner(affected_id)
                    if owner is not None:
                        phased_counts[owner] += 1

            for seat, count in sorted(phased_counts.items()):
                source_id = next(iter(teferi_sources))
                source = card_label(source_id)
                label = owner_label(seat)
                key = (current_match, gsm.get("gameStateId"), "teferi_phase", seat)
                if key not in seen_state_events:
                    seen_state_events.add(key)
                    plural = "permanent" if count == 1 else "permanents"
                    emit(
                        phrase_player_action(
                            label,
                            "phase",
                            f"out {count} {plural} via {source}",
                            third_person="phases",
                        )
                    )

                protection_text = (
                    f"{subject_pronoun(label)} "
                    f"{present_tense_verb(label, 'have', 'has')} "
                    f"protection from everything until next turn via {source}"
                )
                life_text = (
                    f"{possessive_pronoun(label)} life total can't change "
                    f"until next turn via {source}"
                )
                add_active_effect(
                    ("teferi_protection", seat),
                    protection_text,
                    source_id=source_id,
                    until="next_turn",
                )
                add_active_effect(
                    ("teferi_life_total", seat),
                    life_text,
                    source_id=source_id,
                    until="next_turn",
                )
                for text in (protection_text, life_text):
                    state_key = (current_match, gsm.get("gameStateId"), text)
                    if state_key not in seen_state_events:
                        seen_state_events.add(state_key)
                        emit(text)

        phased_in_counts = Counter()
        for ann in annotations:
            if "AnnotationType_PhasedIn" not in set(ann.get("type") or []):
                continue
            for affected_id in ann.get("affectedIds") or []:
                owner = object_owner(affected_id)
                if owner is not None:
                    phased_in_counts[owner] += 1

        for seat, count in sorted(phased_in_counts.items()):
            label = owner_label(seat)
            key = (current_match, gsm.get("gameStateId"), "phase_in", seat)
            if key in seen_state_events:
                continue
            seen_state_events.add(key)
            plural = "permanent" if count == 1 else "permanents"
            emit(f"{possessive_pronoun(label)} {count} phased-out {plural} phase in")
            remove_active_effects_with_prefix(("teferi_protection", seat))
            remove_active_effects_with_prefix(("teferi_life_total", seat))

    def update_state(gsm):
        for team in gsm.get("teams", []):
            team_id = team.get("id")
            if team_id is not None:
                team_to_seats[team_id] = list(team.get("playerIds") or [])

        for zone in gsm.get("zones", []):
            if zone.get("zoneId") is not None:
                zones[zone["zoneId"]] = zone

        for obj in gsm.get("gameObjects", []):
            if obj.get("instanceId") is not None:
                objects[obj["instanceId"]] = obj

        for player in gsm.get("players", []):
            seat = player.get("systemSeatNumber")
            if seat is None:
                continue
            old = players.get(seat, {})
            players[seat] = {**old, **player}

            mulligans = player.get("mulliganCount")
            if mulligans is not None and mulligans != old.get("mulliganCount"):
                key = (current_match, seat, mulligans)
                if key not in seen_mulligans:
                    seen_mulligans.add(key)
                    emit(
                        phrase_player_action(
                            owner_label(seat),
                            "mulligan",
                            f"to {7 - int(mulligans)}",
                        )
                    )

        for deleted_id in gsm.get("diffDeletedInstanceIds") or []:
            objects.pop(deleted_id, None)

    def emit_match_results(gsm):
        for result in gsm.get("gameInfo", {}).get("results") or []:
            scope = result.get("scope")
            winning_team_id = result.get("winningTeamId")
            reason = result.get("reason")
            result_type = result.get("result")
            key = (current_match, scope, winning_team_id, reason, result_type)
            if key in seen_results:
                continue
            seen_results.add(key)

            winner = team_label(winning_team_id)
            scope_text = (scope or "Result").replace("MatchScope_", "").lower()
            reason_text = (reason or result_type or "unknown").replace("ResultReason_", "")
            reason_text = reason_text.replace("ResultType_", "").lower()
            emit("")
            if reason_text == "concede":
                for line in phrase_concede_result(winner, scope_text):
                    emit(line)
            else:
                emit(f"Winner: {winner} ({scope_text}, {reason_text})")

    def record_debug(ann):
        details = detail_dict(ann.get("details"))
        category = details.get("category") or details.get("type") or ""
        key = (tuple(ann.get("type") or []), category)
        debug_counts[key] += 1
        if key not in debug_samples:
            debug_samples[key] = {
                "id": ann.get("id"),
                "type": ann.get("type"),
                "affectorId": ann.get("affectorId"),
                "affectedIds": ann.get("affectedIds"),
                "details": ann.get("details"),
            }

    def compact_json(value):
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return text if len(text) <= 220 else text[:217] + "..."

    def contains_choice_marker(value, path=()):
        needles = (
            "choice",
            "selected",
            "selection",
            "option",
            "cardtype",
            "cardtype_",
            "subtype",
            "prompt",
            "modal",
            "protection",
            "typeline",
        )
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key)
                key_folded = key_text.casefold()
                child_path = path + (key_text,)
                in_game_object = "gameObjects" in path
                # Normal card objects always carry cardTypes/subtypes; those
                # are card identity data, not player choice records.
                if in_game_object and key_text in {"cardTypes", "subtypes", "superTypes"}:
                    continue
                if any(needle in key_folded for needle in needles):
                    return True
                if contains_choice_marker(item, child_path):
                    return True
        elif isinstance(value, list):
            return any(contains_choice_marker(item, path) for item in value)
        elif isinstance(value, str):
            text = value.casefold()
            return any(needle in text for needle in needles)
        return False

    def object_debug_grp_id(obj):
        for key in ("grpId", "objectSourceGrpId"):
            grp_id = obj.get(key)
            if grp_id is None:
                continue
            try:
                grp_id = int(grp_id)
            except (TypeError, ValueError):
                continue
            if grp_id in debug_grp_ids:
                return grp_id
        return None

    def record_gameplay_event(msg, gsm):
        """Store only GRE/gameState payloads, filtering out startup inventory/deck blobs."""
        if current_match_record is None:
            return

        payload = {"greToClientMessage": msg}
        event = {
            "event_index": event_index,
            "turn": current_turn,
            "gameStateId": gsm.get("gameStateId"),
            "payload": payload,
        }
        current_match_record["events"].append(event)

        # Debug card flow:
        # card name -> SQLite lookup to GrpId -> gameObjects instanceId -> nearby raw GRE events.
        for obj in gsm.get("gameObjects", []):
            grp_id = object_debug_grp_id(obj)
            if grp_id is None:
                continue
            instance_id = obj.get("instanceId")
            hit_key = (grp_id, instance_id, obj.get("zoneId"))
            if hit_key in current_match_record["debug_seen_objects"]:
                continue
            current_match_record["debug_seen_objects"].add(hit_key)
            current_match_record["debug_hits"].append(
                {
                    "event_index": event_index,
                    "turn": current_turn,
                    "gameStateId": gsm.get("gameStateId"),
                    "grpId": grp_id,
                    "card": card_name_from_grp(grp_id),
                    "instanceId": instance_id,
                    "objectId": obj.get("objectId") or obj.get("id"),
                    "zoneId": obj.get("zoneId"),
                    "controller": obj.get("controllerSeatId"),
                    "owner": obj.get("ownerSeatId"),
                    "object": obj,
                }
            )

        if debug_choices and contains_choice_marker(payload):
            current_match_record["choice_events"].append(event)

    def render_progress(force=False):
        nonlocal last_progress_at
        if not show_progress or total_bytes <= 0:
            return

        now = time.monotonic()
        if not force and now - last_progress_at < 0.2:
            return
        last_progress_at = now

        ratio = min(read_bytes / total_bytes, 1.0)
        width = 28
        filled = int(width * ratio)
        bar = "#" * filled + "-" * (width - filled)
        read_mb = read_bytes / (1024 * 1024)
        total_mb = total_bytes / (1024 * 1024)
        print(
            f"\rParsing Player.log [{bar}] {ratio:6.1%} "
            f"({read_mb:.1f}/{total_mb:.1f} MB)",
            end="",
            file=sys.stderr,
            flush=True,
        )

    with player_log.open("rb") as f:
        for raw_line in f:
            read_bytes += len(raw_line)
            render_progress()

            line = raw_line.decode("utf-8", errors="ignore")
            line = line.strip()
            if not line.startswith("{"):
                continue

            try:
                root = json.loads(line)
            except json.JSONDecodeError:
                continue

            for msg in root.get("greToClientEvent", {}).get("greToClientMessages", []):
                gsm = msg.get("gameStateMessage")
                if not gsm:
                    continue

                match_id = gsm.get("gameInfo", {}).get("matchID")
                if match_id and match_id != current_match:
                    current_match = match_id
                    current_match_number += 1
                    current_match_lines = []
                    current_match_record = {
                        "number": current_match_number,
                        "match_id": current_match,
                        "lines": current_match_lines,
                        "events": [],
                        "debug_hits": [],
                        "debug_seen_objects": set(),
                        "choice_events": [],
                    }
                    transcript_matches.append(current_match_record)
                    event_index = 0
                    current_turn = None
                    last_game_state_id = None
                    known_local_seat = None
                    zones.clear()
                    objects.clear()
                    players.clear()
                    team_to_seats.clear()
                    seen_combat.clear()
                    active_effects.clear()
                    emit(f"===== GAME {current_match_number}: MATCH {current_match} =====")

                game_state_id = gsm.get("gameStateId")
                if (
                    game_state_id is not None
                    and last_game_state_id is not None
                    and game_state_id < last_game_state_id
                ):
                    continue
                if game_state_id is not None:
                    last_game_state_id = game_state_id

                update_state(gsm)
                if known_local_seat is None:
                    known_local_seat = infer_local_seat()
                emit_match_results(gsm)

                turn_info = gsm.get("turnInfo", {})
                if "turnNumber" in turn_info and turn_info["turnNumber"] != current_turn:
                    current_turn = turn_info["turnNumber"]
                    emit("")
                    emit(
                        f"=== Turn {current_turn}: "
                        f"{owner_label(turn_info.get('activePlayer'))} ==="
                    )
                    emit_turn_state()

                event_index += 1
                record_gameplay_event(msg, gsm)
                emit_continuous_effect_events(gsm)
                emit_combat_events(gsm)

                for ann in gsm.get("annotations", []):
                    if debug_annotations:
                        record_debug(ann)

                    key = annotation_key(ann, gsm, msg)
                    if key in seen_annotations:
                        continue
                    seen_annotations.add(key)

                    ann_types = set(ann.get("type") or [])
                    if "AnnotationType_ZoneTransfer" in ann_types:
                        emit_zone_transfer(ann)
                    elif "AnnotationType_ModifiedLife" in ann_types:
                        emit_life_change(ann, gsm)
                    elif "AnnotationType_ChoiceResult" in ann_types:
                        emit_choice_result(ann, gsm)

    if show_progress:
        render_progress(force=True)
        print(file=sys.stderr)

    if debug_annotations:
        print("\nAnnotation summary:", file=sys.stderr)
        for (types, category), count in debug_counts.most_common():
            type_text = ",".join(types) if types else "<none>"
            cat_text = f" category={category!r}" if category else ""
            sample = json.dumps(debug_samples[(types, category)], sort_keys=True)
            print(f"{count:5d} {type_text}{cat_text} sample={sample}", file=sys.stderr)

    selected_matches = transcript_matches
    if select_game is not None:
        selected_matches = [
            match for match in transcript_matches if match["number"] == select_game
        ]
        if not selected_matches:
            print(
                f"No game {select_game}; found {len(transcript_matches)} game(s).",
                file=sys.stderr,
            )
    elif last_games is not None:
        selected_matches = transcript_matches[-last_games:]

    first = True
    for match in selected_matches:
        if not first:
            print()
            print()
        first = False
        print("\n".join(match["lines"]))

    if debug_grp_ids:
        print("\nDebug GrpId object windows:", file=sys.stderr)
        for match in selected_matches:
            print(
                f"\n===== DEBUG GAME {match['number']}: MATCH {match['match_id']} =====",
                file=sys.stderr,
            )
            if not match["debug_hits"]:
                print("No objects with requested debug grpIds appeared.", file=sys.stderr)
                continue

            events = match["events"]
            event_positions = {
                event["event_index"]: index for index, event in enumerate(events)
            }
            for hit in match["debug_hits"]:
                position = event_positions.get(hit["event_index"])
                if position is None:
                    continue
                start = max(0, position - 10)
                end = min(len(events), position + 31)
                print(
                    "\n"
                    f"--- grpId {hit['grpId']} ({hit['card']}) "
                    f"instanceId={hit['instanceId']} objectId={hit['objectId']} "
                    f"zoneId={hit['zoneId']} controller={hit['controller']} "
                    f"owner={hit['owner']} turn={hit['turn']} "
                    f"event={hit['event_index']} ---",
                    file=sys.stderr,
                )
                print("Tracked object snapshot:", file=sys.stderr)
                print(json.dumps(hit["object"], indent=2, sort_keys=True), file=sys.stderr)
                print(
                    f"Raw GRE/gameState event window: {events[start]['event_index']}.."
                    f"{events[end - 1]['event_index']}",
                    file=sys.stderr,
                )
                for event in events[start:end]:
                    marker = " <== object first seen" if event["event_index"] == hit["event_index"] else ""
                    print(
                        f"\n### event {event['event_index']} "
                        f"turn={event['turn']} gameStateId={event['gameStateId']}{marker}",
                        file=sys.stderr,
                    )
                    print(
                        json.dumps(event["payload"], indent=2, sort_keys=True),
                        file=sys.stderr,
                    )

    if debug_choices:
        print("\nDebug choice-like GRE/gameState events:", file=sys.stderr)
        for match in selected_matches:
            print(
                f"\n===== CHOICES GAME {match['number']}: MATCH {match['match_id']} =====",
                file=sys.stderr,
            )
            if not match["choice_events"]:
                print("No choice-like payloads found.", file=sys.stderr)
                continue
            for event in match["choice_events"]:
                print(
                    f"\n### event {event['event_index']} "
                    f"turn={event['turn']} gameStateId={event['gameStateId']}",
                    file=sys.stderr,
                )
                print(
                    json.dumps(event["payload"], indent=2, sort_keys=True),
                    file=sys.stderr,
                )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract a readable MTG Arena transcript from Player.log."
    )
    parser.add_argument("player_log", type=Path)
    parser.add_argument("carddb", type=Path)
    parser.add_argument(
        "--debug-annotations",
        action="store_true",
        help="print annotation type/category counts and example payloads to stderr",
    )
    parser.add_argument(
        "--debug-grpid",
        type=int,
        action="append",
        default=[],
        metavar="N",
        help="dump raw gameplay event windows around objects with this GrpId",
    )
    parser.add_argument(
        "--debug-card",
        action="append",
        default=[],
        metavar="NAME",
        help="look up a card name in the SQLite card DB and debug its GrpId(s)",
    )
    parser.add_argument(
        "--debug-choices",
        action="store_true",
        help="print GRE/gameState events containing likely choice or selection fields",
    )
    selection_group = parser.add_mutually_exclusive_group()
    selection_group.add_argument(
        "--select",
        type=int,
        metavar="N",
        help="output only game N from the log, using 1-based log order",
    )
    selection_group.add_argument(
        "--last",
        type=int,
        metavar="N",
        help="output only the last N games from the log",
    )
    progress_group = parser.add_mutually_exclusive_group()
    progress_group.add_argument(
        "--progress",
        action="store_true",
        help="show a progress bar on stderr while parsing",
    )
    progress_group.add_argument(
        "--no-progress",
        action="store_true",
        help="suppress the progress bar",
    )
    parser.add_argument(
        "--no-resolves",
        action="store_true",
        help="suppress 'resolves' transcript lines",
    )
    parser.add_argument(
        "--no-turn-state",
        action="store_true",
        help="suppress board and hand snapshots at turn starts",
    )
    args = parser.parse_args()

    player_log = args.player_log.expanduser()
    carddb = args.carddb.expanduser()

    if args.select is not None and args.select < 1:
        parser.error("--select must be 1 or greater")
    if args.last is not None and args.last < 1:
        parser.error("--last must be 1 or greater")

    grp_to_name = load_grp_id_to_name(carddb)
    debug_grp_ids = set(args.debug_grpid)
    for card_name in args.debug_card:
        matches = find_grp_ids_by_card_name(carddb, card_name)
        if not matches:
            parser.error(f"--debug-card did not match any card name: {card_name}")
        debug_grp_ids.update(matches)
        match_text = ", ".join(
            f"{grp_id}={name}" for grp_id, name in sorted(matches.items())
        )
        print(f"--debug-card {card_name!r} matched {match_text}", file=sys.stderr)

    extract_game_plays(
        player_log,
        grp_to_name,
        debug_annotations=args.debug_annotations,
        debug_grp_ids=debug_grp_ids,
        debug_choices=args.debug_choices,
        select_game=args.select,
        last_games=args.last,
        show_progress=True if args.progress else False if args.no_progress else None,
        show_resolves=not args.no_resolves,
        show_turn_state=not args.no_turn_state,
    )


if __name__ == "__main__":
    main()
