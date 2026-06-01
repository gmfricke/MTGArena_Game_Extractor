#!/usr/bin/env python3
import argparse
import json
import re
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


def clean_localized_enum_name(name: str | None) -> str | None:
    """Strip simple Arena localization markup from enum labels."""
    if not name:
        return None
    return re.sub(r"</?nobr>", "", name).strip()


def load_enum_value_names(carddb_path: Path, enum_type: str) -> dict[int, str]:
    """Load Arena enum value names, such as SubType 25 -> Elemental."""
    con = sqlite3.connect(carddb_path)
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    if not {"Enums", "Localizations_enUS"}.issubset(tables):
        con.close()
        return {}

    cur.execute(
        """
        SELECT e.Value, l.Loc, l.Formatted
        FROM Enums e
        JOIN Localizations_enUS l
          ON e.LocId = l.LocId
        WHERE e.Type = ?
        ORDER BY
          e.Value,
          CASE l.Formatted
            WHEN 0 THEN 0
            WHEN 2 THEN 1
            WHEN 1 THEN 2
            ELSE 3
          END
        """,
        (enum_type,),
    )
    mapping = {}
    for value, name, _formatted in cur.fetchall():
        clean_name = clean_localized_enum_name(name)
        if clean_name and int(value) not in mapping:
            mapping[int(value)] = clean_name
    con.close()
    return mapping


def subject_pronoun(label: str) -> str:
    """Return the narrative subject form for a player label."""
    return "I" if label == "Me" else label


def object_pronoun(label: str) -> str:
    """Return the narrative object form for a player label."""
    return "me" if label == "Me" else label


def possessive_pronoun(label: str) -> str:
    """Return a short possessive phrase for a player label."""
    if label == "Me":
        return "My"
    if label:
        return f"{label}'s"
    return ""


def present_tense_verb(label: str, base: str, third_person: str | None = None) -> str:
    """Conjugate a simple present-tense verb for first-person Me vs others."""
    if label == "Me":
        return base
    return third_person or f"{base}s"


def phrase_player_action(label: str, base_verb: str, rest: str, third_person=None) -> str:
    """Build a deterministic narrative sentence for a player action."""
    return (
        f"{subject_pronoun(label)} "
        f"{present_tense_verb(label, base_verb, third_person)} {rest}"
    )


def phrase_life_change(label: str, delta: int, total=None) -> str:
    """Render a life total change in the current narrative voice."""
    verb = "gain" if delta > 0 else "lose"
    amount = abs(delta)
    suffix = f" ({total})" if total is not None else ""
    return phrase_player_action(label, verb, f"{amount} life{suffix}")


def phrase_death(controller_label: str | None, card_name: str) -> str:
    """Render a permanent dying, using possessive attribution when known."""
    possessive = possessive_pronoun(controller_label or "")
    if not possessive:
        return f"{card_name} dies"
    return f"{possessive} {card_name} dies"


def phrase_grouped_deaths(controller_label: str | None, card_name: str, count: int) -> str:
    """Render consecutive identical token deaths as one compact line."""
    if count <= 1:
        return phrase_death(controller_label, card_name)
    if controller_label == "Me":
        owner = "my"
    elif controller_label == "Opponent":
        owner = "opponent"
    elif controller_label:
        owner = f"{controller_label}'s"
    else:
        owner = ""
    prefix = f"{count} {owner} ".strip()
    return f"{prefix} {card_name} tokens die"


def phrase_zone_change(source: str | None, verb: str, target: str) -> str:
    """Render destroy/exile/counter events with source attribution if known."""
    passive = {
        "destroy": "destroyed",
        "exile": "exiled",
        "counter": "countered",
    }
    if source:
        return f"{source} {present_tense_verb('Opponent', verb)} {target}"
    return f"{target} is {passive.get(verb, verb)}"


def phrase_concede_result(winner: str, scope_text: str) -> list[str]:
    """Render concession results as two concise narrative lines."""
    loser = "Opponent" if winner == "Me" else "Me" if winner == "Opponent" else None
    winner_line = f"Match winner: {winner}" if scope_text == "match" else f"Winner: {winner}"
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
    return [concession, winner_line]


def phrase_result(winner: str, scope_text: str, reason_text: str | None = None) -> list[str]:
    """Render non-concession results without duplicated Arena labels."""
    if reason_text == "concede":
        return phrase_concede_result(winner, scope_text)
    if scope_text == "match":
        return [f"Match winner: {winner}"]
    return [f"Winner: {winner}"]


def phrase_commander_cast_note(cast_count: int) -> str:
    """Render commander cast count and the tax that applies to the next cast."""
    return f"commander cast #{cast_count}; next commander tax +{cast_count * 2}"


def phrase_commander_damage(source: str, damage: int, target_label: str, total: int) -> str:
    """Render commander damage with the running total against a player."""
    return (
        f"Commander damage: {source} deals {damage} to "
        f"{object_pronoun(target_label)} ({total} total)"
    )


def phrase_player_counter_change(label: str, counter_name: str, amount: int, total: int) -> str:
    """Render poison, energy, or experience counter changes on a player."""
    verb = "get" if amount > 0 else "lose"
    plural = "" if abs(amount) == 1 else "s"
    return phrase_player_action(
        label,
        verb,
        f"{abs(amount)} {counter_name} counter{plural} ({total} total)",
        third_person="gets" if amount > 0 else "loses",
    )


def phrase_player_has_counter(label: str, counter_name: str, amount: int) -> str:
    """Render a persistent player counter total for turn-state summaries."""
    plural = "" if amount == 1 else "s"
    return (
        f"{subject_pronoun(label)} {present_tense_verb(label, 'have', 'has')} "
        f"{amount} {counter_name} counter{plural}"
    )


def should_emit_resolve_line(name: str, instance_id: int) -> bool:
    """Return false for anonymous stack resolves that would leak raw ids."""
    return name != f"instance {instance_id}"


def resolve_stack_name(instance_id: int, fallback_name: str, stack_names: dict[int, str]) -> str:
    """Use the original cast name when Arena renames adventure/copy objects."""
    return stack_names.get(instance_id) or fallback_name


def copied_object_label(base_name: str | None, is_copy: bool) -> str | None:
    """Make copied spells explicit so they are not confused with originals."""
    if base_name and is_copy:
        return f"A copy of {base_name}"
    return base_name


def ability_source_instance_id(obj: dict | None) -> int | None:
    """Return the source permanent/spell instance for an Arena ability object."""
    if not obj or obj.get("type") != "GameObjectType_Ability":
        return None
    return obj.get("parentId")


def ability_object_label(source_name: str | None, is_trigger: bool = True) -> str | None:
    """Name stack ability objects from their source card instead of their own grpId."""
    if not source_name:
        return None
    kind = "trigger" if is_trigger else "ability"
    return f"{source_name} {kind}"


def phrase_mill_summary(source_name: str | None, label: str | None, count: int) -> str:
    """Summarize grouped mill zone changes caused by one source."""
    source = source_name or "A source"
    source_phrase = (
        f"{source} trigger resolves"
        if count == 1
        else f"{source} triggers resolve"
    )
    player = subject_pronoun(label) if label else "a player"
    verb = present_tense_verb(label or "A player", "mill", "mills")
    plural = "" if count == 1 else "s"
    return f"{source_phrase}; {player} {verb} {count} card{plural}"


def death_label_or_none(name: str, instance_id: int) -> str | None:
    """Suppress unidentified death events instead of printing raw instance ids."""
    if name == f"instance {instance_id}":
        return None
    return name


def active_effect_for_resolved_permanent(name: str, owner: str) -> str | None:
    """Return concise active-effect text for high-impact resolved permanents."""
    if name == "Valkmira, Protector's Shield":
        return (
            "Valkmira, Protector's Shield prevents 1 damage from each "
            f"opponent source to {object_pronoun(owner)}"
        )
    if name == "Leyline of the Void":
        return "Leyline of the Void exiles opponents' cards that would go to graveyard"
    return None


def phrase_library_count(label: str, count: int | None) -> str:
    """Render a player's library size for turn-state snapshots."""
    if count is None:
        return f"{label}: unknown"
    plural = "" if count == 1 else "s"
    return f"{label}: {count} card{plural}"


def phrase_mulligan(label: str, kept_count: int | None = None) -> str:
    """Render a mulligan without assuming every format reduces hand size."""
    base = f"{subject_pronoun(label)} {present_tense_verb(label, 'mulligan', 'mulligans')}"
    if kept_count is None:
        return base
    plural = "" if kept_count == 1 else "s"
    return f"{base} (kept {kept_count} card{plural})"


def phrase_choice_value(domain_text: str, value: int, decoded_value: str | None) -> str:
    """Render decoded choices and explain unknown numeric choice values."""
    if decoded_value:
        return decoded_value
    if domain_text == "creature type":
        return f"unknown creature type {value}"
    return f"unknown {domain_text} {value}"


def phrase_incomplete_game_notice(postgame_hint: str | None = None) -> list[str]:
    """Explain that Arena returned postgame data without final GRE results."""
    lines = [
        "Game appears to have ended, but no final GRE result was written to Player.log."
    ]
    if postgame_hint:
        lines.append(postgame_hint)
    lines.append("Final life total is unavailable from the gameplay log.")
    return lines


def is_low_fidelity_update_without_turn(gsm: dict) -> bool:
    """Detect speculative Send updates that Arena may replace shortly after."""
    if gsm.get("update") != "GameStateUpdate_Send":
        return False
    turn_info = gsm.get("turnInfo") or {}
    return "turnNumber" not in turn_info and "phase" not in turn_info


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
    enum_value_names: dict[str, dict[int, str]] | None = None,
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
    seen_commander_damage = set()
    seen_no_combat_damage = set()
    remembered_object_labels = {}
    stack_display_names = {}
    ability_trigger_ids = set()
    ability_source_names = {}
    pending_stack_casts = {}
    pending_mill_group = None
    pending_death_events = []
    active_effects = {}
    commander_grps_by_seat = {}
    commander_instance_ids = set()
    commander_cast_counts = Counter()
    commander_damage = Counter()
    player_counters = Counter()
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
    enum_value_names = enum_value_names or {}
    subtype_names = enum_value_names.get("SubType") or {}
    choice_value_names = {
        # Observed in current GRE logs:
        # Serra's Emissary: domain 4, value 2 -> Creature.
        # Patchwork Banner / Vanquisher's Banner / Cavern of Souls:
        # domain 5, value 1 -> Angel.
        # Creature type values are Arena SubType enum values when the card DB
        # exposes them, so Cavern value 25 comes from SubType 25 -> Elemental.
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
        5: {1: "Angel", **subtype_names},
        6: {
            1: "White",
            2: "Blue",
            3: "Black",
            4: "Red",
            5: "Green",
            6: "Colorless",
        },
    }
    player_counter_names = {
        # These are intentionally conservative. Current sample logs did not
        # expose poison, energy, or experience counters on players, so unknown
        # numeric counter types are kept out of the transcript until observed.
        "poison": "poison",
        "energy": "energy",
        "experience": "experience",
        "CounterType_Poison": "poison",
        "CounterType_Energy": "energy",
        "CounterType_Experience": "experience",
    }
    # Observed on Skithiryx, the Blight Dragon. Arena records its combat
    # damage to players as damage with markDamage=0 instead of ModifiedLife.
    infect_ability_grp_ids = {91}

    def emit(line=""):
        if current_match_lines is not None:
            current_match_lines.append(line)

    def mark_postgame_payload(root):
        """Remember postgame account/course blobs that appear after GRE stops."""
        if current_match_record is None or current_match_record.get("has_result"):
            return
        if "InventoryInfo" in root or "UpdatedGraphs" in root:
            current_match_record["saw_postgame_payload"] = True
        courses = root.get("Courses") or []
        if courses:
            current_match_record["saw_postgame_payload"] = True
        has_loss_count = any("CurrentLosses" in course for course in courses)
        has_win_count = any("CurrentWins" in course for course in courses)
        if has_loss_count and not current_match_record.get("postgame_hint"):
            current_match_record["postgame_hint"] = (
                "Postgame course/event data includes a loss count after this match."
            )
        elif has_win_count and not current_match_record.get("postgame_hint"):
            current_match_record["postgame_hint"] = (
                "Postgame course/event data includes a win count after this match."
            )

    def finalize_current_match():
        """Append a conservative notice when a match lacks final GRE results."""
        flush_pending_event_groups()
        if current_match_record is None or current_match_record.get("finalized"):
            return
        current_match_record["finalized"] = True
        if current_match_record.get("has_result"):
            return
        if not current_match_record.get("saw_postgame_payload"):
            return
        emit("")
        for line in phrase_incomplete_game_notice(current_match_record.get("postgame_hint")):
            emit(line)

    def add_active_effect(key, text, source_id=None, until=None):
        """Record a major ongoing effect for turn-state summaries."""
        active_effects[key] = {
            "text": text,
            "source_id": source_id,
            "until": until,
        }

    def remove_active_effects_for_source(source_id):
        """Remove ongoing effects tied to a permanent that left its source zone."""
        if source_id is None:
            return
        for key, effect in list(active_effects.items()):
            if effect.get("source_id") == source_id:
                active_effects.pop(key, None)

    def remove_active_effects_with_prefix(prefix):
        """Remove grouped effects such as Teferi's temporary effects."""
        for key in list(active_effects):
            if isinstance(key, tuple) and key[: len(prefix)] == prefix:
                active_effects.pop(key, None)

    def detail_value(detail):
        """Extract the scalar/list value from Arena's typed key-value details."""
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
        """Convert Arena annotation details into a plain dict for easier parsing."""
        return {d.get("key"): detail_value(d) for d in details or []}

    def normalize_for_key(value):
        """Create a hashable annotation key component while ignoring volatile fields."""
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
        """Build a stable dedupe key for annotations with or without Arena ids."""
        ann_id = ann.get("id")
        if ann_id is not None:
            return (
                "id_payload",
                current_match,
                ann_id,
                tuple(ann.get("type") or []),
                ann.get("affectorId"),
                tuple(ann.get("affectedIds") or []),
                normalize_for_key(ann.get("details") or []),
            )

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
        """Convert an Arena seat number to the user-facing player label."""
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
        """Convert a team id to a player/team label for result reporting."""
        seats = team_to_seats.get(team_id) or []
        if len(seats) == 1:
            return owner_label(seats[0])
        if seats:
            return "Team " + "/".join(str(seat) for seat in seats)
        return f"Team {team_id}"

    def zone_owner(zone_id):
        """Return the seat that owns a zone, when Arena exposes one."""
        zone = zones.get(zone_id, {})
        return zone.get("ownerSeatId")

    def hand_count_for_seat(seat):
        """Count cards in a player's hand zone when the game state exposes it."""
        for zone in zones.values():
            if zone.get("type") == "ZoneType_Hand" and zone.get("ownerSeatId") == seat:
                return len(zone.get("objectInstanceIds") or [])
        return None

    def object_owner(instance_id):
        """Return the controlling/owning seat for an object instance."""
        obj = objects.get(instance_id, {})
        return obj.get("controllerSeatId") or obj.get("ownerSeatId")

    def card_name_from_grp(grp_id):
        """Translate an Arena grpId through the SQLite card database mapping."""
        if grp_id is None:
            return None
        try:
            grp_id = int(grp_id)
        except (TypeError, ValueError):
            return None
        return grp_to_name.get(grp_id, f"grpId {grp_id}")

    def token_label_from_object(obj):
        """Build a useful fallback label for tokens that have no card grpId."""
        if obj.get("type") != "GameObjectType_Token":
            return None
        subtypes = obj.get("subtypes") or []
        if subtypes:
            subtype = str(subtypes[0]).removeprefix("SubType_")
            return f"{subtype} token"
        return "token"

    def object_label_from_object(obj):
        """Return the best readable label available directly on a game object."""
        if obj.get("type") == "GameObjectType_Ability":
            source_name = ability_source_names.get(obj.get("instanceId"))
            source_name = source_name or card_name_from_grp(obj.get("objectSourceGrpId"))
            if not source_name and obj.get("parentId") is not None:
                source_name = source_label(obj.get("parentId"))
            if source_name:
                # Ability objects have their own grpIds, and those ids can map
                # to unrelated cards. Use the source card and the observed
                # ability-instance annotation instead.
                return ability_object_label(
                    source_name,
                    obj.get("instanceId") in ability_trigger_ids,
                )
        return (
            card_name_from_grp(obj.get("grpId"))
            or card_name_from_grp(obj.get("objectSourceGrpId"))
            or token_label_from_object(obj)
        )

    def note_object_id_change(ann):
        """Carry labels across Arena ObjectIdChanged annotations."""
        details = detail_dict(ann.get("details"))
        orig_id = details.get("orig_id")
        new_id = details.get("new_id")
        if orig_id is None or new_id is None:
            return
        if orig_id in remembered_object_labels:
            remembered_object_labels[new_id] = remembered_object_labels[orig_id]
        if orig_id in stack_display_names:
            stack_display_names[new_id] = stack_display_names[orig_id]

    def card_label(instance_id, fallback_grp_id=None):
        """Return a readable card label for an instance, falling back to grpId."""
        obj = objects.get(instance_id, {})
        return (
            object_label_from_object(obj)
            or remembered_object_labels.get(instance_id)
            or card_name_from_grp(fallback_grp_id)
            or f"instance {instance_id}"
        )

    def target_label(target_id):
        """Render a combat or spell target in object case when it is a player."""
        if target_id in (1, 2):
            return object_pronoun(owner_label(target_id))
        return card_label(target_id)

    def source_label(instance_id):
        """Return a source card name only when it is more useful than a raw id."""
        if instance_id is None:
            return None
        obj = objects.get(instance_id, {})
        if obj.get("type") == "GameObjectType_Ability":
            return object_label_from_object(obj) or remembered_object_labels.get(instance_id)
        if obj.get("isCopy"):
            base = object_label_from_object(obj) or remembered_object_labels.get(instance_id)
            return copied_object_label(base, True)
        label = card_label(instance_id)
        if label == f"instance {instance_id}" or label.startswith("grpId "):
            return None
        return label

    def object_has_ability(instance_id, ability_grp_ids):
        """Check an object's exposed unique ability grpIds for known mechanics."""
        obj = objects.get(instance_id, {})
        for ability in obj.get("uniqueAbilities") or []:
            if ability.get("grpId") in ability_grp_ids:
                return True
        return False

    def event_owner(ann, details):
        """Infer the player responsible for a zone-transfer style event."""
        src_owner = zone_owner(details.get("zone_src"))
        dst_owner = zone_owner(details.get("zone_dest"))
        affected = ann.get("affectedIds") or []
        affected_owner = object_owner(affected[0]) if affected else None
        affector_owner = object_owner(ann.get("affectorId"))
        return owner_label(src_owner or dst_owner or affected_owner or affector_owner)

    def is_command_zone(zone_id):
        """Return true when a zone id is Arena's shared command zone."""
        return zones.get(zone_id, {}).get("type") == "ZoneType_Command"

    def zone_ids_by_type(zone_type):
        """Collect current zone ids of a given Arena zone type."""
        return {
            zone_id
            for zone_id, zone in zones.items()
            if zone.get("type") == zone_type
        }

    def compact_names(names, unknown_label="unknown card"):
        """Compact repeated card names for board-state summaries."""
        counts = Counter(name or unknown_label for name in names)
        if not counts:
            return "(empty)"
        parts = []
        for name in sorted(counts):
            count = counts[name]
            parts.append(f"{count}x {name}" if count > 1 else name)
        return "; ".join(parts)

    def card_label_for_snapshot(instance_id):
        """Return a snapshot label, preserving hidden cards as unknown."""
        obj = objects.get(instance_id, {})
        if not obj.get("grpId") and not obj.get("objectSourceGrpId"):
            return "unknown card"
        return card_label(instance_id)

    def battlefield_names(seat):
        """Collect battlefield card/token names controlled by a seat."""
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
        """Collect card names from zones such as hand, graveyard, exile."""
        names = []
        for zone in zones.values():
            if zone.get("type") != zone_type:
                continue
            zone_owner_id = zone.get("ownerSeatId")
            if seat is not None and zone_owner_id is not None and zone_owner_id != seat:
                continue
            for instance_id in zone.get("objectInstanceIds") or []:
                # Command and exile are often shared zones. In those cases the
                # zone has no owner, so filter each object by controller/owner.
                if seat is not None and zone_owner_id is None and object_owner(instance_id) != seat:
                    continue
                names.append(card_label_for_snapshot(instance_id))
        return names

    def hand_names(seat):
        """Collect visible/hidden hand card labels for a seat."""
        names = []
        for zone in zones.values():
            if zone.get("type") != "ZoneType_Hand":
                continue
            if zone.get("ownerSeatId") != seat:
                continue
            for instance_id in zone.get("objectInstanceIds") or []:
                names.append(card_label_for_snapshot(instance_id))
        return names

    def library_count(seat):
        """Return the current number of objects in a player's library zone."""
        for zone in zones.values():
            if zone.get("type") == "ZoneType_Library" and zone.get("ownerSeatId") == seat:
                return len(zone.get("objectInstanceIds") or [])
        return None

    def infer_local_seat():
        """Infer the user's seat from the only hand that has visible card names."""
        candidates = []
        for seat in (1, 2):
            names = hand_names(seat)
            if names and "unknown card" not in names:
                candidates.append(seat)
        return candidates[0] if len(candidates) == 1 else None

    def emit_turn_state():
        """Print the optional turn-start board and strategic state summary."""
        if not show_turn_state:
            return
        emit("Board:")
        emit(f"  {owner_label(1)}: {compact_names(battlefield_names(1))}")
        emit(f"  {owner_label(2)}: {compact_names(battlefield_names(2))}")
        emit("Hands:")
        emit(f"  {owner_label(1)}: {compact_names(hand_names(1))}")
        emit(f"  {owner_label(2)}: {compact_names(hand_names(2))}")
        emit("Library:")
        emit(f"  {phrase_library_count(owner_label(1), library_count(1))}")
        emit(f"  {phrase_library_count(owner_label(2), library_count(2))}")
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
        state_lines = strategic_state_lines()
        if state_lines:
            emit("Current State:")
            for line in state_lines:
                emit(f"  {line}")

    def strategic_state_lines():
        """Build a concise summary of counters and commander-related state."""
        lines = []
        for (seat, counter_name), amount in sorted(player_counters.items()):
            if amount:
                lines.append(phrase_player_has_counter(owner_label(seat), counter_name, amount))

        for (seat, grp_id), count in sorted(commander_cast_counts.items()):
            if count > 1:
                tax = count * 2
                lines.append(
                    f"{possessive_pronoun(owner_label(seat))} next commander tax for "
                    f"{card_name_from_grp(grp_id)} is +{tax}"
                )

        for (source_seat, target_seat), amount in sorted(commander_damage.items()):
            if amount:
                commander_names = [
                    card_name_from_grp(grp_id)
                    for grp_id in sorted(commander_grps_by_seat.get(source_seat, set()))
                ]
                source = " / ".join(name for name in commander_names if name) or "Commander"
                lines.append(
                    f"{source} has dealt {amount} commander damage to "
                    f"{owner_label(target_seat)}"
                )
        return lines

    def flush_pending_stack_cast(instance_id):
        """Emit a delayed speculative cast once Arena confirms it mattered."""
        pending = pending_stack_casts.pop(instance_id, None)
        if pending:
            emit(phrase_player_action(pending["owner"], "cast", pending["text"]))

    def flush_pending_cast_for_affector(affector_id):
        """Emit delayed source casts before their triggered effects are narrated."""
        flush_pending_stack_cast(affector_id)
        affector = objects.get(affector_id)
        source_id = ability_source_instance_id(affector)
        if source_id is not None:
            # Arena can report a spell's cast-trigger effect before the later
            # Resolve zone transfer. If the source spell was delayed from a
            # low-fidelity CastSpell update, emit it before narrating the
            # trigger so the transcript stays chronological.
            flush_pending_stack_cast(source_id)
        if not pending_stack_casts or not affector:
            return
        source_grp = affector.get("objectSourceGrpId")
        if source_grp is None:
            return
        controller = affector.get("controllerSeatId")
        for pending_id in list(pending_stack_casts):
            pending_obj = objects.get(pending_id, {})
            if pending_obj.get("grpId") != source_grp:
                continue
            if controller is not None and pending_obj.get("controllerSeatId") != controller:
                continue
            # Some cast-trigger ability objects keep source grpId but their
            # parent linkage is not reliable across all Send/SendHiFi diffs.
            # Matching the pending stack spell by source grpId/controller keeps
            # the cast line ahead of the trigger without hard-coding a card.
            flush_pending_stack_cast(pending_id)

    def mill_source_from_affector(affector_id):
        """Return the source card for a milling ability, when Arena exposes it."""
        if affector_id in ability_source_names:
            return ability_source_names[affector_id]
        obj = objects.get(affector_id, {})
        if obj.get("type") != "GameObjectType_Ability":
            return None
        source_name = card_name_from_grp(obj.get("objectSourceGrpId"))
        if not source_name and obj.get("parentId") is not None:
            source_name = source_label(obj.get("parentId"))
        return source_name

    def flush_pending_mill_group():
        """Emit a compact line for consecutive mill events from one source."""
        nonlocal pending_mill_group
        if not pending_mill_group:
            return
        emit(
            phrase_mill_summary(
                pending_mill_group["source"],
                pending_mill_group["owner"],
                pending_mill_group["count"],
            )
        )
        pending_mill_group = None

    def flush_pending_death_group():
        """Emit a compact line for identical deaths in one contiguous death burst."""
        nonlocal pending_death_events
        if not pending_death_events:
            return
        grouped = Counter((event["controller"], event["name"]) for event in pending_death_events)
        emitted = set()
        for event in pending_death_events:
            key = (event["controller"], event["name"])
            if key in emitted:
                continue
            emitted.add(key)
            emit(phrase_grouped_deaths(event["controller"], event["name"], grouped[key]))
        pending_death_events = []

    def flush_pending_event_groups():
        """Flush grouped event summaries before emitting an unrelated event."""
        flush_pending_mill_group()
        flush_pending_death_group()

    def add_pending_mill(source, owner, count=1):
        """Group repeated mill zone transfers by source and milled player."""
        nonlocal pending_mill_group
        if not source:
            return False
        if (
            pending_mill_group
            and pending_mill_group["source"] == source
            and pending_mill_group["owner"] == owner
        ):
            pending_mill_group["count"] += count
        else:
            flush_pending_mill_group()
            pending_mill_group = {"source": source, "owner": owner, "count": count}
        return True

    def add_pending_death(controller, name):
        """Hold deaths briefly so one destroy/lethal batch can be summarized."""
        pending_death_events.append({"controller": controller, "name": name})

    def emit_zone_transfer(ann, gsm):
        """Emit cast/play/zone-change lines, including command-zone casts."""
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
            flush_pending_event_groups()
            emit(phrase_player_action(owner, "play", name))
        elif category == "CastSpell":
            flush_pending_event_groups()
            pending_stack_casts.pop(iid, None)
            stack_display_names[iid] = name
            suffix = " from command zone" if from_command else ""
            if from_command:
                commander_text = note_commander_cast(iid, details.get("grpid"))
                if commander_text:
                    suffix = f"{suffix}; {commander_text}"
            cast_text = f"{name}{suffix}"
            if is_low_fidelity_update_without_turn(gsm):
                pending_stack_casts[iid] = {"owner": owner, "text": cast_text}
            else:
                emit(phrase_player_action(owner, "cast", cast_text))
        elif category == "Resolve":
            flush_pending_event_groups()
            name = resolve_stack_name(iid, name, stack_display_names)
            flush_pending_stack_cast(iid)
            effect_text = active_effect_for_resolved_permanent(name, owner)
            if effect_text:
                add_active_effect(("resolved_effect", iid, name), effect_text, source_id=iid)
            if show_resolves and should_emit_resolve_line(name, iid):
                # Anonymous stack objects are usually triggered/copy ability
                # bookkeeping. Emitting raw ids is less useful than silence.
                emit(f"{name} resolves")
        elif category == "Copy":
            flush_pending_event_groups()
            # Jin-Gitaxias and similar effects put a copy on the stack. Track
            # that identity so later effects do not look like the original
            # countered spell still resolved.
            copy_name = copied_object_label(name, True)
            if copy_name:
                stack_display_names[iid] = copy_name
                remembered_object_labels[iid] = copy_name
        elif category in death_categories:
            flush_pending_mill_group()
            flush_pending_cast_for_affector(ann.get("affectorId"))
            controller = object_owner(iid)
            name = death_label_or_none(name, iid)
            if not name:
                return
            add_pending_death(owner_label(controller) if controller else None, name)
            remove_active_effects_for_source(iid)
        elif category in {"Destroy", "DestroyNoRegenerate"}:
            flush_pending_event_groups()
            flush_pending_cast_for_affector(ann.get("affectorId"))
            source = source_label(ann.get("affectorId"))
            if source and ann.get("affectorId") != iid and source != name:
                emit(phrase_zone_change(source, "destroy", name))
            else:
                emit(phrase_zone_change(None, "destroy", name))
            remove_active_effects_for_source(iid)
        elif category == "Exile":
            flush_pending_event_groups()
            flush_pending_cast_for_affector(ann.get("affectorId"))
            source = source_label(ann.get("affectorId"))
            if source and ann.get("affectorId") != iid and source != name:
                emit(phrase_zone_change(source, "exile", name))
            else:
                emit(phrase_zone_change(None, "exile", name))
            remove_active_effects_for_source(iid)
        elif category == "Countered":
            flush_pending_event_groups()
            flush_pending_stack_cast(iid)
            emit(phrase_zone_change(None, "counter", name))
            remove_active_effects_for_source(iid)
        elif category == "Discard":
            flush_pending_event_groups()
            emit(phrase_player_action(owner, "discard", name))
        elif category == "Sacrifice":
            flush_pending_event_groups()
            emit(phrase_player_action(owner, "sacrifice", name))
            remove_active_effects_for_source(iid)
        elif category == "Mill":
            source = mill_source_from_affector(ann.get("affectorId"))
            mill_owner = owner_label(zone_owner(details.get("zone_src")) or object_owner(iid))
            if not add_pending_mill(source, mill_owner):
                flush_pending_event_groups()

    def emit_life_change(ann, gsm):
        """Emit life gain/loss lines from ModifiedLife annotations."""
        flush_pending_event_groups()
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
        """Emit readable choice-result lines from Arena's numeric choice annotations."""
        flush_pending_event_groups()
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
        value_text = phrase_choice_value(
            domain_text,
            value,
            choice_value_names.get(domain, {}).get(value),
        )

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

    def note_commander_cast(instance_id, fallback_grp_id=None):
        """Track command-zone casts as commander casts and report current tax."""
        obj = objects.get(instance_id, {})
        seat = obj.get("controllerSeatId") or obj.get("ownerSeatId") or object_owner(instance_id)
        grp_id = obj.get("grpId") or obj.get("objectSourceGrpId") or fallback_grp_id
        if seat is None or grp_id is None:
            return None
        try:
            grp_id = int(grp_id)
        except (TypeError, ValueError):
            return None

        commander_grps_by_seat.setdefault(seat, set()).add(grp_id)
        commander_instance_ids.add(instance_id)
        commander_cast_counts[(seat, grp_id)] += 1
        count = commander_cast_counts[(seat, grp_id)]
        return phrase_commander_cast_note(count)

    def emit_combat_events(gsm):
        """Emit attack/block declarations from combat-state object fields."""
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
                    flush_pending_event_groups()
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
                        flush_pending_event_groups()
                        emit(f"{card_label(iid)} blocks {card_label(attacker_id)}")

    def emit_continuous_effect_events(gsm):
        """Emit major state-changing continuous effects Arena exposes directly."""
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
                    flush_pending_event_groups()
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
                        flush_pending_event_groups()
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
            flush_pending_event_groups()
            plural = "permanent" if count == 1 else "permanents"
            emit(f"{possessive_pronoun(label)} {count} phased-out {plural} phase in")
            remove_active_effects_with_prefix(("teferi_protection", seat))
            remove_active_effects_with_prefix(("teferi_life_total", seat))

    def emit_commander_damage(ann, gsm):
        """Track combat damage from commanders to players when Arena exposes it."""
        details = detail_dict(ann.get("details"))
        damage = details.get("damage")
        affected = ann.get("affectedIds") or []
        source_id = ann.get("affectorId")
        if (
            not isinstance(damage, int)
            or damage <= 0
            or not affected
            or affected[0] not in (1, 2)
        ):
            return
        if gsm.get("turnInfo", {}).get("phase") != "Phase_Combat":
            return

        source = objects.get(source_id, {})
        source_seat = source.get("controllerSeatId") or source.get("ownerSeatId")
        source_grp = source.get("grpId") or source.get("objectSourceGrpId")
        try:
            source_grp = int(source_grp) if source_grp is not None else None
        except (TypeError, ValueError):
            source_grp = None
        is_commander = (
            source_id in commander_instance_ids
            or source_grp in commander_grps_by_seat.get(source_seat, set())
        )
        if not is_commander or source_seat is None:
            return

        target_seat = affected[0]
        key = (current_match, gsm.get("gameStateId"), ann.get("id"), source_id, target_seat)
        if key in seen_commander_damage:
            return
        seen_commander_damage.add(key)
        commander_damage[(source_seat, target_seat)] += damage
        total = commander_damage[(source_seat, target_seat)]
        flush_pending_event_groups()
        emit(phrase_commander_damage(card_label(source_id), damage, owner_label(target_seat), total))

    def emit_no_combat_damage(ann, gsm):
        """Report zero combat damage to players without guessing the prevention source."""
        details = detail_dict(ann.get("details"))
        if details.get("damage") != 0:
            return
        affected = ann.get("affectedIds") or []
        if not affected or affected[0] not in (1, 2):
            return
        if gsm.get("turnInfo", {}).get("phase") != "Phase_Combat":
            return

        source_id = ann.get("affectorId")
        source = source_label(source_id)
        if not source:
            return

        target_seat = affected[0]
        key = (current_match, gsm.get("gameStateId"), ann.get("id"), source_id, target_seat)
        if key in seen_no_combat_damage:
            return
        seen_no_combat_damage.add(key)
        flush_pending_event_groups()
        emit(f"{source} deals no combat damage to {object_pronoun(owner_label(target_seat))}")

    def emit_infect_damage(ann, gsm):
        """Infer poison counters from observed infect combat damage records."""
        details = detail_dict(ann.get("details"))
        damage = details.get("damage")
        if not isinstance(damage, int) or damage <= 0 or details.get("markDamage") != 0:
            return
        affected = ann.get("affectedIds") or []
        if not affected or affected[0] not in (1, 2):
            return
        if gsm.get("turnInfo", {}).get("phase") != "Phase_Combat":
            return

        source_id = ann.get("affectorId")
        if not object_has_ability(source_id, infect_ability_grp_ids):
            return

        seat = affected[0]
        player_counters[(seat, "poison")] += damage
        total = player_counters[(seat, "poison")]
        source = source_label(source_id) or "infect"
        flush_pending_event_groups()
        emit(
            f"{subject_pronoun(owner_label(seat))} "
            f"{present_tense_verb(owner_label(seat), 'get', 'gets')} "
            f"{damage} poison counter{'' if damage == 1 else 's'} "
            f"from {source} ({total} total)"
        )

    def emit_player_counter_change(ann, gsm):
        """Track player counters if future Arena logs expose known counter types."""
        details = detail_dict(ann.get("details"))
        affected = ann.get("affectedIds") or []
        if not affected or affected[0] not in (1, 2):
            return

        raw_type = details.get("counter_type")
        counter_name = player_counter_names.get(raw_type)
        if not counter_name:
            return

        amount = details.get("transaction_amount")
        if not isinstance(amount, int):
            return
        if "AnnotationType_CounterRemoved" in set(ann.get("type") or []):
            amount = -amount

        seat = affected[0]
        player_counters[(seat, counter_name)] += amount
        total = player_counters[(seat, counter_name)]
        flush_pending_event_groups()
        emit(phrase_player_counter_change(owner_label(seat), counter_name, amount, total))

    def update_state(gsm):
        """Merge a full/diff game-state message into the local state model."""
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
                if obj.get("type") == "GameObjectType_Ability":
                    source_name = card_name_from_grp(obj.get("objectSourceGrpId"))
                    if not source_name and obj.get("parentId") is not None:
                        source_name = source_label(obj.get("parentId"))
                    if source_name:
                        # Ability instances may be deleted in the same update
                        # that reports their zone-change effects. Store the
                        # source before deletion so later annotations still
                        # have a readable source.
                        ability_source_names[obj["instanceId"]] = source_name
                label = object_label_from_object(obj)
                if label:
                    remembered_object_labels[obj["instanceId"]] = label

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
                    label = owner_label(seat)
                    kept_count = hand_count_for_seat(seat) if label == "Me" else None
                    emit(phrase_mulligan(label, kept_count))

        for deleted_id in gsm.get("diffDeletedInstanceIds") or []:
            objects.pop(deleted_id, None)

    def emit_match_results(gsm):
        """Emit game/match result lines once per unique Arena result record."""
        for result in gsm.get("gameInfo", {}).get("results") or []:
            scope = result.get("scope")
            winning_team_id = result.get("winningTeamId")
            reason = result.get("reason")
            result_type = result.get("result")
            key = (current_match, scope, winning_team_id, reason, result_type)
            if key in seen_results:
                continue
            seen_results.add(key)
            if current_match_record is not None:
                current_match_record["has_result"] = True

            winner = team_label(winning_team_id)
            scope_text = (scope or "Result").replace("MatchScope_", "").lower()
            reason_text = (reason or result_type or "unknown").replace("ResultReason_", "")
            reason_text = reason_text.replace("ResultType_", "").lower()
            flush_pending_event_groups()
            emit("")
            for line in phrase_result(winner, scope_text, reason_text):
                emit(line)

    def record_debug(ann):
        """Record annotation counts and one sample payload for debug output."""
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
        """Render a short one-line JSON sample for debug summaries."""
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return text if len(text) <= 220 else text[:217] + "..."

    def contains_choice_marker(value, path=()):
        """Detect likely choice payloads without treating card type lines as choices."""
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
        """Return a requested debug grpId if this object matches one."""
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
                    finalize_current_match()
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
                        "has_result": False,
                        "saw_postgame_payload": False,
                        "postgame_hint": None,
                        "finalized": False,
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
                    seen_commander_damage.clear()
                    seen_no_combat_damage.clear()
                    remembered_object_labels.clear()
                    stack_display_names.clear()
                    ability_trigger_ids.clear()
                    ability_source_names.clear()
                    pending_stack_casts.clear()
                    pending_mill_group = None
                    pending_death_events.clear()
                    active_effects.clear()
                    commander_grps_by_seat.clear()
                    commander_instance_ids.clear()
                    commander_cast_counts.clear()
                    commander_damage.clear()
                    player_counters.clear()
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

                turn_info = gsm.get("turnInfo", {})
                if "turnNumber" in turn_info and turn_info["turnNumber"] != current_turn:
                    flush_pending_event_groups()
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
                    if "AnnotationType_ObjectIdChanged" in ann_types:
                        note_object_id_change(ann)
                    elif "AnnotationType_AbilityInstanceCreated" in ann_types:
                        # Arena can create a triggered ability before the source
                        # spell's later Resolve annotation. Emit any delayed
                        # source cast here so the ability does not appear first.
                        flush_pending_cast_for_affector(ann.get("affectorId"))
                        for affected_id in ann.get("affectedIds") or []:
                            ability_trigger_ids.add(affected_id)
                            obj = objects.get(affected_id)
                            if obj:
                                flush_pending_cast_for_affector(affected_id)
                                label = object_label_from_object(obj)
                                if label:
                                    remembered_object_labels[affected_id] = label
                                    stack_display_names[affected_id] = label
                    elif "AnnotationType_ZoneTransfer" in ann_types:
                        emit_zone_transfer(ann, gsm)
                    elif "AnnotationType_ModifiedLife" in ann_types:
                        emit_life_change(ann, gsm)
                    elif "AnnotationType_ChoiceResult" in ann_types:
                        emit_choice_result(ann, gsm)
                    elif "AnnotationType_DamageDealt" in ann_types:
                        emit_commander_damage(ann, gsm)
                        emit_infect_damage(ann, gsm)
                        emit_no_combat_damage(ann, gsm)
                    elif (
                        "AnnotationType_CounterAdded" in ann_types
                        or "AnnotationType_CounterRemoved" in ann_types
                    ):
                        emit_player_counter_change(ann, gsm)

                emit_match_results(gsm)

            if not root.get("greToClientEvent"):
                mark_postgame_payload(root)

    finalize_current_match()

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
        description=(
            "Extract readable MTG Arena game transcripts from Player.log. "
            "The card database argument should point to Arena's local "
            "Raw_CardDatabase_*.mtga SQLite file."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  Print the most recent game:
    python3 mtga_extract_games.py "$LOG" "$CARDDB" --last 1 --no-resolves

  Save the last three games to a text file:
    python3 mtga_extract_games.py "$LOG" "$CARDDB" --last 3 --no-resolves > mtga_transcript.txt

  Print only game 4 from the log:
    python3 mtga_extract_games.py "$LOG" "$CARDDB" --select 4 --no-resolves

  Debug where Arena records a card's choices:
    python3 mtga_extract_games.py "$LOG" "$CARDDB" --last 1 --debug-card "Serra's Emissary"

macOS path examples:
  LOG="$HOME/Library/Logs/Wizards Of The Coast/MTGA/Player.log"
  CARDDB="$HOME/Library/Application Support/com.wizards.mtga/Downloads/Raw/Raw_CardDatabase_....mtga"

No pip install step is required; this script only uses Python's standard library.
""",
    )
    parser.add_argument(
        "player_log",
        type=Path,
        help="path to MTG Arena's Player.log file",
    )
    parser.add_argument(
        "carddb",
        type=Path,
        help="path to Raw_CardDatabase_*.mtga from Arena's Downloads/Raw folder",
    )
    parser.add_argument(
        "--debug-annotations",
        action="store_true",
        help="advanced: print annotation type/category counts and example payloads to stderr",
    )
    parser.add_argument(
        "--debug-grpid",
        type=int,
        action="append",
        default=[],
        metavar="N",
        help="advanced: dump raw gameplay event windows around objects with this GrpId",
    )
    parser.add_argument(
        "--debug-card",
        action="append",
        default=[],
        metavar="NAME",
        help="advanced: look up a card name in the SQLite card DB and debug its GrpId(s)",
    )
    parser.add_argument(
        "--debug-choices",
        action="store_true",
        help="advanced: print game events containing likely choice or selection fields",
    )
    selection_group = parser.add_mutually_exclusive_group()
    selection_group.add_argument(
        "--select",
        type=int,
        metavar="N",
        help="output only game N from the log, counting from the start of the file",
    )
    selection_group.add_argument(
        "--last",
        type=int,
        metavar="N",
        help="output only the last N games; use --last 1 for the most recent game",
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
        help="hide 'resolves' transcript lines for shorter output",
    )
    parser.add_argument(
        "--no-turn-state",
        action="store_true",
        help="hide board, hand, graveyard, exile, and commander snapshots at turn starts",
    )
    args = parser.parse_args()

    player_log = args.player_log.expanduser()
    carddb = args.carddb.expanduser()

    if args.select is not None and args.select < 1:
        parser.error("--select must be 1 or greater")
    if args.last is not None and args.last < 1:
        parser.error("--last must be 1 or greater")

    grp_to_name = load_grp_id_to_name(carddb)
    enum_value_names = {
        # Player choice payloads for creature types use the same numeric values
        # as the card database SubType enum, which keeps this from becoming a
        # handwritten creature-type list.
        "SubType": load_enum_value_names(carddb, "SubType"),
    }
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
        enum_value_names=enum_value_names,
    )


if __name__ == "__main__":
    main()
