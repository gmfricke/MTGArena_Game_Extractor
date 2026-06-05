#!/usr/bin/env python3
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path


DEATH_CATEGORIES = {
    "SBA_Damage", "SBA_Deathtouch", "SBA_ZeroLoyalty",
    "SBA_ZeroToughness", "SBA_UnattachedAura",
}

CHOICE_DOMAIN_NAMES = {
    4: "card type", 5: "creature type", 6: "color",
    11: "permanent type", 14: "mana value parity",
}

PLAYER_COUNTER_NAMES = {
    "poison": "poison", "energy": "energy", "experience": "experience",
    "CounterType_Poison": "poison",
    "CounterType_Energy": "energy",
    "CounterType_Experience": "experience",
}

BASE_CHOICE_VALUE_NAMES = {
    4: {
        1: "Artifact", 2: "Creature", 3: "Enchantment", 4: "Instant",
        5: "Land", 6: "Planeswalker", 7: "Sorcery", 8: "Battle",
    },
    6: {1: "White", 2: "Blue", 3: "Black", 4: "Red", 5: "Green", 6: "Colorless"},
    11: {1: "creature"},
    14: {0: "even", 1: "odd"},
}

# Observed on Skithiryx, the Blight Dragon. Arena records its combat damage to
# players as damage with markDamage=0 instead of ModifiedLife.
INFECT_ABILITY_GRP_IDS = {91}


def clear_all(*containers):
    for container in containers:
        container.clear()


def build_choice_value_names(subtype_names: dict[int, str]) -> dict[int, dict[int, str]]:
    values = {domain: dict(names) for domain, names in BASE_CHOICE_VALUE_NAMES.items()}
    values[5] = {1: "Angel", **subtype_names}
    return values


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


def parse_carddb_int_list(value: str | None) -> list[int]:
    """Parse comma/colon encoded integer lists from Arena card database fields."""
    if not value:
        return []
    ids = []
    for part in re.split(r"[,;]", str(value)):
        part = part.strip()
        if not part:
            continue
        # AbilityIds are stored as "abilityId:numericAid"; the first number is
        # the Abilities.Id value that joins to localized rules text.
        part = part.split(":", 1)[0]
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids


def resource_mechanics_from_text(texts: list[str]) -> list[str]:
    """Detect explicit self-play mechanics from localized ability text."""
    found = []
    haystack = "\n".join(texts).casefold()
    for mechanic in ("escape", "flashback", "disturb", "aftermath", "adventure"):
        if mechanic in haystack:
            found.append(mechanic)
    return found


def resource_mechanics_for_zone(mechanics: list[str], zone_name: str) -> list[str]:
    """Keep only mechanics that plausibly allow play from a specific zone."""
    allowed_by_zone = {
        "graveyard": {"escape", "flashback", "disturb", "aftermath"},
        "exile": {"adventure"},
    }
    allowed = allowed_by_zone.get(zone_name, set())
    return [mechanic for mechanic in mechanics if mechanic in allowed]


def load_grp_id_to_metadata(carddb_path: Path) -> dict[int, dict]:
    """Load small card metadata needed for conservative resource summaries."""
    con = sqlite3.connect(carddb_path)
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    if not {"Cards", "Localizations_enUS"}.issubset(tables):
        con.close()
        return {}
    cur.execute("PRAGMA table_info(Cards)")
    card_columns = {row[1] for row in cur.fetchall()}
    colors_expr = "c.Colors" if "Colors" in card_columns else "''"
    color_identity_expr = "c.ColorIdentity" if "ColorIdentity" in card_columns else "''"
    frame_colors_expr = "c.FrameColors" if "FrameColors" in card_columns else "''"

    cur.execute(f"""
        SELECT
            c.GrpId,
            l.Loc,
            l.Formatted,
            c.Types,
            c.AbilityIds,
            {colors_expr},
            {color_identity_expr},
            {frame_colors_expr}
        FROM Cards c
        JOIN Localizations_enUS l
          ON c.TitleId = l.LocId
        WHERE l.Formatted IN (0, 1)
        ORDER BY c.GrpId, l.Formatted
    """)
    metadata = {}
    ability_ids_by_grp = {}
    for (
        grp_id,
        name,
        _formatted,
        type_text,
        ability_text,
        colors_text,
        color_identity_text,
        frame_colors_text,
    ) in cur.fetchall():
        grp_id = int(grp_id)
        if grp_id not in metadata:
            ability_ids = parse_carddb_int_list(ability_text)
            metadata[grp_id] = {
                "name": name,
                "type_numbers": set(parse_carddb_int_list(type_text)),
                "colors": set(parse_carddb_int_list(colors_text)),
                "color_identity": set(parse_carddb_int_list(color_identity_text)),
                "frame_colors": set(parse_carddb_int_list(frame_colors_text)),
                "ability_texts": [],
                "play_mechanics": [],
            }
            ability_ids_by_grp[grp_id] = ability_ids

    if "Abilities" in tables and ability_ids_by_grp:
        ability_ids = sorted({aid for aids in ability_ids_by_grp.values() for aid in aids})
        ability_texts = {}
        for start in range(0, len(ability_ids), 900):
            chunk = ability_ids[start : start + 900]
            placeholders = ",".join("?" for _ in chunk)
            cur.execute(
                f"""
                SELECT a.Id, l.Loc, l.Formatted
                FROM Abilities a
                JOIN Localizations_enUS l
                  ON a.TextId = l.LocId
                WHERE a.Id IN ({placeholders})
                  AND l.Formatted IN (0, 1)
                ORDER BY a.Id, l.Formatted
                """,
                chunk,
            )
            for ability_id, text, _formatted in cur.fetchall():
                if text and int(ability_id) not in ability_texts:
                    ability_texts[int(ability_id)] = text

        for grp_id, aids in ability_ids_by_grp.items():
            texts = [ability_texts[aid] for aid in aids if aid in ability_texts]
            metadata[grp_id]["ability_texts"] = texts
            metadata[grp_id]["play_mechanics"] = resource_mechanics_from_text(texts)

    con.close()
    return metadata


def load_ability_texts(carddb_path: Path) -> dict[int, str]:
    """Load Arena ability id -> English rules text from the card database."""
    con = sqlite3.connect(carddb_path)
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    if not {"Abilities", "Localizations_enUS"}.issubset(tables):
        con.close()
        return {}

    cur.execute("""
        SELECT a.Id, l.Loc, l.Formatted
        FROM Abilities a
        JOIN Localizations_enUS l
          ON a.TextId = l.LocId
        WHERE l.Formatted IN (0, 1, 2)
        ORDER BY
          a.Id,
          CASE l.Formatted
            WHEN 1 THEN 0
            WHEN 0 THEN 1
            WHEN 2 THEN 2
            ELSE 3
          END
    """)
    mapping = {}
    for ability_id, text, _formatted in cur.fetchall():
        if text and int(ability_id) not in mapping:
            mapping[int(ability_id)] = clean_localized_enum_name(text)
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


def first_existing_path(candidates: list[Path]) -> Path | None:
    """Return the first candidate path that exists on this machine."""
    for candidate in candidates:
        expanded = candidate.expanduser()
        if expanded.exists():
            return expanded
    return None


def newest_existing_path(candidates: list[Path]) -> Path | None:
    """Return the newest existing path from a candidate list."""
    existing = [candidate.expanduser() for candidate in candidates if candidate.expanduser().exists()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


def env_path(names: list[str]) -> Path | None:
    """Read the first non-empty environment variable from a list of names."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser()
    return None


def likely_player_log_path() -> Path | None:
    """Find MTG Arena's Player.log in common local locations."""
    return first_existing_path(
        [
            Path("~/Library/Logs/Wizards Of The Coast/MTGA/Player.log"),
            Path("~/Library/Application Support/com.wizards.mtga/Logs/Player.log"),
        ]
    )


def arena_archive_log_paths(player_log: Path) -> list[Path]:
    """Find archived macOS Arena GRE logs that precede the current Player logs."""
    mac_log_path = Path("~/Library/Logs/Wizards Of The Coast/MTGA/Player.log").expanduser()
    try:
        if player_log.expanduser().resolve() != mac_log_path.resolve():
            return []
    except FileNotFoundError:
        return []

    archive_dir = Path("~/Library/Application Support/com.wizards.mtga/Logs/Logs").expanduser()
    return sorted(archive_dir.glob("UTC_Log - *.log"), key=lambda path: path.stat().st_mtime)


def player_log_paths_for_reading(player_log: Path, live: bool = False) -> list[Path]:
    """Return log files to parse, including Arena's rotated/archive logs."""
    if live:
        # Live mode tails the current file, so including historical logs would
        # print old games before the current one and would not follow new writes.
        return [player_log]

    previous_log = player_log.with_name("Player-prev.log")
    paths = []
    if player_log.name == "Player.log":
        paths.extend(arena_archive_log_paths(player_log))
        if previous_log.exists():
            paths.append(previous_log)
    paths.append(player_log)

    unique_paths = []
    seen_paths = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        unique_paths.append(path)
    return unique_paths


def likely_carddb_path() -> Path | None:
    """Find the newest local Arena raw card database in common locations."""
    candidates = []
    raw_dirs = [
        Path("~/Library/Application Support/com.wizards.mtga/Downloads/Raw"),
    ]
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        raw_dirs.append(Path(local_appdata) / "Packages" / "WizardsOfTheCoast.MTGA_8wekyb3d8bbwe" / "LocalCache" / "Local" / "Temp" / "Wizards Of The Coast" / "MTGA" / "Downloads" / "Raw")
    for raw_dir in raw_dirs:
        candidates.extend(raw_dir.expanduser().glob("Raw_CardDatabase_*.mtga"))
    return newest_existing_path(candidates)


def path_setup_instructions(missing: list[str]) -> str:
    """Build a user-facing setup message when paths cannot be discovered."""
    missing_text = " and ".join(missing)
    return f"""Could not find {missing_text}.

You can pass paths directly:
  python3 mtga_extract_games.py "/path/to/Player.log" "/path/to/Raw_CardDatabase_....mtga" --last 1

Or set environment variables:
  export LOG="$HOME/Library/Logs/Wizards Of The Coast/MTGA/Player.log"
  export CARDDB="$HOME/Library/Application Support/com.wizards.mtga/Downloads/Raw/Raw_CardDatabase_....mtga"

On macOS, Player.log is usually here:
  ~/Library/Logs/Wizards Of The Coast/MTGA/Player.log

The card database is usually the newest Raw_CardDatabase_*.mtga file here:
  ~/Library/Application Support/com.wizards.mtga/Downloads/Raw/
"""


def resolve_input_paths(
    player_log_arg: Path | None,
    carddb_arg: Path | None,
    *,
    live: bool = False,
) -> tuple[Path, Path, str | None]:
    """Resolve CLI/env/autodiscovered paths and return an optional warning."""
    player_log = (
        player_log_arg.expanduser()
        if player_log_arg
        else env_path(["LOG", "PLAYER_LOG", "MTGA_PLAYER_LOG"]) or likely_player_log_path()
    )
    carddb = (
        carddb_arg.expanduser()
        if carddb_arg
        else env_path(["CARDDB", "MTGA_CARDDB", "MTGA_CARD_DATABASE"]) or likely_carddb_path()
    )

    missing = []
    if player_log is None or not player_log.exists():
        missing.append("Player.log")
    if carddb is None or not carddb.exists():
        missing.append("Raw_CardDatabase_*.mtga")
    if missing:
        raise FileNotFoundError(path_setup_instructions(missing))

    warning = None
    if player_log_arg is None or carddb_arg is None:
        all_log_paths = player_log_paths_for_reading(player_log, live)
        log_lines = ["Using logs:"]
        log_lines.extend(f"  {log_path}" for log_path in all_log_paths)
        log_lines.append(f"Using card database: {carddb}")
        warning = "\n".join(log_lines)
    return player_log, carddb, warning


def find_last_game_start(player_log: Path) -> tuple[int, int]:
    """Return the byte offset and game number for the last match in Player.log."""
    last_start = 0
    game_count = 0
    current_match_id = None
    with player_log.open("rb") as f:
        while True:
            offset = f.tell()
            raw_line = f.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="ignore").strip()
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
                if match_id and match_id != current_match_id:
                    current_match_id = match_id
                    game_count += 1
                    last_start = offset
    return last_start, game_count


def subject_pronoun(label: str) -> str:
    """Return the narrative subject form for a player label."""
    return "I" if label == "Me" else label


def object_pronoun(label: str) -> str:
    """Return the narrative object form for a player label."""
    return "me" if label == "Me" else label


def state_player_label(label: str) -> str:
    """Return the compact player label used in board-state summaries."""
    return "I" if label == "Me" else label


def state_zone_label(label: str, zone_name: str) -> str:
    """Return a grammatical owner label for a turn-state zone line."""
    owner = possessive_pronoun(label)
    return f"{owner} {zone_name}" if owner else zone_name


def state_player_heading(label: str) -> str:
    """Return a turn-state block heading when grouping zones by player."""
    if label == "Me":
        return "My board"
    if label == "Opponent":
        return "Opponent's board"
    if label:
        return f"{label}'s board"
    return label


def select_transcript_matches(
    matches: list[dict],
    *,
    nth_from_start: int | None = None,
    last_games: int | None = None,
    first_games: int | None = None,
    nth_from_end: int | None = None,
    game_range: tuple[int, int] | None = None,
) -> tuple[list[dict], str | None]:
    """Apply CLI game selection rules and return selected matches plus any warning."""
    if nth_from_start is not None:
        selected = [match for match in matches if match["number"] == nth_from_start]
        if not selected:
            return [], f"No game {nth_from_start}; found {len(matches)} game(s)."
        return selected, None
    if nth_from_end is not None:
        # --nth-from-end counts backward from the end: 1 is the latest game,
        # 2 is the next-to-last game. It selects one game, unlike --last N.
        if nth_from_end > len(matches):
            return [], f"No nth-from-end game {nth_from_end}; found {len(matches)} game(s)."
        return [matches[-nth_from_end]], None
    if game_range is not None:
        start, end = game_range
        selected = [match for match in matches if start <= match["number"] <= end]
        if not selected:
            return [], f"No games in range {start}-{end}; found {len(matches)} game(s)."
        return selected, None
    if last_games is not None:
        return matches[-last_games:], None
    if first_games is not None:
        return matches[:first_games], None
    return matches, None


def has_live_selection_conflict(args) -> bool:
    """Return true when --live is combined with a game selector."""
    return bool(
        args.all
        or args.select is not None
        or args.nth_from_end is not None
        or args.first is not None
        or args.last is not None
        or args.range is not None
    )


def clean_arena_enum(value: str | None, prefix: str) -> str | None:
    """Convert Arena enum strings such as GameVariant_Brawl to Brawl."""
    if not value:
        return None
    return str(value).removeprefix(prefix).replace("_", " ")


def format_game_type(game_info: dict | None, players: list[dict] | None = None) -> str | None:
    """Return a concise game-type line from Arena gameInfo fields."""
    if not game_info:
        return None

    variant = clean_arena_enum(game_info.get("variant"), "GameVariant_")
    super_format = clean_arena_enum(game_info.get("superFormat"), "SuperFormat_")
    game_type = clean_arena_enum(game_info.get("type"), "GameType_")

    if variant == "Normal":
        label = f"{super_format} {game_type}".strip()
    else:
        label = " ".join(part for part in (super_format, variant) if part)
    if not label:
        label = game_type
    if not label:
        return None

    starting_life_totals = sorted(
        {
            player.get("startingLifeTotal")
            for player in players or []
            if player.get("startingLifeTotal") is not None
        }
    )
    if len(starting_life_totals) == 1:
        label = f"{label} ({starting_life_totals[0]} starting life)"
    return f"Game type: {label}"


def game_has_commanders(game_info: dict | None) -> bool:
    """Return true when Arena reports commander deck constraints or variants."""
    if not game_info:
        return False
    deck_constraints = game_info.get("deckConstraintInfo") or {}
    if deck_constraints.get("minCommanderSize") or deck_constraints.get("maxCommanderSize"):
        return True
    variant = clean_arena_enum(game_info.get("variant"), "GameVariant_") or ""
    return "Brawl" in variant or "Commander" in variant


ANSI_RESET = "\033[0m"
WHITE_MANA_COLOR = "\033[1;37m"
BLUE_MANA_COLOR = "\033[1;34m"
BLACK_MANA_COLOR = "\033[90m"
RED_MANA_COLOR = "\033[1;31m"
GREEN_MANA_COLOR = "\033[1;32m"
COLOURLESS_MANA_COLOR = "\033[37m"
MANA_COLORS = {
    1: WHITE_MANA_COLOR,
    2: BLUE_MANA_COLOR,
    3: BLACK_MANA_COLOR,
    4: RED_MANA_COLOR,
    5: GREEN_MANA_COLOR,
}
MULTICOLOUR_MANA_COLORS = {
    (1, 2): "\033[1;36m",
    (1, 3): "\033[1;90m",
    (1, 4): "\033[1;35m",
    (1, 5): "\033[1;32m",
    (2, 3): "\033[35m",
    (2, 4): "\033[35m",
    (2, 5): "\033[36m",
    (3, 4): "\033[31m",
    (3, 5): "\033[32m",
    (4, 5): "\033[33m",
}
TRANSCRIPT_COLORS = {
    "me": "\033[36m",
    "opponent": "\033[35m",
    "me_header": "\033[1;36m",
    "opponent_header": "\033[1;35m",
    "game_header": "\033[1m",
    "metadata": "\033[2m",
    "state": "\033[33m",
    "result_me": "\033[1;32m",
    "result_opponent": "\033[1;31m",
    "state_detail": "\033[2m",
}
LAND_COLORS = {
    "Plains": WHITE_MANA_COLOR,
    "Island": BLUE_MANA_COLOR,
    "Swamp": BLACK_MANA_COLOR,
    "Mountain": RED_MANA_COLOR,
    "Forest": GREEN_MANA_COLOR,
    "Snow-Covered Plains": WHITE_MANA_COLOR,
    "Snow-Covered Island": BLUE_MANA_COLOR,
    "Snow-Covered Swamp": BLACK_MANA_COLOR,
    "Snow-Covered Mountain": RED_MANA_COLOR,
    "Snow-Covered Forest": GREEN_MANA_COLOR,
    "Wastes": COLOURLESS_MANA_COLOR,
}
COLORLESS_LAND_NAMES = {
    "Cavern of Souls",
    "Field of Ruin",
    "Mutavault",
    "Nykthos, Shrine to Nyx",
    "Reliquary Tower",
}
MULTICOLOUR_LAND_COLORS = {
    "Wind-Scarred Crag": (1, 4),
}
DEFAULT_CARD_NAME_COLORS = {
    **{name: (color,) for name, color in LAND_COLORS.items()},
    **{name: (COLOURLESS_MANA_COLOR,) for name in COLORLESS_LAND_NAMES},
    **{name: colors for name, colors in MULTICOLOUR_LAND_COLORS.items()},
}
ARCHIVE_SCHEMA_VERSION = 1
LAND_NAME_PATTERN = re.compile(
    r"(?<!\w)("
    + "|".join(
        re.escape(name)
        for name in sorted(
            DEFAULT_CARD_NAME_COLORS,
            key=len,
            reverse=True,
        )
    )
    + r")(?!\w)"
)


def should_color_output(color_mode: str, stdout_is_tty: bool) -> bool:
    """Return true when transcript lines should include ANSI color."""
    if color_mode == "always":
        return True
    if color_mode == "auto":
        return stdout_is_tty
    return False


def transcript_line_perspective(line: str) -> str | None:
    """Classify transcript lines that clearly belong to one player."""
    if line.startswith(("  ", "    ")):
        return None
    if line.startswith("=== Turn ") and line.endswith(": Me ==="):
        return "me"
    if line.startswith("=== Turn ") and line.endswith(": Opponent ==="):
        return "opponent"
    if line.startswith(("I ", "My ", "My board:", "Winner: Me", "Match winner: Me")):
        return "me"
    if line.startswith(
        (
            "Opponent ",
            "Opponent:",
            "Opponent's ",
            "Winner: Opponent",
            "Match winner: Opponent",
        )
    ):
        return "opponent"
    return None


def transcript_line_style(line: str) -> str | None:
    """Return the color style name for a transcript line."""
    if not line:
        return None
    if line.startswith("===== GAME "):
        return "game_header"
    if line.startswith("Game type:") or (line.startswith("-- ") and line.endswith("--")):
        return "metadata"
    if line.startswith("=== Turn ") and line.endswith(": Me ==="):
        return "me_header"
    if line.startswith("=== Turn ") and line.endswith(": Opponent ==="):
        return "opponent_header"
    if line == "My board:":
        return "me_header"
    if line == "Opponent's board:":
        return "opponent_header"
    if line.startswith(("  ", "    ")):
        return "state_detail"
    if line.startswith(("Active Effects:", "Current State:", "Available Resources:")):
        return "state"
    if line.startswith(("Winner: Me", "Match winner: Me")):
        return "result_me"
    if line.startswith(("Winner: Opponent", "Match winner: Opponent")):
        return "result_opponent"
    if line.startswith("Match result:"):
        return "result_me" if "Opponent conceded" in line else "result_opponent"
    perspective = transcript_line_perspective(line)
    if perspective:
        return perspective
    if "trigger:" in line or "ability:" in line or line.startswith("Commander damage:"):
        return "state"
    return None


def blended_mana_color_for_values(values) -> str:
    """Return a readable ANSI colour blended from Arena mana colour values."""
    colors = tuple(sorted(value for value in set(values) if value in MANA_COLORS))
    if not colors:
        return COLOURLESS_MANA_COLOR
    if len(colors) == 1:
        return MANA_COLORS[colors[0]]
    return MULTICOLOUR_MANA_COLORS.get(colors, "\033[1;33m")


def build_card_name_colors(card_metadata: dict[int, dict]) -> dict[str, str | tuple[int, ...]]:
    """Build transcript colour accents for known card names."""
    name_colors = dict(DEFAULT_CARD_NAME_COLORS)
    for metadata in card_metadata.values():
        name = metadata.get("name")
        if not name:
            continue
        type_numbers = set(metadata.get("type_numbers") or [])
        if not type_numbers:
            continue
        # Arena card type 5 = land. Lands usually have no printed colours, so
        # use colour identity/frame colours before falling back to Colors.
        is_land = 5 in type_numbers
        if is_land:
            color_values = (
                metadata.get("color_identity")
                or metadata.get("frame_colors")
                or metadata.get("colors")
                or set()
            )
        else:
            color_values = (
                metadata.get("colors")
                or metadata.get("frame_colors")
                or metadata.get("color_identity")
                or set()
            )
        name_colors[name] = blended_mana_color_for_values(color_values)
    return name_colors


def build_card_name_pattern(name_colors: dict[str, str | tuple[int, ...]]) -> re.Pattern | None:
    """Compile a longest-name-first matcher for known card names."""
    if not name_colors:
        return None
    return re.compile(
        r"(?<!\w)("
        + "|".join(re.escape(name) for name in sorted(name_colors, key=len, reverse=True))
        + r")(?!\w)"
    )


def palette_for_card_name(
    name: str,
    name_colors: dict[str, str | tuple[int, ...]] | None = None,
) -> tuple[str, ...]:
    """Return the ANSI colour for a known card name as a one-item palette."""
    if name_colors and name in name_colors:
        color = name_colors[name]
    else:
        color = DEFAULT_CARD_NAME_COLORS.get(name) or (COLOURLESS_MANA_COLOR,)
    if isinstance(color, tuple) and color and isinstance(color[0], int):
        return (blended_mana_color_for_values(color),)
    if isinstance(color, tuple):
        return color
    return (color,)


def colorize_card_name(name: str, palette: tuple[str, ...]) -> str:
    """Apply a mana colour to a card name."""
    return f"{palette[0]}{name}{ANSI_RESET}" if palette else name


def colorize_list_conjunctions(line: str, outer_color: str | None) -> str:
    """Make list conjunctions neutral before highlighted card names."""
    if not outer_color:
        return line

    def replace(match):
        return f"{match.group(1)}{ANSI_RESET}{COLOURLESS_MANA_COLOR}and{ANSI_RESET}{outer_color} "

    return re.sub(r"([;\s])and (?=\033\[)", replace, line)


def colorize_card_names(
    line: str,
    color_enabled: bool,
    outer_color: str | None = None,
    name_colors: dict[str, str | tuple[int, ...]] | None = None,
    name_pattern: re.Pattern | None = None,
) -> str:
    """Apply mana-style colours to known card names in a transcript line."""
    if not color_enabled:
        return line
    name_colors = name_colors or DEFAULT_CARD_NAME_COLORS
    name_pattern = name_pattern or LAND_NAME_PATTERN

    def replace(match):
        name = match.group(0)
        coloured_name = colorize_card_name(name, palette_for_card_name(name, name_colors))
        if outer_color:
            return f"{ANSI_RESET}{coloured_name}{outer_color}"
        return coloured_name

    return colorize_list_conjunctions(name_pattern.sub(replace, line), outer_color)


def colorize_land_names(line: str, color_enabled: bool, outer_color: str | None = None) -> str:
    """Apply mana-style colours to the default known land names in a transcript line."""
    return colorize_card_names(line, color_enabled, outer_color)


def colorize_transcript_line(
    line: str,
    color_enabled: bool,
    name_colors: dict[str, str | tuple[int, ...]] | None = None,
    name_pattern: re.Pattern | None = None,
) -> str:
    """Apply ANSI color to transcript syntax when requested."""
    if not color_enabled:
        return line
    color = TRANSCRIPT_COLORS.get(transcript_line_style(line))
    if transcript_line_style(line) not in {"game_header", "metadata", "me_header", "opponent_header"}:
        line = colorize_card_names(line, color_enabled, color, name_colors, name_pattern)
    if not color:
        return line
    return f"{color}{line.removeprefix(ANSI_RESET)}{ANSI_RESET}"


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


def phrase_life_change_group(label: str, delta: int, count: int, total=None) -> str:
    """Summarize repeated source-less life changes without inventing a source."""
    if count <= 1:
        return phrase_life_change(label, delta, total)
    return f"{count}x {phrase_life_change(label, delta * count, total)}"


def phrase_life_change_summary(source: str | None, label: str, delta: int, count: int, total=None) -> str:
    """Summarize repeated same-source life changes with the final life total."""
    if not source:
        return phrase_life_change_group(label, delta, count, total)
    if count <= 1:
        return f"{source}: {phrase_life_change(label, delta, total)}"
    total_delta = delta * count
    return f"{count}x {source}: {phrase_life_change(label, total_delta, total)}"


def should_group_life_change_source(source: str | None) -> bool:
    """Return true for named ability sources that make repeated life lines readable."""
    return bool(source and (source.endswith(" trigger") or source.endswith(" ability")))


def life_change_group_source(source: str | None) -> str | None:
    """Normalize known permanent sources when grouping life-change bursts."""
    if not source or should_group_life_change_source(source):
        return source
    return f"{source} ability"


def phase_section_label(turn_info: dict) -> str | None:
    """Return a compact transcript heading for an Arena phase/step pair."""
    phase = turn_info.get("phase")
    step = turn_info.get("step")
    if phase == "Phase_Beginning":
        return {
            "Step_Untap": "Beginning - untap",
            "Step_Upkeep": "Beginning - upkeep",
            "Step_Draw": "Beginning - draw",
        }.get(step, "Beginning")
    if phase == "Phase_Main1":
        return None
    if phase == "Phase_Combat":
        return {
            "Step_BeginCombat": "Combat - beginning",
            "Step_DeclareAttack": "Combat - attackers",
            "Step_DeclareBlock": "Combat - blockers",
            "Step_FirstStrikeDamage": "Combat - first strike damage",
            "Step_CombatDamage": "Combat - damage",
            "Step_EndCombat": "Combat - end",
        }.get(step, "Combat")
    if phase == "Phase_Main2":
        return "Postcombat main"
    if phase == "Phase_Ending":
        return {
            "Step_End": "Ending - end step",
            "Step_Cleanup": "Ending - cleanup",
        }.get(step, "Ending")
    return None


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
        return f"{count} of my {card_name} tokens die"
    elif controller_label == "Opponent":
        owner = "opponent"
    elif controller_label:
        return f"{count} of {controller_label}'s {card_name} tokens die"
    else:
        owner = ""
    prefix = f"{count} {owner} ".strip()
    return f"{prefix} {card_name} tokens die"


def phrase_enters_attacking(source: str | None, creature: str) -> str:
    """Render a creature/token entering already attacking without inventing a source."""
    if source:
        return f"{source} puts {creature} onto the battlefield attacking"
    return f"{creature} enters the battlefield attacking"


def attack_phrase(target: str | None, attacker: str) -> str:
    """Render attack details without leaking raw ids for unknown targets."""
    if target:
        return f"{target} with {attacker}"
    return f"with {attacker}"


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


def scaled_power_toughness_counter(name: str, amount: int) -> str | None:
    """Render repeated +1/+1 style counters as their total P/T modifier."""
    match = re.fullmatch(r"([+-])(\d+)/([+-])(\d+)", name)
    if not match:
        return None
    power_sign, power, toughness_sign, toughness = match.groups()
    power_total = int(power) * amount
    toughness_total = int(toughness) * amount
    return f"{power_sign}{power_total}/{toughness_sign}{toughness_total}"


def grouped_name_phrase(names: list[str]) -> str:
    """Render repeated attachment names in the same style as board state."""
    counts = Counter(name for name in names if name)
    return ", ".join(
        f"{count}x {name}" if count > 1 else name
        for name, count in sorted(counts.items())
    )


def compact_counted_name(name: str, count: int) -> str:
    """Render repeated board/zone names, pluralizing hidden-card labels."""
    if count <= 1:
        return name
    if name == "unknown card":
        return f"{count} unknown cards"
    if name == "a face-down card":
        return f"{count} face-down cards"
    return f"{count}x {name}"


def should_combine_transcript_line(line: str) -> bool:
    """Return true for plain event lines that are safe to collapse when adjacent."""
    if not line or line.startswith(("=", " ", "Active Effects:", "Game State:")):
        return False
    if line.startswith(("Winner:", "Match winner:", "Match result:")):
        return False
    return True


def combine_duplicate_transcript_lines(lines: list[str]) -> list[str]:
    """Collapse adjacent identical event lines into a compact 'Nx ...' line."""
    combined = []
    index = 0
    while index < len(lines):
        line = lines[index]
        count = 1
        if should_combine_transcript_line(line):
            while (
                index + count < len(lines)
                and lines[index + count] == line
                and should_combine_transcript_line(lines[index + count])
            ):
                count += 1
        if count > 1:
            combined.append(f"{count}x {line}")
        else:
            combined.append(line)
        index += count
    return combined


def joined_english_list(items: list[str]) -> str:
    """Join names as A, B, and C for transcript summaries."""
    if len(items) <= 1:
        return items[0] if items else ""
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def joined_semicolon_list(items: list[str]) -> str:
    """Join card names with semicolons because card names often contain commas."""
    if len(items) <= 1:
        return items[0] if items else ""
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{'; '.join(items[:-1])}; and {items[-1]}"


def parse_attack_line(line: str) -> tuple[str, str, str] | None:
    """Parse simple attack declaration transcript lines."""
    match = re.fullmatch(r"(I|Opponent) attacks? (.+) with (.+)", line)
    if not match:
        return None
    actor, target, attacker = match.groups()
    return actor, target, attacker


def grouped_attack_line(actor: str, target: str, attackers: list[str]) -> str:
    """Render adjacent same-target attacks as one readable line."""
    verb = "attack" if actor == "I" else "attacks"
    return f"{actor} {verb} {target} with {joined_semicolon_list(attackers)}"


def combine_adjacent_attack_lines(lines: list[str]) -> list[str]:
    """Collapse adjacent same-player, same-target attack declarations."""
    combined = []
    index = 0
    while index < len(lines):
        parsed = parse_attack_line(lines[index])
        if not parsed:
            combined.append(lines[index])
            index += 1
            continue

        actor, target, attacker = parsed
        attackers = [attacker]
        cursor = index + 1
        while cursor < len(lines):
            next_parsed = parse_attack_line(lines[cursor])
            if not next_parsed:
                break
            next_actor, next_target, next_attacker = next_parsed
            if next_actor != actor or next_target != target:
                break
            attackers.append(next_attacker)
            cursor += 1

        if len(attackers) == 1:
            combined.append(lines[index])
        else:
            combined.append(grouped_attack_line(actor, target, attackers))
        index = cursor
    return combined


def result_winner(line: str, prefix: str) -> str | None:
    """Return the winner from a result line with a specific prefix."""
    if not line.startswith(prefix):
        return None
    return line.removeprefix(prefix).strip()


def remove_redundant_match_winner_lines(lines: list[str]) -> list[str]:
    """Remove repeated match-winner lines after same-winner game results."""
    cleaned = []
    last_game_winner = None
    pending_blank_after_game_winner = False
    for line in lines:
        game_winner = result_winner(line, "Winner:")
        if game_winner:
            cleaned.append(line)
            last_game_winner = game_winner
            pending_blank_after_game_winner = False
            continue

        match_winner = result_winner(line, "Match winner:")
        if match_winner and match_winner == last_game_winner:
            if pending_blank_after_game_winner and cleaned and cleaned[-1] == "":
                cleaned.pop()
            pending_blank_after_game_winner = False
            continue

        cleaned.append(line)
        if line == "" and last_game_winner:
            pending_blank_after_game_winner = True
        elif line:
            last_game_winner = None
            pending_blank_after_game_winner = False
    return cleaned


def modifier_summary_suffix(parts: list[str]) -> str:
    """Render permanent modifiers in one parenthetical board-state suffix."""
    return f" ({'; '.join(parts)})" if parts else ""


def counter_summary_parts(counter_totals: Counter, counter_names: dict[int, str]) -> list[str]:
    """Render object counters as board-state modifier fragments."""
    parts = []
    for raw_type, amount in sorted(counter_totals.items(), key=lambda item: str(item[0])):
        if amount <= 0:
            continue
        name = counter_names.get(raw_type, f"counter {raw_type}")
        scaled = scaled_power_toughness_counter(name, amount)
        if scaled:
            parts.append(f"{scaled} from counters")
            continue
        noun = "" if name.startswith("counter ") else " counter"
        if amount == 1:
            parts.append(f"{name}{noun}")
        else:
            parts.append(f"{name}{noun}s: {amount}")
    return parts


def counter_summary_suffix(counter_totals: Counter, counter_names: dict[int, str]) -> str:
    """Render object counters as a stable suffix for board-state grouping."""
    return modifier_summary_suffix(counter_summary_parts(counter_totals, counter_names))


def attachment_summary_parts(attachment_names_by_kind: dict[str, list[str]]) -> list[str]:
    """Render Arena attachment annotations as modified-permanent fragments."""
    parts = []
    for kind, verb in (
        ("aura", "enchanted by"),
        ("equipment", "equipped with"),
        ("other", "attached to"),
    ):
        phrase = grouped_name_phrase(attachment_names_by_kind.get(kind, []))
        if phrase:
            parts.append(f"{verb} {phrase}")
    return parts


def ownership_summary_part(controller_label: str, owner_label_text: str) -> str | None:
    """Describe controlled-but-not-owned permanents for board snapshots."""
    if controller_label == owner_label_text:
        return None
    return f"owned by {object_pronoun(owner_label_text)}"


def format_target_phrase(target_names: list[str]) -> str:
    """Render one or more resolved spell targets for a cast line."""
    return "; ".join(target_names)


def append_target_phrase(text: str, target_names: list[str]) -> str:
    """Append target text to a cast/action phrase when targets are known."""
    if not target_names:
        return text
    return f"{text} targeting {format_target_phrase(target_names)}"


def base_cast_name(cast_text: str) -> str:
    """Strip transcript suffixes so delayed casts can be matched by spell name."""
    return (
        cast_text.split(" targeting ", 1)[0]
        .split(" from command zone", 1)[0]
        .split(";", 1)[0]
    )


def is_target_like_key(key: str) -> bool:
    """Return true for raw payload keys that are useful for target debugging."""
    key = str(key).lower()
    return "target" in key or key in {"affectedids", "affectorid", "objectid", "instanceid"}


def find_target_like_paths(payload, path=""):
    """Find raw JSON key paths that may describe spell or ability targets."""
    paths = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            child_path = f"{path}.{key}" if path else str(key)
            if is_target_like_key(key):
                paths.append(child_path)
            paths.extend(find_target_like_paths(value, child_path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            paths.extend(find_target_like_paths(value, f"{path}[{index}]"))
    return paths


def should_emit_resolve_line(name: str, instance_id: int) -> bool:
    """Return false for anonymous stack resolves that would leak raw ids."""
    return name != f"instance {instance_id}"


def should_infer_missing_cast_before_resolve(
    name: str,
    instance_id: int,
    obj: dict | None,
    emitted_cast_instance_ids: set[int],
) -> bool:
    """Return true when a named spell resolved but its cast was hidden earlier."""
    if instance_id in emitted_cast_instance_ids:
        return False
    if not obj or obj.get("type") != "GameObjectType_Card":
        return False
    if "CardType_Land" in (obj.get("cardTypes") or []):
        return False
    if obj.get("isCopy"):
        return False
    return should_emit_resolve_line(name, instance_id)


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


def is_hidden_arena_object(obj: dict | None) -> bool:
    """Return true for Arena placeholder objects whose real card is hidden."""
    if not obj:
        return False
    return bool(obj.get("isFacedown")) or obj.get("grpId") == 3


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


def card_has_type(obj: dict | None, metadata: dict | None, arena_type: str, type_number: int) -> bool:
    """Check a card type using live object enums first, then card DB metadata."""
    if obj and arena_type in set(obj.get("cardTypes") or []):
        return True
    return bool(metadata and type_number in set(metadata.get("type_numbers") or []))


def card_is_land(obj: dict | None, metadata: dict | None) -> bool:
    """Return true when a card is known to be a land."""
    return card_has_type(obj, metadata, "CardType_Land", 5)


def card_is_nonland_permanent(obj: dict | None, metadata: dict | None) -> bool:
    """Return true for known nonland permanent card types."""
    permanent_checks = (
        ("CardType_Artifact", 1),
        ("CardType_Creature", 2),
        ("CardType_Enchantment", 3),
        ("CardType_Planeswalker", 8),
        ("CardType_Battle", 12),
    )
    if card_is_land(obj, metadata):
        return False
    return any(card_has_type(obj, metadata, enum_name, number) for enum_name, number in permanent_checks)


def available_resource_lines(resources: dict[str, list[str]]) -> list[str]:
    """Format the optional Available Resources block for a turn-state snapshot."""
    sections = [
        ("potential_graveyard_exile_plays", "Other playable cards"),
    ]
    lines = []
    for key, title in sections:
        values = resources.get(key) or []
        if not values:
            continue
        if not lines:
            lines.append("Available Resources:")
        lines.append(f"  {title}:")
        for value in values:
            lines.append(f"    {value}")
    return lines


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


def new_match_record(number: int, match_id: str, lines: list[str]) -> dict:
    """Build the per-match transcript/debug container."""
    return {
        "number": number, "match_id": match_id, "lines": lines,
        "events": [], "debug_hits": [],
        "debug_seen_objects": set(),
        "choice_events": [], "target_events": [], "trigger_events": [],
        "has_result": False, "saw_postgame_payload": False,
        "postgame_hint": None, "finalized": False,
    }


def default_archive_db_path() -> Path:
    return Path("mtga_seen_games.sqlite3")


def cleaned_transcript_lines(lines: list[str]) -> list[str]:
    return remove_redundant_match_winner_lines(
        combine_adjacent_attack_lines(combine_duplicate_transcript_lines(lines))
    )


def transcript_with_game_header(lines: list[str], number: int, match_id: str) -> list[str]:
    header = f"===== GAME {number}: MATCH {match_id} ====="
    if lines and lines[0].startswith("===== GAME ") and f"MATCH {match_id}" in lines[0]:
        return [header, *lines[1:]]
    return [header, *lines]


def ensure_archive_schema(con) -> None:
    current_version = con.execute("PRAGMA user_version").fetchone()[0]
    if current_version > ARCHIVE_SCHEMA_VERSION:
        raise ValueError(
            f"Archive database schema version {current_version} is newer than "
            f"this program supports ({ARCHIVE_SCHEMA_VERSION})."
        )

    old_games = []
    existing_tables = {
        row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "games" in existing_tables:
        columns = {row[1] for row in con.execute("PRAGMA table_info(games)")}
        if "transcript" in columns:
            old_games = list(
                con.execute(
                    """
                    SELECT
                        match_id,
                        COALESCE(archive_index, game_number),
                        game_type,
                        has_result,
                        line_count,
                        transcript,
                        first_seen_at,
                        last_seen_at
                    FROM games
                    ORDER BY COALESCE(archive_index, game_number), first_seen_at
                    """
                )
            )
            con.execute("ALTER TABLE games RENAME TO games_legacy_v0")

    con.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY,
            match_id TEXT NOT NULL UNIQUE,
            archive_index INTEGER NOT NULL UNIQUE,
            game_type TEXT,
            has_result INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS transcripts (
            id INTEGER PRIMARY KEY,
            game_id INTEGER NOT NULL,
            format TEXT NOT NULL DEFAULT 'plain_text',
            parser_version TEXT,
            line_count INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(game_id, format),
            FOREIGN KEY(game_id) REFERENCES games(id) ON DELETE CASCADE
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS log_sources (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            size_bytes INTEGER,
            modified_at REAL,
            first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS games_archive_index_idx ON games(archive_index)")
    con.execute("CREATE INDEX IF NOT EXISTS transcripts_game_id_idx ON transcripts(game_id)")

    for (
        match_id,
        archive_index,
        game_type,
        has_result,
        line_count,
        transcript,
        first_seen_at,
        last_seen_at,
    ) in old_games:
        con.execute(
            """
            INSERT OR IGNORE INTO games (
                match_id, archive_index, game_type, has_result, first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (match_id, archive_index, game_type, has_result, first_seen_at, last_seen_at),
        )
        game_id = con.execute("SELECT id FROM games WHERE match_id = ?", (match_id,)).fetchone()[0]
        con.execute(
            """
            INSERT OR REPLACE INTO transcripts (
                game_id, format, parser_version, line_count, content, created_at, updated_at
            )
            VALUES (?, 'plain_text', NULL, ?, ?, ?, ?)
            """,
            (game_id, line_count, transcript, first_seen_at, last_seen_at),
        )
    con.execute(f"PRAGMA user_version = {ARCHIVE_SCHEMA_VERSION}")
    con.commit()


def archive_seen_games(
    db_path: Path,
    matches: list[dict],
    log_paths: list[Path] | None = None,
) -> tuple[int, int]:
    """Store parsed transcripts by match id and return inserted/updated counts."""
    db_path = db_path.expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    ensure_archive_schema(con)

    for log_path in log_paths or []:
        expanded = log_path.expanduser()
        try:
            stat = expanded.stat()
        except FileNotFoundError:
            continue
        con.execute(
            """
            INSERT INTO log_sources (path, size_bytes, modified_at)
            VALUES (?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size_bytes=excluded.size_bytes,
                modified_at=excluded.modified_at,
                last_seen_at=CURRENT_TIMESTAMP
            """,
            (str(expanded), stat.st_size, stat.st_mtime),
        )

    existing = {}
    if matches:
        existing = {
            match_id: archive_index
            for match_id, archive_index in con.execute(
                "SELECT match_id, archive_index FROM games WHERE match_id IN (%s)"
                % ",".join("?" for _ in matches),
                [match["match_id"] for match in matches],
            )
        }
    next_archive_index = (
        con.execute("SELECT COALESCE(MAX(archive_index), 0) + 1 FROM games").fetchone()[0]
    )

    inserted = 0
    updated = 0
    for match in matches:
        if match["match_id"] in existing:
            archive_index = existing[match["match_id"]]
        else:
            archive_index = next_archive_index
            next_archive_index += 1
        lines = cleaned_transcript_lines(match["lines"])
        lines = transcript_with_game_header(lines, archive_index, match["match_id"])
        game_type = next((line for line in lines if line.startswith("Game type:")), None)
        transcript = "\n".join(lines)
        con.execute(
            """
            INSERT INTO games (
                match_id, archive_index, game_type, has_result
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(match_id) DO UPDATE SET
                game_type=excluded.game_type,
                has_result=excluded.has_result,
                last_seen_at=CURRENT_TIMESTAMP
            """,
            (
                match["match_id"],
                archive_index,
                game_type,
                1 if match.get("has_result") else 0,
            ),
        )
        game_id = con.execute(
            "SELECT id FROM games WHERE match_id = ?",
            (match["match_id"],),
        ).fetchone()[0]
        con.execute(
            """
            INSERT INTO transcripts (
                game_id, format, parser_version, line_count, content
            )
            VALUES (?, 'plain_text', ?, ?, ?)
            ON CONFLICT(game_id, format) DO UPDATE SET
                parser_version=excluded.parser_version,
                line_count=excluded.line_count,
                content=excluded.content,
                updated_at=CURRENT_TIMESTAMP
            """,
            (game_id, ARCHIVE_SCHEMA_VERSION, len(lines), transcript),
        )
        if match["match_id"] in existing:
            updated += 1
        else:
            inserted += 1
    con.commit()
    con.close()
    return inserted, updated


def archived_transcript_matches(db_path: Path) -> list[dict]:
    """Load archived transcripts in stable archive order for normal output selection."""
    con = sqlite3.connect(db_path.expanduser())
    ensure_archive_schema(con)
    rows = list(
        con.execute(
            """
            SELECT g.archive_index, g.match_id, g.has_result, t.content
            FROM games g
            JOIN transcripts t ON t.game_id = g.id AND t.format = 'plain_text'
            ORDER BY archive_index
            """
        )
    )
    con.close()
    matches = []
    for archive_index, match_id, has_result, transcript in rows:
        matches.append(
            {
                "number": archive_index,
                "match_id": match_id,
                "lines": transcript.splitlines(),
                "events": [],
                "debug_hits": [],
                "debug_seen_objects": set(),
                "choice_events": [],
                "target_events": [],
                "trigger_events": [],
                "has_result": bool(has_result),
                "saw_postgame_payload": False,
                "postgame_hint": None,
                "finalized": True,
            }
        )
    return matches


def extract_game_plays(
    player_log: Path,
    grp_to_name: dict[int, str],
    *,
    debug_annotations: bool = False,
    debug_grp_ids: set[int] | None = None,
    debug_choices: bool = False,
    debug_targets: bool = False,
    debug_triggers: bool = False,
    nth_from_start: int | None = None,
    last_games: int | None = None,
    first_games: int | None = None,
    nth_from_end: int | None = None,
    game_range: tuple[int, int] | None = None,
    show_progress: bool | None = None,
    show_resolves: bool = True,
    show_turn_state: bool = True,
    show_phases: bool = True,
    live: bool = False,
    enum_value_names: dict[str, dict[int, str]] | None = None,
    card_metadata: dict[int, dict] | None = None,
    ability_texts: dict[int, str] | None = None,
    color_mode: str = "never",
    archive_db_path: Path | None = None,
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
    seen_enters_attacking = set()
    remembered_object_labels = {}
    stack_display_names = {}
    emitted_cast_instance_ids = set()
    ability_trigger_ids = set()
    ability_source_names = {}
    pending_stack_casts = {}
    pending_mill_group = None
    pending_life_groups = {}
    pending_death_events = []
    active_effects = {}
    commander_grps_by_seat = {}
    commander_instance_ids = set()
    commander_cast_counts = Counter()
    commander_damage = Counter()
    player_counters = Counter()
    object_counters = defaultdict(Counter)
    persistent_annotations = {}
    known_targets_by_source = defaultdict(list)
    known_target_names_by_source = defaultdict(list)
    known_target_ability_ids_by_source = defaultdict(list)
    current_match = None
    current_turn = None
    last_game_state_id = None
    current_game_has_commanders = False
    known_local_seat = None
    current_match_number = 0
    current_turn_info = {}
    current_phase_section = None
    emitted_phase_section = None
    suppress_phase_heading = False
    current_match_lines = None
    current_match_record = None
    transcript_matches = []
    event_index = 0
    seen_match_ids = set()
    skipping_duplicate_match = False
    debug_grp_ids = debug_grp_ids or set()

    debug_counts = Counter()
    debug_samples = {}
    log_paths = player_log_paths_for_reading(player_log, live)
    if show_progress is None:
        show_progress = (
            sys.stderr.isatty()
            and not sys.stdout.isatty()
            and player_log.exists()
            and player_log.stat().st_size >= 10 * 1024 * 1024
        )
    total_bytes = sum(log_path.stat().st_size for log_path in log_paths if log_path.exists())
    read_bytes = 0
    last_progress_at = 0.0

    enum_value_names = enum_value_names or {}
    card_metadata = card_metadata or {}
    ability_texts = ability_texts or {}
    color_enabled = should_color_output(color_mode, sys.stdout.isatty())
    card_name_colors = build_card_name_colors(card_metadata)
    card_name_pattern = build_card_name_pattern(card_name_colors)
    subtype_names = enum_value_names.get("SubType") or {}
    counter_type_names = enum_value_names.get("CounterType") or {}
    choice_value_names = build_choice_value_names(subtype_names)
    def transcript_line_needs_phase_heading(line: str) -> bool:
        """Return true for visible gameplay lines that benefit from phase context."""
        if not line or line.startswith(("=", "-- ", "  ", "    ")):
            return False
        if line.startswith(
            (
                "Game type:",
                "Active Effects:",
                "Available Resources:",
                "Current State:",
                "My hand:",
                "Opponent's hand:",
                "My board:",
                "Opponent's board:",
                "Winner:",
                "Match result:",
                "Match winner:",
            )
        ):
            return False
        return True

    def emit_phase_heading_if_needed(line: str):
        """Emit the current phase heading before the first gameplay line in it."""
        nonlocal emitted_phase_section
        if (
            not show_phases
            or suppress_phase_heading
            or not current_phase_section
            or emitted_phase_section == current_phase_section
            or not transcript_line_needs_phase_heading(line)
        ):
            return
        if current_match_lines is None:
            return
        if current_match_lines and current_match_lines[-1]:
            current_match_lines.append("")
            if live:
                print("", flush=True)
        heading = f"-- {current_phase_section} --"
        current_match_lines.append(heading)
        if live:
            print(
                colorize_transcript_line(
                    heading,
                    color_enabled,
                    card_name_colors,
                    card_name_pattern,
                ),
                flush=True,
            )
        emitted_phase_section = current_phase_section

    def emit(line=""):
        emit_phase_heading_if_needed(line)
        if current_match_lines is not None:
            current_match_lines.append(line)
            if live:
                print(
                    colorize_transcript_line(
                        line,
                        color_enabled,
                        card_name_colors,
                        card_name_pattern,
                    ),
                    flush=True,
                )

    def emit_in_phase_section(line: str, section: str | None):
        """Emit a delayed grouped line under the section where it was recorded."""
        nonlocal current_phase_section
        saved_section = current_phase_section
        current_phase_section = section
        try:
            emit(line)
        finally:
            current_phase_section = saved_section

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

    def reset_match_state(gsm):
        """Clear state that belongs only to the newly started Arena match."""
        nonlocal current_turn, current_turn_info, last_game_state_id
        nonlocal current_game_has_commanders, known_local_seat, event_index
        nonlocal pending_mill_group, current_phase_section, emitted_phase_section

        event_index = 0
        current_turn = None
        current_turn_info = {}
        current_phase_section = None
        emitted_phase_section = None
        last_game_state_id = None
        current_game_has_commanders = game_has_commanders(gsm.get("gameInfo"))
        known_local_seat = None

        clear_all(
            zones,
            objects,
            players,
            team_to_seats,
            remembered_object_labels,
            stack_display_names,
            ability_source_names,
            pending_stack_casts,
            pending_life_groups,
            active_effects,
            commander_grps_by_seat,
            commander_damage,
            player_counters,
            object_counters,
            persistent_annotations,
            known_targets_by_source,
            known_target_names_by_source,
            known_target_ability_ids_by_source,
        )
        clear_all(
            seen_combat,
            seen_commander_damage,
            seen_no_combat_damage,
            seen_enters_attacking,
            ability_trigger_ids,
            commander_instance_ids,
            commander_cast_counts,
            pending_death_events,
        )
        pending_mill_group = None

    def start_match(match_id, gsm):
        """Initialize transcript and parser state for a new Arena match id."""
        nonlocal current_match, current_match_number
        nonlocal current_match_lines, current_match_record
        nonlocal skipping_duplicate_match

        finalize_current_match()
        current_match = match_id
        skipping_duplicate_match = False
        seen_match_ids.add(match_id)
        current_match_number += 1
        current_match_lines = []
        current_match_record = new_match_record(
            current_match_number,
            current_match,
            current_match_lines,
        )
        transcript_matches.append(current_match_record)
        reset_match_state(gsm)

        emit(f"===== GAME {current_match_number}: MATCH {current_match} =====")
        game_type_line = format_game_type(gsm.get("gameInfo"), gsm.get("players"))
        if game_type_line:
            emit(game_type_line)

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

    def compact_annotation(ann):
        """Keep debug trigger output readable by printing only the useful annotation fields."""
        return {
            "type": ann.get("type"),
            "affectorId": ann.get("affectorId"),
            "affectorName": source_label(ann.get("affectorId")),
            "affectedIds": ann.get("affectedIds") or [],
            "affectedNames": [card_label(iid) for iid in ann.get("affectedIds") or []],
            "details": detail_dict(ann.get("details")),
        }

    def trigger_debug_annotations(gsm):
        """Collect compact snippets for ability/trigger-like gameplay annotations."""
        snippets = []
        for ann in gsm.get("annotations") or []:
            ann_types = set(ann.get("type") or [])
            details = detail_dict(ann.get("details"))
            affector = ann.get("affectorId")
            affected = ann.get("affectedIds") or []
            affector_obj = objects.get(affector) or {}
            source = source_label(affector)
            affected_attacking = any(
                (objects.get(iid) or {}).get("attackState") == "AttackState_Attacking"
                for iid in affected
            )

            include = False
            if "AnnotationType_AbilityInstanceCreated" in ann_types:
                # Arena creates ability instances for routine mana abilities.
                # Keep debug output focused on non-land sources and combat
                # triggers, which are the interesting discovery path here.
                include = "CardType_Land" not in set(affector_obj.get("cardTypes") or [])
            elif "AnnotationType_ZoneTransfer" in ann_types:
                include = affected_attacking or (
                    source
                    and source.endswith(" trigger")
                    and details.get("category") not in {"CastSpell", "PlayLand"}
                )
            elif "AnnotationType_ModifiedLife" in ann_types:
                include = bool(source and source.endswith(" trigger"))
            elif "AnnotationType_LayeredEffectCreated" in ann_types:
                include = affected_attacking

            if include:
                snippets.append(compact_annotation(ann))
        return snippets

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

    def object_controller(instance_id):
        """Return the controller seat for cast/play attribution when known."""
        obj = objects.get(instance_id, {})
        return obj.get("controllerSeatId")

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
        if is_hidden_arena_object(obj):
            # Effects like Gonti exile cards face down. Arena uses grpId 3 for
            # these placeholders, which is not a card database id we should
            # print as though it were a card name.
            return "a face-down card"
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
        if orig_id in pending_stack_casts:
            # Low-fidelity Send updates can delay a cast line until a later
            # Resolve, and Arena may change the stack object's id in between.
            # Move the pending cast to the new id so it is not lost.
            pending_stack_casts[new_id] = pending_stack_casts.pop(orig_id)
            if pending_stack_casts[new_id].get("source_id") == orig_id:
                pending_stack_casts[new_id]["source_id"] = new_id
        if orig_id in object_counters:
            # Counter annotations are tied to instance ids. Carry them across
            # object id changes so board-state grouping remains accurate.
            object_counters[new_id].update(object_counters.pop(orig_id))
        for ann in persistent_annotations.values():
            # TargetSpec annotations may be created before Arena changes the
            # stack object's id. Keep both source and target references aligned
            # so delayed cast lines can still name targets at resolve time.
            if ann.get("affectorId") == orig_id:
                ann["affectorId"] = new_id
            if orig_id in (ann.get("affectedIds") or []):
                ann["affectedIds"] = [
                    new_id if affected_id == orig_id else affected_id
                    for affected_id in ann.get("affectedIds") or []
                ]
        if orig_id in known_targets_by_source:
            known_targets_by_source[new_id].extend(known_targets_by_source.pop(orig_id))
        if orig_id in known_target_names_by_source:
            known_target_names_by_source[new_id].extend(known_target_names_by_source.pop(orig_id))
        if orig_id in known_target_ability_ids_by_source:
            known_target_ability_ids_by_source[new_id].extend(
                known_target_ability_ids_by_source.pop(orig_id)
            )
        for source_id, target_ids in list(known_targets_by_source.items()):
            known_targets_by_source[source_id] = [
                new_id if target_id == orig_id else target_id for target_id in target_ids
            ]
        if orig_id in emitted_cast_instance_ids:
            # Arena may change the stack object's instance id between the
            # hidden cast and the resolve. Preserve the emitted-cast marker so
            # the fallback does not narrate the same spell twice.
            emitted_cast_instance_ids.add(new_id)

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

    def confident_combat_target_label(target_id):
        """Return a readable attack target, or None for unidentified objects."""
        if target_id in (1, 2):
            return object_pronoun(owner_label(target_id))
        name = (
            object_label_from_object(objects.get(target_id, {}))
            or remembered_object_labels.get(target_id)
        )
        if not name or name == f"instance {target_id}" or name.startswith("grpId "):
            return None
        if name in {"unknown card", "a face-down card"}:
            return None
        return name

    def confident_target_name(target_id):
        """Resolve a target id only when it maps to a readable current object."""
        if target_id in (1, 2):
            return object_pronoun(owner_label(target_id))
        name = card_label(target_id)
        if name in {"unknown card", "a face-down card", f"instance {target_id}"}:
            return None
        if name.startswith("grpId "):
            return None
        return name

    def target_ids_for_source(source_id):
        """Read chosen targets from Arena TargetSpec persistent annotations."""
        target_ids = []
        for target_id in known_targets_by_source.get(source_id, []):
            if target_id != source_id and target_id not in target_ids:
                target_ids.append(target_id)
        for ann in persistent_annotations.values():
            if "AnnotationType_TargetSpec" not in set(ann.get("type") or []):
                continue
            if ann.get("affectorId") != source_id:
                continue
            # In observed GRE payloads, TargetSpec.affectorId is the spell or
            # ability object and TargetSpec.affectedIds are the selected
            # target objects. This is safer than inferring targets from later
            # destroy/exile/bounce effects.
            for target_id in ann.get("affectedIds") or []:
                if target_id != source_id and target_id not in target_ids:
                    target_ids.append(target_id)
        return target_ids

    def target_names_for_source(source_id, source_name=None):
        """Return readable target names for a spell/ability source id."""
        names = []
        for name in known_target_names_by_source.get(source_id, []):
            if name and name not in names:
                names.append(name)
        for target_id in target_ids_for_source(source_id):
            name = confident_target_name(target_id)
            if name and name not in names:
                names.append(name)
        if not names and source_name:
            matching_sources = [
                candidate_id
                for candidate_id, candidate_names in known_target_names_by_source.items()
                if candidate_names and card_label(candidate_id) == source_name
            ]
            if len(matching_sources) == 1:
                for name in known_target_names_by_source[matching_sources[0]]:
                    if name and name not in names:
                        names.append(name)
        return names

    def target_spec_ability_ids_for_source(source_id):
        """Return ability ids Arena attached to target specs for a source."""
        ability_ids = []
        for ability_id in known_target_ability_ids_by_source.get(source_id, []):
            if ability_id not in ability_ids:
                ability_ids.append(ability_id)
        for ann in persistent_annotations.values():
            if "AnnotationType_TargetSpec" not in set(ann.get("type") or []):
                continue
            if ann.get("affectorId") != source_id:
                continue
            ability_id = detail_dict(ann.get("details")).get("abilityGrpId")
            if isinstance(ability_id, int) and ability_id not in ability_ids:
                ability_ids.append(ability_id)
        return ability_ids

    def ability_choice_text_for_source(source_id):
        """Return chosen modal ability text when TargetSpec exposes one."""
        obj = objects.get(source_id, {})
        source_ability_ids = [
            ability.get("grpId")
            for ability in obj.get("uniqueAbilities") or []
            if ability.get("grpId") is not None
        ]
        source_texts = [ability_texts.get(ability_id) for ability_id in source_ability_ids]
        source_metadata = metadata_for_object(obj) or {}
        source_texts.extend(source_metadata.get("ability_texts") or [])
        has_modal_parent = any(
            text and "choose one" in text.casefold()
            for text in source_texts
        )
        if not has_modal_parent:
            return None

        for ability_id in target_spec_ability_ids_for_source(source_id):
            text = ability_texts.get(ability_id)
            if text and "choose one" not in text.casefold():
                return text
        return None

    def cast_text_with_targets(source_id, text):
        """Append target wording to a cast line when Arena gives targets."""
        source_name = text.split(" from command zone", 1)[0].split(";", 1)[0]
        text = append_target_phrase(text, target_names_for_source(source_id, source_name))
        choice_text = ability_choice_text_for_source(source_id)
        if choice_text:
            text = f"{text} ({choice_text})"
        return text

    def source_label(instance_id):
        """Return a source card name only when it is more useful than a raw id."""
        if instance_id is None:
            return None
        # AbilityInstanceCreated annotations can name a later ability object even
        # when the object snapshot is gone by the time ModifiedLife points at it.
        # Check that remembered label before falling back to "instance N".
        if instance_id in remembered_object_labels and not objects.get(instance_id):
            return remembered_object_labels[instance_id]
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

    def is_battlefield_zone(zone_id):
        """Return true when a zone id is one of Arena's battlefield zones."""
        return zones.get(zone_id, {}).get("type") == "ZoneType_Battlefield"

    def object_is_attacking_creature(obj):
        """Detect current object state for creatures already attacking."""
        if not obj or obj.get("attackState") != "AttackState_Attacking":
            return False
        return "CardType_Creature" in set(obj.get("cardTypes") or [])

    def action_activates_source(gsm, source_id):
        """Return true when the same GRE update shows a player activating this source."""
        for action_wrapper in gsm.get("actions") or []:
            action = action_wrapper.get("action") or {}
            if action.get("instanceId") != source_id:
                continue
            if str(action.get("actionType") or "").startswith("ActionType_Activate"):
                return True
        return False

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
            parts.append(compact_counted_name(name, count))
        return "; ".join(parts)

    def card_label_for_snapshot(instance_id):
        """Return a snapshot label, preserving hidden cards as unknown."""
        obj = objects.get(instance_id, {})
        if is_hidden_arena_object(obj):
            return "unknown card"
        if not obj.get("grpId") and not obj.get("objectSourceGrpId"):
            return "unknown card"
        label = card_label(instance_id)
        return "unknown card" if label.startswith("grpId ") else label

    def metadata_for_object(obj):
        """Return card DB metadata for an Arena object when its grpId is known."""
        grp_id = obj.get("grpId") or obj.get("objectSourceGrpId")
        try:
            return card_metadata.get(int(grp_id))
        except (TypeError, ValueError):
            return None

    def zone_object_entries(zone_type, seat=None):
        """Collect visible object records from a zone for resource analysis."""
        entries = []
        for zone in zones.values():
            if zone.get("type") != zone_type:
                continue
            zone_owner_id = zone.get("ownerSeatId")
            if seat is not None and zone_owner_id is not None and zone_owner_id != seat:
                continue
            for instance_id in zone.get("objectInstanceIds") or []:
                if seat is not None and zone_owner_id is None and object_owner(instance_id) != seat:
                    continue
                obj = objects.get(instance_id, {})
                name = card_label_for_snapshot(instance_id)
                if name in {"unknown card", "a face-down card"}:
                    continue
                entries.append(
                    {
                        "instance_id": instance_id,
                        "object": obj,
                        "name": name,
                        "metadata": metadata_for_object(obj),
                    }
                )
        return entries

    def controlled_battlefield_names(seat):
        """Return names of permanents a player currently controls."""
        names = set()
        battlefield_zone_ids = zone_ids_by_type("ZoneType_Battlefield")
        for obj in objects.values():
            if obj.get("zoneId") not in battlefield_zone_ids:
                continue
            controller = obj.get("controllerSeatId") or obj.get("ownerSeatId")
            if controller != seat:
                continue
            name = card_label_for_snapshot(obj.get("instanceId"))
            if name and name != "unknown card":
                names.add(name)
        return names

    def card_resource_name(entry, suffix=None):
        """Render one available-resource card, optionally with a caveat suffix."""
        if suffix:
            return f"{entry['name']} [{suffix}]"
        return entry["name"]

    def command_zone_names(seat):
        """List command-zone cards, adding commander tax only when nonzero."""
        lines = []
        for entry in zone_object_entries("ZoneType_Command", seat):
            obj = entry["object"]
            grp_id = obj.get("grpId") or obj.get("objectSourceGrpId")
            try:
                count = commander_cast_counts.get((seat, int(grp_id)), 0)
            except (TypeError, ValueError):
                count = 0
            tax = count * 2
            if tax:
                lines.append(f"{entry['name']} [next commander tax +{tax}]")
            else:
                lines.append(entry["name"])
        return sorted(set(lines))

    def available_resources_for_seat(seat):
        """Find high-confidence cards playable or usable outside the hand."""
        resources = defaultdict(list)
        graveyard_entries = zone_object_entries("ZoneType_Graveyard", seat)
        exile_entries = zone_object_entries("ZoneType_Exile", seat)

        for zone_name, entries in (
            ("graveyard", graveyard_entries),
            ("exile", exile_entries),
        ):
            for entry in entries:
                mechanics = resource_mechanics_for_zone(
                    list((entry["metadata"] or {}).get("play_mechanics") or []),
                    zone_name,
                )
                if not mechanics:
                    continue
                resources["potential_graveyard_exile_plays"].append(
                    card_resource_name(
                        entry,
                        f"{', '.join(mechanics)} from {zone_name}, cost not checked",
                    )
                )

        return {key: sorted(set(values)) for key, values in resources.items() if values}

    def permanent_label_for_snapshot(instance_id):
        """Return a battlefield label with counters and known attachments."""
        base = card_label_for_snapshot(instance_id)
        if base == "unknown card":
            return base
        obj = objects.get(instance_id, {})
        modifier_parts = counter_summary_parts(object_counters[instance_id], counter_type_names)
        modifier_parts.extend(attachment_summary_parts(attachments_for_permanent(instance_id)))
        controller = obj.get("controllerSeatId")
        owner = obj.get("ownerSeatId")
        if controller is not None and owner is not None:
            # Control-changing effects keep ownerSeatId and controllerSeatId
            # separate. Board snapshots should expose that strategic context.
            ownership_part = ownership_summary_part(owner_label(controller), owner_label(owner))
            if ownership_part:
                modifier_parts.append(ownership_part)
        return f"{base}{modifier_summary_suffix(modifier_parts)}"

    def board_permanent_label(obj):
        """Return a board label with transient visible state such as summoning sickness."""
        label = permanent_label_for_snapshot(obj.get("instanceId"))
        if obj.get("hasSummoningSickness"):
            label = f"{label} (summoning sick)"
        return label

    def attachments_for_permanent(instance_id):
        """Collect auras/equipment explicitly attached to this permanent."""
        attachment_names_by_kind = defaultdict(list)
        for ann in persistent_annotations.values():
            if "AnnotationType_Attachment" not in set(ann.get("type") or []):
                continue
            if instance_id not in (ann.get("affectedIds") or []):
                continue
            attachment_id = ann.get("affectorId")
            attachment = objects.get(attachment_id, {})
            name = card_label_for_snapshot(attachment_id)
            if not name or name == "unknown card":
                continue
            subtypes = set(attachment.get("subtypes") or [])
            # The annotation tells us the relationship; the subtype tells us
            # how to word it without guessing from the card name.
            if "SubType_Aura" in subtypes:
                kind = "aura"
            elif "SubType_Equipment" in subtypes:
                kind = "equipment"
            else:
                kind = "other"
            attachment_names_by_kind[kind].append(name)
        return attachment_names_by_kind

    def battlefield_row_for_object(obj):
        """Classify a permanent into the board row used in turn-state blocks."""
        card_types = set(obj.get("cardTypes") or [])
        # Lands get the first row even when a land is temporarily also a
        # creature, because land count/mana is usually the first board read.
        if "CardType_Land" in card_types:
            return "lands"
        # Do not treat every token-like object as a creature. Arena can expose
        # copied permanents or Aura-like objects with sparse type data, and
        # those should not be promoted into the creature row without the enum.
        if "CardType_Creature" in card_types:
            return "creatures"
        if card_types & {"CardType_Artifact", "CardType_Enchantment"}:
            return "artifacts_enchantments"
        return "other"

    def battlefield_rows(seat):
        """Collect battlefield permanents into stable, readable board rows."""
        battlefield_zone_ids = zone_ids_by_type("ZoneType_Battlefield")
        rows = {
            "lands": {"untapped": [], "tapped": []},
            "artifacts_enchantments": {"untapped": [], "tapped": []},
            "creatures": {"untapped": [], "tapped": []},
            "other": {"untapped": [], "tapped": []},
        }
        for obj in objects.values():
            if obj.get("zoneId") not in battlefield_zone_ids:
                continue
            if obj.get("type") not in {"GameObjectType_Card", "GameObjectType_Token"}:
                continue
            controller = obj.get("controllerSeatId") or obj.get("ownerSeatId")
            if controller == seat:
                tapped_key = "tapped" if obj.get("isTapped") else "untapped"
                rows[battlefield_row_for_object(obj)][tapped_key].append(
                    board_permanent_label(obj)
                )
        return rows

    def board_row_line(label, row):
        """Render one board row, grouping permanents by tapped state."""
        parts = []
        if row["untapped"]:
            parts.append(f"Untapped: {compact_names(row['untapped'])}")
        if row["tapped"]:
            parts.append(f"Tapped: {compact_names(row['tapped'])}")
        return f"  {label}: {'; '.join(parts) if parts else '(empty)'}"

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
        nonlocal suppress_phase_heading
        if not show_turn_state:
            return
        was_suppressed = suppress_phase_heading
        suppress_phase_heading = True
        for seat in turn_state_seat_order():
            emit_player_state(seat, owner_label(seat))
        if active_effects:
            emit("Active Effects:")
            for effect in sorted(active_effects.values(), key=lambda item: item["text"]):
                emit(f"  {effect['text']}")
        state_lines = strategic_state_lines()
        if state_lines:
            emit("Current State:")
            for line in state_lines:
                emit(f"  {line}")
        suppress_phase_heading = was_suppressed

    def emit_player_state(seat, label):
        """Print all visible zones for one player in a compact block."""
        emit(f"{state_zone_label(label, 'hand')}: {compact_names(hand_names(seat))}")
        emit(f"{state_player_heading(label)}:")
        emit_board_rows(seat)
        emit(f"  {phrase_library_count('Library', library_count(seat))}")
        if current_game_has_commanders:
            emit(f"  Command: {compact_names(command_zone_names(seat))}")
        emit(f"  Graveyard: {compact_names(zone_names('ZoneType_Graveyard', seat))}")
        emit(f"  Exile: {compact_names(zone_names('ZoneType_Exile', seat))}")
        for line in available_resource_lines(available_resources_for_seat(seat)):
            emit(f"  {line}")

    def emit_board_rows(seat):
        """Print board rows in the requested land/noncreature/creature order."""
        rows = battlefield_rows(seat)
        emit(board_row_line("Lands", rows["lands"]))
        emit(board_row_line("Artifacts/Enchantments", rows["artifacts_enchantments"]))
        emit(board_row_line("Creatures", rows["creatures"]))
        if rows["other"]["untapped"] or rows["other"]["tapped"]:
            emit(board_row_line("Other", rows["other"]))

    def turn_state_seat_order():
        """Prefer the user's side first once the local seat is known."""
        if known_local_seat in (1, 2):
            return [known_local_seat, 1 if known_local_seat == 2 else 2]
        return [1, 2]

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
                    f"{object_pronoun(owner_label(target_seat))}"
                )
        return lines

    def flush_pending_stack_cast(instance_id):
        """Emit a delayed speculative cast once Arena confirms it mattered."""
        pending = pending_stack_casts.pop(instance_id, None)
        if pending:
            emitted_cast_instance_ids.add(instance_id)
            text = cast_text_with_targets(pending.get("source_id", instance_id), pending["text"])
            emit(phrase_player_action(pending["owner"], "cast", text))

    def flush_all_pending_stack_casts():
        """Emit delayed cast lines before a game ends without resolving them."""
        for instance_id in list(pending_stack_casts):
            flush_pending_stack_cast(instance_id)

    def infer_missing_cast_for_instance(instance_id):
        """Emit a cast line for named spells whose CastSpell event was hidden."""
        obj = objects.get(instance_id)
        name = card_label(instance_id)
        if not should_infer_missing_cast_before_resolve(
            name,
            instance_id,
            obj,
            emitted_cast_instance_ids,
        ):
            return
        # Some effects from face-down exile report the spell's sacrifice,
        # destroy, or death annotations before the later Resolve annotation.
        # Emit the missing cast as soon as that spell acts as the affector.
        actor_seat = object_controller(instance_id) or object_owner(instance_id)
        if not actor_seat:
            return
        actor = owner_label(actor_seat)
        emitted_cast_instance_ids.add(instance_id)
        emit(phrase_player_action(actor, "cast", name))

    def flush_pending_cast_for_affector(affector_id):
        """Emit delayed source casts before their triggered effects are narrated."""
        flush_pending_stack_cast(affector_id)
        infer_missing_cast_for_instance(affector_id)
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
            infer_missing_cast_for_instance(pending_id)

    def flush_pending_cast_for_copy_name(name):
        """Emit a delayed original spell before its copied spell resolves."""
        matching_ids = [
            pending_id
            for pending_id, pending in pending_stack_casts.items()
            if base_cast_name(pending.get("text", "")) == name
        ]
        if len(matching_ids) != 1:
            return
        # Arena can report Copy before the original spell's later Resolve.
        # When exactly one delayed cast has the copied spell's name, emitting it
        # here keeps "I cast X" ahead of "A copy of X resolves" without guessing
        # among multiple same-name spells.
        flush_pending_stack_cast(matching_ids[0])

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

    def flush_pending_life_group():
        """Emit grouped repeated ability life changes with final totals."""
        nonlocal pending_life_groups
        if not pending_life_groups:
            return
        groups = list(pending_life_groups.values())
        last_group_index_by_owner = {}
        for index, group in enumerate(groups):
            # Interleaved drains can affect the same player from several sources.
            # Showing a running total on every grouped source line makes the math
            # look independent, so keep totals only on that player's final line.
            last_group_index_by_owner[group["owner"]] = index
        for index, group in enumerate(groups):
            total = group["total"] if last_group_index_by_owner[group["owner"]] == index else None
            emit_in_phase_section(
                phrase_life_change_summary(
                    group["source"],
                    group["owner"],
                    group["delta"],
                    group["count"],
                    total,
                ),
                group.get("section"),
            )
        pending_life_groups = {}

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
        flush_pending_life_group()
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

    def add_pending_life_change(source, owner, delta, total):
        """Group repeated ability life changes while keeping the final total."""
        key = (source, owner, delta)
        if key in pending_life_groups:
            pending_life_groups[key]["count"] += 1
            # Running life totals differ on every annotation; the final one is
            # the strategic value readers need in the grouped transcript line.
            pending_life_groups[key]["total"] = total
        else:
            # Several triggers can alternate in one event burst, e.g. Ayara
            # draining and Vito seeing the life gain. Keep separate buckets until
            # an unrelated event flushes the whole burst.
            pending_life_groups[key] = {
                "source": source,
                "owner": owner,
                "delta": delta,
                "count": 1,
                "total": total,
                "section": current_phase_section,
            }
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
            # Cards cast or played from exile can be owned by one player and
            # controlled by another. Prefer controller for the actor so Gonti
            # and similar effects do not attribute stolen-card casts to the
            # original owner.
            actor = owner_label(object_controller(iid)) if object_controller(iid) else owner
            emit(phrase_player_action(actor, "play", name))
        elif category == "CastSpell":
            flush_pending_event_groups()
            pending_stack_casts.pop(iid, None)
            stack_display_names[iid] = name
            suffix = " from command zone" if from_command else ""
            if from_command:
                commander_text = note_commander_cast(iid, details.get("grpid"))
                if commander_text:
                    suffix = f"{suffix}; {commander_text}"
            cast_base_text = f"{name}{suffix}"
            cast_text = cast_text_with_targets(iid, cast_base_text)
            # See PlayLand above: the caster is the current controller, not
            # necessarily the owner of the source zone or card.
            owner = owner_label(object_controller(iid)) if object_controller(iid) else owner
            if is_low_fidelity_update_without_turn(gsm) or cast_text == cast_base_text:
                # TargetSpec annotations can arrive just after CastSpell in
                # the same stack sequence. Delay targetless cast lines until
                # the spell resolves or produces an effect, then re-check
                # TargetSpec so cards like Fading Hope can name their target.
                pending_stack_casts[iid] = {
                    "owner": owner,
                    "text": cast_base_text,
                    "source_id": iid,
                }
            else:
                emitted_cast_instance_ids.add(iid)
                emit(phrase_player_action(owner, "cast", cast_text))
        elif category == "Resolve":
            flush_pending_event_groups()
            name = resolve_stack_name(iid, name, stack_display_names)
            flush_pending_stack_cast(iid)
            if should_infer_missing_cast_before_resolve(
                name,
                iid,
                objects.get(iid),
                emitted_cast_instance_ids,
            ):
                # Some face-down exile effects reveal the spell only on the
                # resolving stack object. Emit a conservative cast line here so
                # named spells do not appear to resolve from nowhere.
                actor = owner_label(object_controller(iid)) if object_controller(iid) else owner
                emitted_cast_instance_ids.add(iid)
                emit(phrase_player_action(actor, "cast", cast_text_with_targets(iid, name)))
            effect_text = active_effect_for_resolved_permanent(name, owner)
            if effect_text:
                add_active_effect(("resolved_effect", iid, name), effect_text, source_id=iid)
            if show_resolves and should_emit_resolve_line(name, iid):
                # Anonymous stack objects are usually triggered/copy ability
                # bookkeeping. Emitting raw ids is less useful than silence.
                emit(f"{name} resolves")
        elif category == "Copy":
            flush_pending_event_groups()
            flush_pending_cast_for_copy_name(name)
            # Jin-Gitaxias and similar effects put a copy on the stack. Track
            # that identity so later effects do not look like the original
            # countered spell still resolved.
            copy_name = copied_object_label(name, True)
            if copy_name:
                stack_display_names[iid] = copy_name
                remembered_object_labels[iid] = copy_name
        elif category in DEATH_CATEGORIES:
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
            flush_pending_cast_for_affector(ann.get("affectorId"))
            emit(phrase_player_action(owner, "sacrifice", name))
            remove_active_effects_for_source(iid)
        elif category == "Mill":
            source = mill_source_from_affector(ann.get("affectorId"))
            mill_owner = owner_label(zone_owner(details.get("zone_src")) or object_owner(iid))
            if not add_pending_mill(source, mill_owner):
                flush_pending_event_groups()

    def emit_life_change(ann, gsm):
        """Emit life gain/loss lines from ModifiedLife annotations."""
        details = detail_dict(ann.get("details"))
        delta = details.get("life")
        affected = ann.get("affectedIds") or []
        if not affected or not isinstance(delta, int) or delta == 0:
            return

        seat = affected[0]
        game_state_id = gsm.get("gameStateId")

        def life_key(life_ann, life_seat, life_delta):
            return (current_match, game_state_id, life_ann.get("id"), life_seat, life_delta)

        key = life_key(ann, seat, delta)
        if key in seen_life_changes:
            return

        total = players.get(seat, {}).get("lifeTotal")
        source = source_label(ann.get("affectorId"))
        if not should_group_life_change_source(source):
            sign = 1 if delta > 0 else -1
            batch = []
            for other in gsm.get("annotations", []):
                if "AnnotationType_ModifiedLife" not in (other.get("type") or []):
                    continue
                other_affected = other.get("affectedIds") or []
                other_delta = detail_dict(other.get("details")).get("life")
                if (
                    not other_affected
                    or other_affected[0] != seat
                    or not isinstance(other_delta, int)
                    or other_delta == 0
                    or (1 if other_delta > 0 else -1) != sign
                    or should_group_life_change_source(source_label(other.get("affectorId")))
                ):
                    continue
                batch.append((other, other_delta))
            if len(batch) > 1:
                flush_pending_event_groups()
                for other, other_delta in batch:
                    seen_life_changes.add(life_key(other, seat, other_delta))
                emit(phrase_life_change(owner_label(seat), sum(other_delta for _, other_delta in batch), total))
                return

        seen_life_changes.add(key)
        if should_group_life_change_source(source) or pending_life_groups:
            add_pending_life_change(life_change_group_source(source), owner_label(seat), delta, total)
        else:
            flush_pending_event_groups()
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
        domain_text = CHOICE_DOMAIN_NAMES.get(domain, f"choice domain {domain}")
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
        turn_info = gsm.get("turnInfo") or current_turn_info
        if turn_info.get("phase") != "Phase_Combat":
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
                    target = confident_combat_target_label(target_id)
                    emit(
                        phrase_player_action(
                            owner_label(obj.get("controllerSeatId")),
                            "attack",
                            attack_phrase(target, card_label(iid)),
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

    def emit_enters_attacking_events(gsm):
        """Emit zone-transfer effects that put a creature directly into combat."""
        if gsm.get("turnInfo", {}).get("phase") != "Phase_Combat":
            return
        for ann in gsm.get("annotations") or []:
            if "AnnotationType_ZoneTransfer" not in set(ann.get("type") or []):
                continue
            details = detail_dict(ann.get("details"))
            if not is_battlefield_zone(details.get("zone_dest")):
                continue
            if is_battlefield_zone(details.get("zone_src")):
                continue
            for iid in ann.get("affectedIds") or []:
                obj = objects.get(iid)
                if not object_is_attacking_creature(obj):
                    continue
                key = (current_match, current_turn, gsm.get("gameStateId"), iid)
                if key in seen_enters_attacking:
                    continue
                seen_enters_attacking.add(key)
                flush_pending_event_groups()
                emit(phrase_enters_attacking(source_label(ann.get("affectorId")), card_label(iid)))

    def emit_continuous_effect_events(gsm):
        """Emit major state-changing continuous effects Arena exposes directly."""
        annotations = gsm.get("annotations") or []
        if not annotations:
            return

        phased_out_counts = Counter()
        phased_out_sources = {}
        for ann in annotations:
            if "AnnotationType_PhasedOut" not in set(ann.get("type") or []):
                continue
            source_id = ann.get("affectorId")
            for affected_id in ann.get("affectedIds") or []:
                owner = object_owner(affected_id)
                if owner is None:
                    continue
                phased_out_counts[owner] += 1
                phased_out_sources.setdefault(owner, source_id)

        for seat, count in sorted(phased_out_counts.items()):
            source = source_label(phased_out_sources.get(seat))
            label = owner_label(seat)
            key = (current_match, gsm.get("gameStateId"), "phase_out", seat)
            if key in seen_state_events:
                continue
            seen_state_events.add(key)
            flush_pending_event_groups()
            plural = "permanent" if count == 1 else "permanents"
            suffix = f" via {source}" if source else ""
            emit(
                phrase_player_action(
                    label,
                    "phase",
                    f"out {count} {plural}{suffix}",
                    third_person="phases",
                )
            )

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
        if not object_has_ability(source_id, INFECT_ABILITY_GRP_IDS):
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

    def emit_counter_change(ann, gsm):
        """Track counters on players and object instances for state summaries."""
        details = detail_dict(ann.get("details"))
        affected = ann.get("affectedIds") or []
        if not affected:
            return

        raw_type = details.get("counter_type")
        amount = details.get("transaction_amount")
        if not isinstance(amount, int):
            return
        if "AnnotationType_CounterRemoved" in set(ann.get("type") or []):
            amount = -amount

        affected_id = affected[0]
        if affected_id in (1, 2):
            counter_name = PLAYER_COUNTER_NAMES.get(raw_type)
            if not counter_name:
                return
            player_counters[(affected_id, counter_name)] += amount
            total = player_counters[(affected_id, counter_name)]
            flush_pending_event_groups()
            emit(phrase_player_counter_change(owner_label(affected_id), counter_name, amount, total))
            return

        # Object counters affect battlefield grouping. We do not emit a line for
        # every +1/+1 counter because those logs are very noisy; the turn-state
        # board snapshot shows the resulting piles.
        object_counters[affected_id][raw_type] += amount
        if object_counters[affected_id][raw_type] <= 0:
            del object_counters[affected_id][raw_type]

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

        for ann_id in gsm.get("diffDeletedPersistentAnnotationIds") or []:
            persistent_annotations.pop(ann_id, None)

        for ann in gsm.get("persistentAnnotations") or []:
            ann_id = ann.get("id")
            if ann_id is not None:
                # Arena stores attachments and ongoing effects as persistent
                # annotations. Keeping them by id lets board-state snapshots
                # survive later diff messages until Arena deletes them.
                persistent_annotations[ann_id] = ann
            if "AnnotationType_TargetSpec" in set(ann.get("type") or []):
                source_id = ann.get("affectorId")
                if source_id is not None:
                    ability_id = detail_dict(ann.get("details")).get("abilityGrpId")
                    if (
                        isinstance(ability_id, int)
                        and ability_id not in known_target_ability_ids_by_source[source_id]
                    ):
                        known_target_ability_ids_by_source[source_id].append(ability_id)
                    # TargetSpec can disappear before the later Resolve
                    # annotation. Remember the selected ids by stack object so
                    # delayed cast lines can still report them.
                    for target_id in ann.get("affectedIds") or []:
                        if target_id != source_id and target_id not in known_targets_by_source[source_id]:
                            known_targets_by_source[source_id].append(target_id)
                        name = confident_target_name(target_id)
                        if name and name not in known_target_names_by_source[source_id]:
                            known_target_names_by_source[source_id].append(name)

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
            flush_all_pending_stack_casts()
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

    def handle_annotation(ann, gsm, msg):
        """Deduplicate one Arena annotation and route it to the right handler."""
        if debug_annotations:
            record_debug(ann)

        key = annotation_key(ann, gsm, msg)
        if key in seen_annotations:
            return
        seen_annotations.add(key)

        ann_types = set(ann.get("type") or [])
        if "AnnotationType_ObjectIdChanged" in ann_types:
            note_object_id_change(ann)
        elif "AnnotationType_AbilityInstanceCreated" in ann_types:
            flush_pending_cast_for_affector(ann.get("affectorId"))
            is_triggered_ability = not action_activates_source(gsm, ann.get("affectorId"))
            for affected_id in ann.get("affectedIds") or []:
                if is_triggered_ability:
                    ability_trigger_ids.add(affected_id)
                obj = objects.get(affected_id)
                if not obj:
                    continue
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
            emit_counter_change(ann, gsm)

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

    def target_debug_annotations(gsm):
        """Summarize target-related annotations for --debug-targets output."""
        entries = []
        for ann in (gsm.get("annotations") or []) + (gsm.get("persistentAnnotations") or []):
            ann_types = set(ann.get("type") or [])
            if not any("Target" in ann_type for ann_type in ann_types):
                continue
            source_id = ann.get("affectorId")
            target_ids = list(ann.get("affectedIds") or [])
            entries.append(
                {
                    "annotationId": ann.get("id"),
                    "types": ann.get("type"),
                    "sourceId": source_id,
                    "sourceGrpId": (objects.get(source_id) or {}).get("grpId"),
                    "sourceName": card_label(source_id) if source_id is not None else None,
                    "targetIds": target_ids,
                    "targetNames": [
                        confident_target_name(target_id) or f"instance {target_id}"
                        for target_id in target_ids
                    ],
                    "details": detail_dict(ann.get("details")),
                }
            )
        return entries

    def contains_target_marker(value):
        """Detect gameplay payloads worth printing in --debug-targets mode."""
        if isinstance(value, dict):
            for key, item in value.items():
                if "target" in str(key).casefold():
                    return True
                if contains_target_marker(item):
                    return True
        elif isinstance(value, list):
            return any(contains_target_marker(item) for item in value)
        elif isinstance(value, str):
            return "target" in value.casefold()
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
        if debug_targets and contains_target_marker(payload):
            event["target_key_paths"] = find_target_like_paths(payload)
            event["target_annotations"] = target_debug_annotations(gsm)
            current_match_record["target_events"].append(event)
        if debug_triggers:
            trigger_annotations = trigger_debug_annotations(gsm)
            if trigger_annotations:
                event["trigger_annotations"] = trigger_annotations
                current_match_record["trigger_events"].append(event)

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

    try:
        for log_path in log_paths:
            with log_path.open("rb") as f:
                if live:
                    # Live mode starts with the current game, meaning the last match
                    # Arena has started in Player.log, then behaves like tail -f.
                    live_start, live_game_number = find_last_game_start(player_log)
                    f.seek(live_start)
                    read_bytes = live_start
                    current_match_number = max(0, live_game_number - 1)

                while True:
                    raw_line = f.readline()
                    if not raw_line:
                        if live:
                            time.sleep(0.25)
                            continue
                        break

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
                            if match_id in seen_match_ids:
                                finalize_current_match()
                                current_match = match_id
                                current_match_lines = None
                                current_match_record = None
                                skipping_duplicate_match = True
                            else:
                                start_match(match_id, gsm)

                        if skipping_duplicate_match:
                            continue

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
                        if turn_info:
                            current_turn_info = turn_info
                        if "turnNumber" in turn_info and turn_info["turnNumber"] != current_turn:
                            flush_pending_event_groups()
                            current_turn = turn_info["turnNumber"]
                            current_phase_section = None
                            emitted_phase_section = None
                            emit("")
                            emit(
                                f"=== Turn {current_turn}: "
                                f"{owner_label(turn_info.get('activePlayer'))} ==="
                            )
                            emit_turn_state()
                        if turn_info:
                            current_phase_section = phase_section_label(turn_info)

                        event_index += 1
                        record_gameplay_event(msg, gsm)
                        emit_continuous_effect_events(gsm)
                        emit_enters_attacking_events(gsm)
                        emit_combat_events(gsm)

                        for ann in gsm.get("annotations", []):
                            handle_annotation(ann, gsm, msg)

                        emit_match_results(gsm)

                    if not root.get("greToClientEvent"):
                        mark_postgame_payload(root)
    except KeyboardInterrupt:
        if not live:
            raise

    finalize_current_match()

    if show_progress:
        render_progress(force=True)
        print(file=sys.stderr)

    if live:
        return

    if debug_annotations:
        print("\nAnnotation summary:", file=sys.stderr)
        for (types, category), count in debug_counts.most_common():
            type_text = ",".join(types) if types else "<none>"
            cat_text = f" category={category!r}" if category else ""
            sample = json.dumps(debug_samples[(types, category)], sort_keys=True)
            print(f"{count:5d} {type_text}{cat_text} sample={sample}", file=sys.stderr)

    if archive_db_path:
        inserted, updated = archive_seen_games(archive_db_path, transcript_matches, log_paths)
        print(
            f"Archived {inserted} new and {updated} existing game(s) to {archive_db_path.expanduser()}",
            file=sys.stderr,
        )
        transcript_matches = archived_transcript_matches(archive_db_path)

    selected_matches, selection_warning = select_transcript_matches(
        transcript_matches,
        nth_from_start=nth_from_start,
        last_games=last_games,
        first_games=first_games,
        nth_from_end=nth_from_end,
        game_range=game_range,
    )
    if selection_warning:
        print(selection_warning, file=sys.stderr)

    first = True
    for match in selected_matches:
        if not first:
            print()
            print()
        first = False
        lines = cleaned_transcript_lines(match["lines"])
        print(
            "\n".join(
                colorize_transcript_line(
                    line,
                    color_enabled,
                    card_name_colors,
                    card_name_pattern,
                )
                for line in lines
            )
        )

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

    if debug_targets:
        print("\nDebug target-like GRE/gameState events:", file=sys.stderr)
        for match in selected_matches:
            print(
                f"\n===== TARGETS GAME {match['number']}: MATCH {match['match_id']} =====",
                file=sys.stderr,
            )
            if not match["target_events"]:
                print("No target-like payloads found.", file=sys.stderr)
                continue
            for event in match["target_events"]:
                print(
                    f"\n### event {event['event_index']} "
                    f"turn={event['turn']} gameStateId={event['gameStateId']}",
                    file=sys.stderr,
                )
                print(
                    "Target key paths: "
                    + ", ".join(event.get("target_key_paths", [])[:40]),
                    file=sys.stderr,
                )
                print(
                    "Target annotations: "
                    + json.dumps(event.get("target_annotations", []), sort_keys=True),
                    file=sys.stderr,
                )
                print(
                    json.dumps(event["payload"], indent=2, sort_keys=True),
                    file=sys.stderr,
                )

    if debug_triggers:
        print("\nDebug trigger-like GRE/gameState events:", file=sys.stderr)
        for match in selected_matches:
            print(
                f"\n===== TRIGGERS GAME {match['number']}: MATCH {match['match_id']} =====",
                file=sys.stderr,
            )
            if not match["trigger_events"]:
                print("No trigger-like payloads found.", file=sys.stderr)
                continue
            for event in match["trigger_events"]:
                print(
                    f"\n### event {event['event_index']} "
                    f"turn={event['turn']} gameStateId={event['gameStateId']}",
                    file=sys.stderr,
                )
                print(
                    json.dumps(event.get("trigger_annotations", []), indent=2, sort_keys=True),
                    file=sys.stderr,
                )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Extract readable MTG Arena game transcripts from Player.log. "
            "If paths are not provided, the script checks LOG/CARDDB "
            "environment variables and common local MTG Arena locations."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  Print the most recent game:
    python3 mtga_extract_games.py --last 1 --no-resolves

  Print every game in Player.log:
    python3 mtga_extract_games.py --all --no-resolves

  Save the last three games to a text file:
    python3 mtga_extract_games.py --last 3 --no-resolves > mtga_transcript.txt

  Print only game 4 from the log:
    python3 mtga_extract_games.py --nth-from-start 4 --no-resolves

  Print the next-to-last game:
    python3 mtga_extract_games.py --nth-from-end 2 --no-resolves

  Print games 3 through 5:
    python3 mtga_extract_games.py --range 3 5 --no-resolves

  Debug where Arena records a card's choices:
    python3 mtga_extract_games.py --last 1 --debug-card "Serra's Emissary"

  Watch for new games as Arena writes Player.log:
    python3 mtga_extract_games.py --live --no-resolves

  Highlight my lines and opponent lines in a terminal:
    python3 mtga_extract_games.py --last 1 --colour always

macOS path examples:
  LOG="$HOME/Library/Logs/Wizards Of The Coast/MTGA/Player.log"
  CARDDB="$HOME/Library/Application Support/com.wizards.mtga/Downloads/Raw/Raw_CardDatabase_....mtga"

If paths are not provided, the script checks LOG/CARDDB environment variables,
then common local MTG Arena locations.

No pip install step is required; this script only uses Python's standard library.
""",
    )
    parser.add_argument(
        "player_log",
        nargs="?",
        type=Path,
        help="optional path to MTG Arena's Player.log file",
    )
    parser.add_argument(
        "carddb",
        nargs="?",
        type=Path,
        help="optional path to Raw_CardDatabase_*.mtga from Arena's Downloads/Raw folder",
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
    parser.add_argument(
        "--debug-targets",
        action="store_true",
        help="advanced: print game events containing likely spell/ability target fields",
    )
    parser.add_argument(
        "--debug-triggers",
        action="store_true",
        help="advanced: print compact snippets for trigger-like gameplay events",
    )
    selection_group = parser.add_mutually_exclusive_group()
    selection_group.add_argument(
        "--all",
        action="store_true",
        help="output every game in the log; this is also the default when no selector is used",
    )
    selection_group.add_argument(
        "--nth-from-start",
        "--select",
        dest="select",
        type=int,
        metavar="N",
        help="output only game N, counting from the start of the log; --select is an alias",
    )
    selection_group.add_argument(
        "--nth-from-end",
        type=int,
        metavar="N",
        help="output only game N, counting backward from the end; 1 is the latest game",
    )
    selection_group.add_argument(
        "--range",
        type=int,
        nargs=2,
        metavar=("X", "Y"),
        help="output games X through Y, inclusive, counting from the start of the log",
    )
    selection_group.add_argument(
        "--first",
        type=int,
        metavar="N",
        help="output only the first N games",
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
    parser.add_argument(
        "--no-phases",
        action="store_true",
        help="hide phase/step headings in the transcript",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="print the current game from its start, then watch Player.log for new lines",
    )
    parser.add_argument(
        "--colour",
        "--color",
        dest="colour",
        choices=("never", "auto", "always"),
        default="never",
        help="colour transcript structure and clearly attributed Me/Opponent lines; default: never",
    )
    parser.add_argument(
        "--archive-db",
        nargs="?",
        const=default_archive_db_path(),
        default=default_archive_db_path(),
        type=Path,
        metavar="PATH",
        help=(
            "save parsed games to a SQLite archive keyed by match id; "
            "default path when no PATH is given: ./mtga_seen_games.sqlite3"
        ),
    )
    parser.add_argument(
        "--no-archive-db",
        action="store_true",
        help="read and print directly from the available logs without updating the archive",
    )
    args = parser.parse_args()

    try:
        player_log, carddb, path_warning = resolve_input_paths(
            args.player_log,
            args.carddb,
            live=args.live,
        )
    except FileNotFoundError as exc:
        parser.exit(2, f"{exc}\n")
    if path_warning:
        print(path_warning, file=sys.stderr)

    if args.select is not None and args.select < 1:
        parser.error("--nth-from-start must be 1 or greater")
    if args.nth_from_end is not None and args.nth_from_end < 1:
        parser.error("--nth-from-end must be 1 or greater")
    if args.first is not None and args.first < 1:
        parser.error("--first must be 1 or greater")
    if args.last is not None and args.last < 1:
        parser.error("--last must be 1 or greater")
    if args.range is not None:
        range_start, range_end = args.range
        if range_start < 1 or range_end < 1:
            parser.error("--range values must be 1 or greater")
        if range_start > range_end:
            parser.error("--range X Y requires X to be less than or equal to Y")
    if args.live:
        if has_live_selection_conflict(args):
            parser.error("--live cannot be combined with game selection options")
        if args.progress:
            parser.error("--live cannot be combined with --progress")
    if args.no_archive_db:
        args.archive_db = None

    grp_to_name = load_grp_id_to_name(carddb)
    card_metadata = load_grp_id_to_metadata(carddb)
    ability_texts = load_ability_texts(carddb)
    enum_value_names = {
        # Player choice payloads for creature types use the same numeric values
        # as the card database SubType enum, which keeps this from becoming a
        # handwritten creature-type list.
        "SubType": load_enum_value_names(carddb, "SubType"),
        # Object counter annotations use CounterType values. Loading this enum
        # lets board-state snapshots say +1/+1 instead of raw counter ids.
        "CounterType": load_enum_value_names(carddb, "CounterType"),
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
        debug_targets=args.debug_targets,
        debug_triggers=args.debug_triggers,
        nth_from_start=args.select,
        last_games=args.last,
        first_games=args.first,
        nth_from_end=args.nth_from_end,
        game_range=tuple(args.range) if args.range is not None else None,
        show_progress=False if args.live else True if args.progress else False if args.no_progress else None,
        show_resolves=not args.no_resolves,
        show_turn_state=not args.no_turn_state,
        show_phases=not args.no_phases,
        live=args.live,
        enum_value_names=enum_value_names,
        card_metadata=card_metadata,
        ability_texts=ability_texts,
        color_mode=args.colour,
        archive_db_path=args.archive_db,
    )


if __name__ == "__main__":
    main()
