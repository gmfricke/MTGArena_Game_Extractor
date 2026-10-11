#!/usr/bin/env python3
"""Extract MTG Arena logs into readable transcripts.

The code is organized around the parser's data flow: the top section loads card
and path metadata, the middle sections format transcript text and maintain the
archive database, and `extract_game_plays` contains the stateful Game Rules
Engine event parser that turns Arena JSON updates into turn-by-turn game text.
The overall goal is to preserve games from rotating Arena logs and make them
easy for humans and other tools to review.
"""

import argparse
import hashlib
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
    6: {1: "White", 2: "Blue", 3: "Black", 4: "Red", 5: "Green", 6: "Colorless"},
    11: {1: "creature"},
    14: {0: "even", 1: "odd"},
}

# Arena SelectN keyword prompts use numeric ids that correspond to intrinsic
# ability ids in the card database. This fallback covers common keywords if a
# future database omits or renames the localization rows.
BASE_KEYWORD_VALUE_NAMES = {
    1: "deathtouch",
    2: "defender",
    3: "double strike",
    6: "first strike",
    7: "flash",
    8: "flying",
    9: "haste",
    10: "hexproof",
    12: "lifelink",
    13: "reach",
    14: "trample",
    15: "vigilance",
    21: "protection from",
    22: "shroud",
    29: "shadow",
    38: "fear",
    91: "infect",
    104: "indestructible",
    142: "menace",
    143: "skulk",
    211: "ward",
    264: "toxic",
}

# Keep board snapshots focused on abilities that materially affect casting,
# attacking, blocking, damage, or interaction. The CARDDB loader below finds
# their ids dynamically; this set controls which exposed ability records are
# concise enough to render beside a permanent.
IMPORTANT_ENGINE_KEYWORDS = {
    "annihilator",
    "bushido",
    "deathtouch",
    "defender",
    "double strike",
    "fear",
    "first strike",
    "flash",
    "flying",
    "haste",
    "hexproof",
    "horsemanship",
    "indestructible",
    "infect",
    "intimidate",
    "islandwalk",
    "landwalk",
    "lifelink",
    "menace",
    "mountainwalk",
    "plainswalk",
    "protection",
    "reach",
    "shadow",
    "shroud",
    "skulk",
    "swampwalk",
    "toxic",
    "trample",
    "vigilance",
    "ward",
    "wither",
}

# These keyword abilities may include a value, cost, quality, or player choice
# in the localized ability record (for example, "Ward—Pay 2 life" or
# "Protection from red").
PARAMETERIZED_ENGINE_KEYWORDS = {
    "annihilator",
    "bushido",
    "hexproof",
    "protection",
    "toxic",
    "ward",
}

# Observed on Skithiryx, the Blight Dragon. Arena records its combat damage to
# players as damage with markDamage=0 instead of ModifiedLife.
INFECT_ABILITY_GRP_IDS = {91}


def clear_all(*containers):
    """Clear several mutable containers that track per-game parser state."""
    for container in containers:
        container.clear()


def build_choice_value_names(
    subtype_names: dict[int, str],
    card_type_names: dict[int, str] | None = None,
) -> dict[int, dict[int, str]]:
    """Build lookup tables for numeric Arena choice values, including card database creature types."""
    values = {domain: dict(names) for domain, names in BASE_CHOICE_VALUE_NAMES.items()}
    if card_type_names:
        values[4] = dict(card_type_names)
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


RARITY_NAMES = {
    1: "basic",
    2: "common",
    3: "uncommon",
    4: "rare",
    5: "mythic",
}


def load_grp_id_to_printing_info(carddb_path: Path) -> dict[int, dict]:
    """Load compact printing metadata used when listing owned cards."""
    con = sqlite3.connect(carddb_path)
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    if not {"Cards", "Localizations_enUS"}.issubset(tables):
        con.close()
        return {}

    cur.execute("""
        SELECT
            c.GrpId,
            l.Loc,
            l.Formatted,
            c.ExpansionCode,
            c.CollectorNumber,
            c.Rarity,
            c.IsToken
        FROM Cards c
        JOIN Localizations_enUS l
          ON c.TitleId = l.LocId
        WHERE l.Formatted IN (0, 1)
        ORDER BY c.GrpId, l.Formatted
    """)
    metadata = {}
    for grp_id, name, _formatted, expansion, collector_number, rarity, is_token in cur.fetchall():
        grp_id = int(grp_id)
        if grp_id in metadata:
            continue
        metadata[grp_id] = {
            "name": name,
            "set": expansion or "",
            "collector": collector_number or "",
            "rarity": RARITY_NAMES.get(int(rarity), str(rarity)),
            "is_token": bool(is_token),
        }
    con.close()
    return metadata


def json_object_from_log_line(line: str) -> dict | None:
    """Parse the JSON object embedded in an Arena log line, if the line has one."""
    start = line.find("{")
    if start < 0:
        return None
    try:
        return json.loads(line[start:])
    except json.JSONDecodeError:
        return None


def int_like(value) -> int | None:
    """Convert Arena ids/counts to int while rejecting booleans and non-numeric text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def extract_card_count_from_value(value) -> int | None:
    """Extract an owned-card count from common Arena collection value shapes."""
    direct = int_like(value)
    if direct is not None:
        return direct
    if not isinstance(value, dict):
        return None
    for key in ("quantity", "Quantity", "count", "Count", "owned", "Owned", "cardCount", "CardCount"):
        count = int_like(value.get(key))
        if count is not None:
            return count
    return None


def card_counts_from_numeric_map(value) -> dict[int, int]:
    """Read a {grpId: count} style Arena collection map."""
    if not isinstance(value, dict):
        return {}
    counts = {}
    for raw_id, raw_count in value.items():
        grp_id = int_like(raw_id)
        count = extract_card_count_from_value(raw_count)
        if grp_id is None or count is None or count <= 0:
            continue
        counts[grp_id] = max(counts.get(grp_id, 0), count)
    return counts


def card_counts_from_object_list(value) -> dict[int, int]:
    """Read a list of Arena card-count objects when collection data is not a map."""
    if not isinstance(value, list):
        return {}
    counts = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        grp_id = None
        for key in ("grpId", "GrpId", "cardId", "CardId", "cardGrpId", "CardGrpId"):
            grp_id = int_like(item.get(key))
            if grp_id is not None:
                break
        count = extract_card_count_from_value(item)
        if grp_id is None or count is None or count <= 0:
            continue
        counts[grp_id] = max(counts.get(grp_id, 0), count)
    return counts


def is_collection_candidate_path(path: str, key: str, value) -> bool:
    """Return true for payload locations likely to contain owned collection counts."""
    blocked = (
        "Cosmetics", "Deck", "Decks", "Precon", "Formats", "Graph", "Courses",
        "Course", "CardPool", "PreferredCosmetics", "CardStyles",
    )
    if any(part in path for part in blocked):
        return False
    if key not in {
        "Cards",
        "PlayerCards",
        "PlayerCardCounts",
        "cardCounts",
        "CardCounts",
        "Collection",
        "collection",
    }:
        return False
    return bool(card_counts_from_numeric_map(value) or card_counts_from_object_list(value))


def owned_card_count_candidates(payload, path: str = "") -> list[dict]:
    """Find likely owned-card count maps inside an Arena JSON payload."""
    candidates = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            child_path = f"{path}/{key}"
            if is_collection_candidate_path(child_path, key, value):
                counts = card_counts_from_numeric_map(value) or card_counts_from_object_list(value)
                candidates.append({"path": child_path, "counts": counts})
            candidates.extend(owned_card_count_candidates(value, child_path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload[:20]):
            # Limiting list descent keeps cosmetic/deck dumps cheap while still
            # covering normal service-response wrappers around collection data.
            candidates.extend(owned_card_count_candidates(value, f"{path}[{index}]"))
    return candidates


def scan_owned_cards_from_logs(log_paths: list[Path]) -> dict | None:
    """Scan Arena logs and return the latest largest owned-card collection found."""
    best = None
    for log_path in log_paths:
        try:
            handle = log_path.open(errors="ignore")
        except OSError:
            continue
        with handle:
            for line_number, line in enumerate(handle, 1):
                root = json_object_from_log_line(line)
                if root is None:
                    continue
                for candidate in owned_card_count_candidates(root):
                    count_total = len(candidate["counts"])
                    if count_total == 0:
                        continue
                    if best is None or count_total >= len(best["counts"]):
                        best = {
                            "log_path": log_path,
                            "line_number": line_number,
                            "path": candidate["path"],
                            "counts": candidate["counts"],
                        }
    return best


def format_owned_card_line(grp_id: int, count: int, metadata: dict[int, dict]) -> str:
    """Format one owned-card line using card names resolved from the local CARDDB."""
    info = metadata.get(grp_id, {})
    name = info.get("name") or f"unknown grpId {grp_id}"
    set_code = info.get("set")
    collector = info.get("collector")
    rarity = info.get("rarity")
    set_text = f" ({set_code} {collector})" if set_code or collector else ""
    rarity_text = f" [{rarity}]" if rarity else ""
    return f"{count}x {name}{set_text}{rarity_text} [grpId {grp_id}]"


def print_owned_cards(log_paths: list[Path], carddb_path: Path) -> int:
    """Print all owned cards from the newest full collection payload in Arena logs."""
    found = scan_owned_cards_from_logs(log_paths)
    if not found:
        print(
            "No full owned-card collection was found in the parsed Arena logs.\n"
            "Arena sometimes logs only wildcard/inventory summaries. Open the Collection "
            "or Deck Builder in MTGA and run this command again if no card-count payload "
            "has been written yet.",
            file=sys.stderr,
        )
        return 1

    metadata = load_grp_id_to_printing_info(carddb_path)
    counts = found["counts"]
    print(f"Owned cards: {len(counts)} unique Arena grpIds")
    print(f"Source: {found['log_path']}:{found['line_number']} {found['path']}")
    print("")
    for grp_id, count in sorted(
        counts.items(),
        key=lambda item: ((metadata.get(item[0], {}).get("name") or "").casefold(), item[0]),
    ):
        print(format_owned_card_line(grp_id, count, metadata))
    return 0


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
    mana_cost_expr = "c.OldSchoolManaText" if "OldSchoolManaText" in card_columns else "''"
    mana_value_expr = "c.Order_CMCWithXLast" if "Order_CMCWithXLast" in card_columns else "NULL"
    power_expr = "c.Power" if "Power" in card_columns else "NULL"
    toughness_expr = "c.Toughness" if "Toughness" in card_columns else "NULL"

    cur.execute(f"""
        SELECT
            c.GrpId,
            l.Loc,
            l.Formatted,
            c.Types,
            c.AbilityIds,
            {colors_expr},
            {color_identity_expr},
            {frame_colors_expr},
            {mana_cost_expr},
            {mana_value_expr},
            {power_expr},
            {toughness_expr}
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
        mana_cost_text,
        mana_value,
        power_text,
        toughness_text,
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
                "mana_cost": mana_cost_text or "",
                "mana_value": mana_value,
                "power": str(power_text) if power_text is not None else None,
                "toughness": str(toughness_text) if toughness_text is not None else None,
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


def load_keyword_value_names(carddb_path: Path) -> dict[int, str]:
    """Load keyword-like ability ids from localized CARDDB ability records.

    Older databases marked the reusable keyword rows as intrinsic abilities.
    Current databases no longer do that consistently, but still classify the
    rows as category 3. Prefer either signal when available so engine-exposed
    abilities and StaticList_Keywords choices continue to decode across CARDDB
    versions.
    """
    con = sqlite3.connect(carddb_path)
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    if not {"Abilities", "Localizations_enUS"}.issubset(tables):
        con.close()
        return dict(BASE_KEYWORD_VALUE_NAMES)

    cur.execute("PRAGMA table_info(Abilities)")
    ability_columns = {row[1] for row in cur.fetchall()}
    classifiers = []
    if "Category" in ability_columns:
        classifiers.append("a.Category = 3")
    if "IsIntrinsicAbility" in ability_columns:
        classifiers.append("a.IsIntrinsicAbility = 1")
    where_classifier = f"AND ({' OR '.join(classifiers)})" if classifiers else ""

    cur.execute(
        f"""
        SELECT a.Id, l.Loc
        FROM Abilities a
        JOIN Localizations_enUS l
          ON a.TextId = l.LocId
        WHERE l.Formatted = 1
          AND l.Loc IS NOT NULL
          {where_classifier}
        """
    )
    mapping = dict(BASE_KEYWORD_VALUE_NAMES)
    for ability_id, name in cur.fetchall():
        clean_name = clean_localized_enum_name(name)
        if not clean_name:
            continue
        # Keyword choices are game terms rather than card names, so render them
        # lower-case in transcript sentences.
        mapping[int(ability_id)] = clean_name.rstrip(".").casefold()
    con.close()
    return mapping


WINDOWS_ENV_VARS = ("LOCALAPPDATA", "APPDATA", "USERPROFILE", "ProgramFiles", "ProgramFiles(x86)")


def path_exists(path: Path) -> bool:
    """Return whether a path exists, treating inaccessible candidates as missing."""
    try:
        return path.exists()
    except OSError:
        return False


def path_mtime(path: Path) -> float:
    """Return a path mtime, treating inaccessible candidates as oldest."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def safe_glob(path: Path, pattern: str) -> list[Path]:
    """Return glob matches, treating inaccessible directories as empty."""
    try:
        return list(path.glob(pattern))
    except OSError:
        return []


def safe_rglob(path: Path, pattern: str) -> list[Path]:
    """Return recursive glob matches, treating inaccessible directories as empty."""
    try:
        return list(path.rglob(pattern))
    except OSError:
        return []


def first_existing_path(candidates: list[Path]) -> Path | None:
    """Return the first candidate path that exists on this machine."""
    for candidate in candidates:
        expanded = candidate.expanduser()
        if path_exists(expanded):
            return expanded
    return None


def newest_existing_path(candidates: list[Path]) -> Path | None:
    """Return the newest existing path from a candidate list."""
    existing = [candidate.expanduser() for candidate in candidates if path_exists(candidate.expanduser())]
    if not existing:
        return None
    return max(existing, key=path_mtime)


def env_path(names: list[str]) -> Path | None:
    """Read the first non-empty environment variable from a list of names."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser()
    return None


def env_path_from_names(env: dict[str, str], names: list[str]) -> Path | None:
    """Read the first non-empty environment variable from a mapping."""
    for name in names:
        value = env.get(name)
        if value:
            return Path(value).expanduser()
    return None


def env_path_from(env: dict[str, str], name: str) -> Path | None:
    """Read one non-empty environment variable as a Path."""
    value = env.get(name)
    if not value:
        return None
    return Path(value).expanduser()


def operating_system_name(platform_name: str | None = None) -> str:
    """Return a compact operating system label for path discovery."""
    platform_name = platform_name or sys.platform
    if platform_name.startswith("win"):
        return "Windows"
    if platform_name == "darwin":
        return "macOS"
    return platform_name


def candidate_windows_paths(env: dict[str, str] | None = None) -> dict[str, list[Path]]:
    """Build Windows MTG Arena path candidates from standard environment variables."""
    env = env or os.environ
    local_appdata = env_path_from(env, "LOCALAPPDATA")
    appdata = env_path_from(env, "APPDATA")
    userprofile = env_path_from(env, "USERPROFILE")
    program_files = env_path_from(env, "ProgramFiles")
    program_files_x86 = env_path_from(env, "ProgramFiles(x86)")

    player_dirs = []
    if local_appdata:
        player_dirs.append(local_appdata.parent / "LocalLow" / "Wizards Of The Coast" / "MTGA")
    if appdata:
        player_dirs.append(appdata.parent / "LocalLow" / "Wizards Of The Coast" / "MTGA")
    if userprofile:
        player_dirs.append(
            userprofile / "AppData" / "LocalLow" / "Wizards Of The Coast" / "MTGA"
        )

    install_roots = []
    for root in (program_files, program_files_x86):
        if root:
            install_roots.extend(
                [
                    root / "Wizards of the Coast" / "MTGA",
                    root / "Wizards Of The Coast" / "MTGA",
                    root / "MTGA",
                ]
            )
    if local_appdata:
        install_roots.extend(
            [
                local_appdata / "Programs" / "Wizards of the Coast" / "MTGA",
                local_appdata / "Programs" / "Wizards Of The Coast" / "MTGA",
                local_appdata / "MTGA",
                local_appdata
                / "Packages"
                / "WizardsOfTheCoast.MTGA_8wekyb3d8bbwe"
                / "LocalCache"
                / "Local"
                / "Temp"
                / "Wizards Of The Coast"
                / "MTGA",
            ]
        )

    known_raw_dirs = []
    for root in install_roots:
        known_raw_dirs.append(root / "MTGA_Data" / "Downloads" / "Raw")
        known_raw_dirs.append(root / "Downloads" / "Raw")

    fallback_roots = []
    for root in install_roots:
        if root not in fallback_roots:
            fallback_roots.append(root)
    if local_appdata and local_appdata not in fallback_roots:
        fallback_roots.append(local_appdata)

    player_logs = []
    for player_dir in player_dirs:
        player_logs.append(player_dir / "Player.log")
        player_logs.append(player_dir / "Player-prev.log")

    return {
        "player_logs": dedupe_paths(player_logs),
        "carddb_known_dirs": dedupe_paths(known_raw_dirs),
        "carddb_recursive_roots": dedupe_paths(fallback_roots),
    }


def candidate_macos_paths() -> dict[str, list[Path]]:
    """Build macOS MTG Arena path candidates."""
    raw_dir = Path("~/Library/Application Support/com.wizards.mtga/Downloads/Raw")
    return {
        "player_logs": [
            Path("~/Library/Logs/Wizards Of The Coast/MTGA/Player.log"),
            Path("~/Library/Application Support/com.wizards.mtga/Logs/Player.log"),
        ],
        "carddb_known_dirs": [raw_dir],
        "carddb_recursive_roots": [],
    }


def dedupe_paths(paths: list[Path]) -> list[Path]:
    """Remove repeated path candidates while preserving order."""
    unique = []
    seen = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def record_checked_path(checked_paths: list[Path] | None, path: Path) -> None:
    """Append a path to diagnostics if diagnostics are being collected."""
    if checked_paths is not None:
        checked_paths.append(path.expanduser())


def newest_carddb_in_dir(raw_dir: Path, checked_paths: list[Path] | None = None) -> Path | None:
    """Return newest Raw_CardDatabase_*.mtga in one raw directory."""
    expanded = raw_dir.expanduser()
    record_checked_path(checked_paths, expanded)
    if not path_exists(expanded):
        return None
    candidates = safe_glob(expanded, "Raw_CardDatabase_*.mtga")
    if checked_paths is not None:
        checked_paths.extend(candidates)
    return newest_existing_path(candidates)


def newest_carddb_under_root(root: Path, checked_paths: list[Path] | None = None) -> Path | None:
    """Return newest Raw_CardDatabase_*.mtga below one recursive search root."""
    expanded = root.expanduser()
    record_checked_path(checked_paths, expanded)
    if not path_exists(expanded):
        return None
    candidates = safe_rglob(expanded, "Raw_CardDatabase_*.mtga")
    if checked_paths is not None:
        checked_paths.extend(candidates)
    return newest_existing_path(candidates)


def find_player_logs(
    platform_name: str | None = None,
    env: dict[str, str] | None = None,
    checked_paths: list[Path] | None = None,
) -> list[Path]:
    """Find existing MTG Arena Player logs for the current platform."""
    os_name = operating_system_name(platform_name)
    candidates = (
        candidate_windows_paths(env)["player_logs"]
        if os_name == "Windows"
        else candidate_macos_paths()["player_logs"]
    )
    existing = []
    for candidate in candidates:
        expanded = candidate.expanduser()
        record_checked_path(checked_paths, expanded)
        if path_exists(expanded):
            existing.append(expanded)
    return existing


def find_card_database(
    platform_name: str | None = None,
    env: dict[str, str] | None = None,
    checked_paths: list[Path] | None = None,
) -> Path | None:
    """Find the newest local Arena raw card database for the current platform."""
    os_name = operating_system_name(platform_name)
    path_sets = (
        candidate_windows_paths(env)
        if os_name == "Windows"
        else candidate_macos_paths()
    )

    known_matches = []
    for raw_dir in path_sets["carddb_known_dirs"]:
        found = newest_carddb_in_dir(raw_dir, checked_paths)
        if found:
            known_matches.append(found)
    if known_matches:
        return newest_existing_path(known_matches)

    recursive_matches = []
    for root in path_sets["carddb_recursive_roots"]:
        found = newest_carddb_under_root(root, checked_paths)
        if found:
            recursive_matches.append(found)
    return newest_existing_path(recursive_matches)


def likely_player_log_path() -> Path | None:
    """Find MTG Arena's Player.log in common local locations."""
    logs = find_player_logs()
    for log_path in logs:
        if log_path.name == "Player.log":
            return log_path
    return logs[0] if logs else None


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
    return find_card_database()


def format_checked_paths(label: str, paths: list[Path]) -> str:
    """Format candidate paths and existence state for diagnostics."""
    lines = [f"{label} checked:"]
    if not paths:
        lines.append("  (none)")
        return "\n".join(lines)
    for path in dedupe_paths(paths):
        lines.append(f"  [{'yes' if path_exists(path) else 'no '}] {path}")
    return "\n".join(lines)


def path_setup_instructions(
    missing: list[str],
    player_checked: list[Path] | None = None,
    carddb_checked: list[Path] | None = None,
) -> str:
    """Build a user-facing setup message when paths cannot be discovered."""
    missing_text = " and ".join(missing)
    checked_sections = []
    if player_checked is not None:
        checked_sections.append(format_checked_paths("Player log paths", player_checked))
    if carddb_checked is not None:
        checked_sections.append(format_checked_paths("Card database paths", carddb_checked))
    checked_text = "\n\n".join(checked_sections)
    if checked_text:
        checked_text = f"\n\n{checked_text}"
    return f"""Could not find {missing_text}.{checked_text}

You can pass paths directly:
  python3 mtga_extract_games.py "/path/to/Player.log" "/path/to/Raw_CardDatabase_....mtga" --last 1

Or set environment variables:
  export LOG="$HOME/Library/Logs/Wizards Of The Coast/MTGA/Player.log"
  export CARDDB="$HOME/Library/Application Support/com.wizards.mtga/Downloads/Raw/Raw_CardDatabase_....mtga"

On macOS, Player.log is usually here:
  ~/Library/Logs/Wizards Of The Coast/MTGA/Player.log

The card database is usually the newest Raw_CardDatabase_*.mtga file here:
  ~/Library/Application Support/com.wizards.mtga/Downloads/Raw/

On Windows, Player.log is usually here:
  %USERPROFILE%\\AppData\\LocalLow\\Wizards Of The Coast\\MTGA\\Player.log

Common Windows card database locations are under:
  %ProgramFiles%\\Wizards of the Coast\\MTGA\\MTGA_Data\\Downloads\\Raw
  %LOCALAPPDATA%\\Packages\\WizardsOfTheCoast.MTGA_8wekyb3d8bbwe\\LocalCache\\Local\\Temp\\Wizards Of The Coast\\MTGA\\Downloads\\Raw
"""


def discover_input_paths(
    player_log_arg: Path | None,
    carddb_arg: Path | None,
    *,
    platform_name: str | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    """Discover input paths and collect candidate diagnostics."""
    env = env or os.environ
    os_name = operating_system_name(platform_name)
    player_checked = []
    carddb_checked = []

    player_log = None
    player_source = None
    if player_log_arg:
        player_log = player_log_arg.expanduser()
        player_source = "argument"
        record_checked_path(player_checked, player_log)
    else:
        player_log = env_path_from_names(env, ["LOG", "PLAYER_LOG", "MTGA_PLAYER_LOG"])
        if player_log:
            player_source = "environment"
            record_checked_path(player_checked, player_log)
        else:
            logs = find_player_logs(platform_name, env, player_checked)
            for log_path in logs:
                if log_path.name == "Player.log":
                    player_log = log_path
                    break
            if player_log is None and logs:
                player_log = logs[0]
            if player_log:
                player_source = "autodiscovery"

    carddb = None
    carddb_source = None
    if carddb_arg:
        carddb = carddb_arg.expanduser()
        carddb_source = "argument"
        record_checked_path(carddb_checked, carddb)
    else:
        carddb = env_path_from_names(env, ["CARDDB", "MTGA_CARDDB", "MTGA_CARD_DATABASE"])
        if carddb:
            carddb_source = "environment"
            record_checked_path(carddb_checked, carddb)
        else:
            carddb = find_card_database(platform_name, env, carddb_checked)
            if carddb:
                carddb_source = "autodiscovery"

    env_names = ["LOG", "PLAYER_LOG", "MTGA_PLAYER_LOG", "CARDDB", "MTGA_CARDDB", "MTGA_CARD_DATABASE"]
    if os_name == "Windows":
        env_names.extend(WINDOWS_ENV_VARS)
    else:
        env_names.append("HOME")

    return {
        "os": os_name,
        "env": {name: env.get(name) for name in env_names if env.get(name)},
        "player_log": player_log,
        "player_source": player_source,
        "player_checked": player_checked,
        "carddb": carddb,
        "carddb_source": carddb_source,
        "carddb_checked": carddb_checked,
    }


def format_path_diagnostics(diagnostics: dict) -> str:
    """Format path discovery diagnostics for --find-paths."""
    lines = [f"Operating system detected: {diagnostics['os']}"]
    lines.append("Environment variables used:")
    if diagnostics["env"]:
        for name, value in diagnostics["env"].items():
            lines.append(f"  {name}={value}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append(format_checked_paths("Player log paths", diagnostics["player_checked"]))
    selected_log = diagnostics["player_log"]
    lines.append(
        f"Selected player log: {selected_log if selected_log and path_exists(selected_log) else '(none)'}"
    )
    if diagnostics["player_source"]:
        lines.append(f"Player log source: {diagnostics['player_source']}")
    lines.append("")
    lines.append(format_checked_paths("Card database paths", diagnostics["carddb_checked"]))
    selected_carddb = diagnostics["carddb"]
    lines.append(
        f"Selected card database: {selected_carddb if selected_carddb and path_exists(selected_carddb) else '(none)'}"
    )
    if diagnostics["carddb_source"]:
        lines.append(f"Card database source: {diagnostics['carddb_source']}")
    return "\n".join(lines)


def resolve_input_paths(
    player_log_arg: Path | None,
    carddb_arg: Path | None,
    *,
    live: bool = False,
    platform_name: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[Path, Path, str | None]:
    """Resolve CLI/env/autodiscovered paths and return an optional warning."""
    diagnostics = discover_input_paths(
        player_log_arg,
        carddb_arg,
        platform_name=platform_name,
        env=env,
    )
    player_log = diagnostics["player_log"]
    carddb = diagnostics["carddb"]

    missing = []
    if player_log is None or not path_exists(player_log):
        missing.append("Player.log")
    if carddb is None or not path_exists(carddb):
        missing.append("Raw_CardDatabase_*.mtga")
    if missing:
        raise FileNotFoundError(
            path_setup_instructions(
                missing,
                diagnostics["player_checked"],
                diagnostics["carddb_checked"],
            )
        )

    warning = None
    if player_log_arg is None or carddb_arg is None:
        all_log_paths = player_log_paths_for_reading(player_log, live)
        log_lines = ["Using logs:"]
        log_lines.extend(f"  {log_path}" for log_path in all_log_paths)
        log_lines.append(f"Using card database: {carddb}")
        warning = "\n".join(log_lines)
    return player_log, carddb, warning


def find_last_game_start(
    player_log: Path,
    player_names_by_match: dict[str, dict[int, str]] | None = None,
) -> tuple[int, int]:
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
            if player_names_by_match is not None:
                room_match_id, room_player_names = match_room_player_names(root)
                if room_match_id and room_player_names:
                    player_names_by_match[room_match_id] = room_player_names
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


def match_room_player_names(root: dict) -> tuple[str | None, dict[int, str]]:
    """Extract Arena seat-to-name mappings from a match-room state record."""
    event = root.get("matchGameRoomStateChangedEvent") or {}
    room_info = event.get("gameRoomInfo") or {}
    config = room_info.get("gameRoomConfig") or {}
    match_id = config.get("matchId")
    players = config.get("reservedPlayers") or event.get("players") or []
    names = {}
    for player in players:
        seat = player.get("systemSeatId")
        name = player.get("playerName")
        if seat in (1, 2) and name:
            names[seat] = str(name)
    return match_id, names


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


def is_numeric_cli_token(value) -> bool:
    """Return true for positional arguments that look like mistaken selectors."""
    return value is not None and str(value).strip().isdigit()


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
MANA_SYMBOL_LABELS = {
    1: "W",
    2: "U",
    3: "B",
    4: "R",
    5: "G",
    6: "C",
}
MANA_COLOR_VALUES = {
    "ManaColor_White": 1,
    "ManaColor_Blue": 2,
    "ManaColor_Black": 3,
    "ManaColor_Red": 4,
    "ManaColor_Green": 5,
    "ManaColor_Colorless": 6,
    "ManaColor_Generic": 6,
}
COLORLESS_LAND_NAMES = {
    "Cavern of Souls",
    "Field of Ruin",
    "Mutavault",
    "Nykthos, Shrine to Nyx",
    "Reliquary Tower",
}
LAND_MANA_OPTIONS = {
    "Adarkar Wastes": {1, 2},
    "Battlefield Forge": {1, 4},
    "Brushland": {1, 5},
    "Caves of Koilos": {1, 3},
    "Karplusan Forest": {4, 5},
    "Llanowar Wastes": {3, 5},
    "Shivan Reef": {2, 4},
    "Sulfurous Springs": {3, 4},
    "Underground River": {2, 3},
    "Yavimaya Coast": {2, 5},
    "Raugrin Triome": {1, 2, 4},
    "Rockfall Vale": {4, 5},
    "Command Tower": {1, 2, 3, 4, 5},
    **{name: {value} for value, name in ((1, "Plains"), (2, "Island"), (3, "Swamp"), (4, "Mountain"), (5, "Forest"))},
    **{name: {value} for value, name in ((1, "Snow-Covered Plains"), (2, "Snow-Covered Island"), (3, "Snow-Covered Swamp"), (4, "Snow-Covered Mountain"), (5, "Snow-Covered Forest"))},
    **{name: {6} for name in ("Wastes", *COLORLESS_LAND_NAMES, "Urza's Mine", "Urza's Power Plant", "Urza's Tower")},
}
NON_MANA_LAND_NAMES = {
    "Evolving Wilds",
    "Fabled Passage",
    "Flooded Strand",
    "Marsh Flats",
    "Misty Rainforest",
    "Polluted Delta",
    "Prismatic Vista",
    "Scalding Tarn",
    "Verdant Catacombs",
    "Windswept Heath",
    "Wooded Foothills",
}
MULTICOLOUR_LAND_COLORS = {
    "Wind-Scarred Crag": (1, 4),
}
DEFAULT_CARD_NAME_COLORS = {
    **{name: (color,) for name, color in LAND_COLORS.items()},
    **{name: (COLOURLESS_MANA_COLOR,) for name in COLORLESS_LAND_NAMES},
    **{name: colors for name, colors in MULTICOLOUR_LAND_COLORS.items()},
}
ARCHIVE_SCHEMA_VERSION = 3
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
    if "trigger:" in line or "ability:" in line:
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
        """Replace a highlighted list conjunction with neutral colour and restore the outer colour."""
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
        """Replace a matched card name with its mana-coloured transcript form."""
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
    if source and source.startswith("grpId "):
        return None
    if not source or should_group_life_change_source(source):
        return source
    return f"{source} ability"


PHASE_SECTION_ORDER = [
    "Untap",
    "Upkeep",
    "Draw",
    "Pre-combat main",
    "Beginning of combat",
    "Declare attackers",
    "Declare blockers",
    "First strike combat damage",
    "Combat damage",
    "End of combat",
    "Postcombat main",
    "End",
    "Cleanup",
]
PHASE_SECTION_INDEX = {section: index for index, section in enumerate(PHASE_SECTION_ORDER)}


def phase_section_label(turn_info: dict) -> str | None:
    """Return the normal Magic step name for an Arena phase/step pair."""
    phase = turn_info.get("phase")
    step = turn_info.get("step")
    if phase == "Phase_Beginning":
        return {
            "Step_Untap": "Untap",
            "Step_Upkeep": "Upkeep",
            "Step_Draw": "Draw",
        }.get(step, "Beginning")
    if phase == "Phase_Main1":
        return "Pre-combat main"
    if phase == "Phase_Combat":
        return {
            "Step_BeginCombat": "Beginning of combat",
            "Step_DeclareAttack": "Declare attackers",
            "Step_DeclareBlock": "Declare blockers",
            "Step_FirstStrikeDamage": "First strike combat damage",
            "Step_CombatDamage": "Combat damage",
            "Step_EndCombat": "End of combat",
        }.get(step, "Combat")
    if phase == "Phase_Main2":
        return "Postcombat main"
    if phase == "Phase_Ending":
        return {
            "Step_End": "End",
            "Step_Cleanup": "Cleanup",
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


def phrase_source_action(source: str | None, text: str) -> str:
    """Prefix an event with its source when Arena exposes a useful source."""
    return f"{source}: {text}" if source else text


def phrase_draw(label: str, name: str, visible: bool) -> str:
    """Render draws without revealing hidden opponent cards."""
    card_text = name if visible else "a card"
    return phrase_player_action(label, "draw", card_text)


def phrase_sacrifice(source: str | None, label: str, name: str) -> str:
    """Render sacrifice events, keeping source-trigger context when available."""
    return phrase_source_action(source, phrase_player_action(label, "sacrifice", name))


def phrase_move_to_zone(
    source: str | None,
    label: str,
    verb: str,
    name: str,
    zone: str,
    preposition: str = "to",
) -> str:
    """Render visible non-cast zone movements such as seek, return, or conjure."""
    text = phrase_player_action(label, verb, f"{name} {preposition} {zone}")
    return phrase_source_action(source, text)


def phrase_scry(label: str, top_names: list[str], bottom_names: list[str]) -> str:
    """Render Arena's exposed scry ordering without inventing hidden choices."""
    parts = []
    if top_names:
        parts.append(f"top: {joined_semicolon_list(top_names)}")
    if bottom_names:
        parts.append(f"bottom: {joined_semicolon_list(bottom_names)}")
    if not parts:
        parts.append("no cards moved")
    return phrase_player_action(label, "scry", "; ".join(parts), third_person="scries")


def phrase_shuffle(source: str | None, label: str) -> str:
    """Render library shuffles in player voice."""
    library = "my library" if label == "Me" else "their library"
    return phrase_source_action(source, phrase_player_action(label, "shuffle", library))


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


def phrase_damage(source: str | None, damage: int, target: str) -> str:
    """Render a direct damage event without adding rules interpretation."""
    return f"{source or 'A source'} deals {damage} damage to {target}"


def damage_mark_suffix(marked_damage, toughness) -> str:
    """Render Arena's marked damage against toughness for damaged permanents."""
    if not isinstance(marked_damage, int) or marked_damage <= 0:
        return ""
    if isinstance(toughness, dict):
        toughness = toughness.get("value")
    if isinstance(toughness, int) and toughness > 0:
        return f" ({marked_damage}/{toughness} damage)"
    return f" ({marked_damage} damage marked)"


def arena_value_int(value) -> int | None:
    """Extract an integer from Arena scalar fields that may be wrapped as {'value': N}."""
    if isinstance(value, dict):
        value = value.get("value")
    return value if isinstance(value, int) else None


def power_toughness_summary(obj: dict | None) -> str | None:
    """Return a creature's current power/toughness as X/Y when Arena exposes both values."""
    if not obj or "CardType_Creature" not in set(obj.get("cardTypes") or []):
        return None
    power = arena_value_int(obj.get("power"))
    toughness = arena_value_int(obj.get("toughness"))
    if power is None or toughness is None:
        return None
    return f"{power}/{toughness}"


def token_board_name(name: str, obj: dict | None) -> str:
    """Prefix token permanent names with Token so board snapshots distinguish tokens from cards."""
    if not obj or obj.get("type") != "GameObjectType_Token" or name.startswith("Token "):
        return name
    return f"Token {name}"


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


def printed_power_toughness_value(value: str | None) -> int | None:
    """Parse numeric printed power/toughness values from card metadata."""
    if value is None:
        return None
    text = str(value).strip()
    return int(text) if re.fullmatch(r"-?\d+", text) else None


def capped_power_toughness_counter_amount(
    name: str,
    amount: int,
    current_power: int | None,
    current_toughness: int | None,
    printed_power: int | None,
    printed_toughness: int | None,
) -> int:
    """Cap simple positive P/T counters when current stats prove accumulated logs drifted."""
    match = re.fullmatch(r"\+(\d+)/\+(\d+)", name)
    if (
        not match
        or amount <= 0
        or current_power is None
        or current_toughness is None
        or printed_power is None
        or printed_toughness is None
    ):
        return amount
    power_step, toughness_step = (int(part) for part in match.groups())
    power_delta = current_power - printed_power
    toughness_delta = current_toughness - printed_toughness
    if power_delta < 0 or toughness_delta < 0:
        return amount
    cap = min(power_delta // power_step, toughness_delta // toughness_step)
    if cap < amount:
        return max(0, cap)
    return amount


def effect_power_toughness_modifier(
    current_power: int | None,
    current_toughness: int | None,
    printed_power: int | None,
    printed_toughness: int | None,
    counter_parts: list[str],
) -> str | None:
    """Infer remaining visible P/T modification after printed stats and counters."""
    if None in {current_power, current_toughness, printed_power, printed_toughness}:
        return None
    power_delta = current_power - printed_power
    toughness_delta = current_toughness - printed_toughness
    for part in counter_parts:
        match = re.fullmatch(r"([+-]\d+)/([+-]\d+) from counters", part)
        if not match:
            continue
        power_delta -= int(match.group(1))
        toughness_delta -= int(match.group(2))
    if power_delta == 0 and toughness_delta == 0:
        return None
    power_sign = "+" if power_delta >= 0 else ""
    toughness_sign = "+" if toughness_delta >= 0 else ""
    return f"{power_sign}{power_delta}/{toughness_sign}{toughness_delta} from effects"


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


def is_cast_line(line: str) -> bool:
    """Return true for normal transcript cast lines after perspective wording."""
    return bool(re.fullmatch(r"(I|Opponent|Player \d+) casts? .+", line or ""))


def cast_line_identity(line: str) -> tuple[str, str] | None:
    """Return the actor/card identity for duplicate-cast cleanup."""
    match = re.fullmatch(r"(I|Opponent|Player \d+) casts? (.+)", line or "")
    if not match:
        return None
    actor, spell = match.groups()
    # Drop suffixes that are derived from later state, not spell identity.
    spell = spell.split(" targeting ", 1)[0]
    spell = spell.split(" [spent ", 1)[0]
    spell = spell.split("; commander cast", 1)[0]
    return actor, spell


def is_non_event_separator(line: str) -> bool:
    """Return true for structural lines that can sit between duplicate events."""
    return not line or line.startswith("-- ")


def remove_near_duplicate_cast_lines(lines: list[str]) -> list[str]:
    """Remove duplicate cast echoes separated only by phase/blank separators."""
    cleaned = []
    recent_cast = None
    recent_separator_count = 0
    for line in lines:
        identity = cast_line_identity(line)
        if identity and recent_cast == identity and recent_separator_count <= 2:
            continue
        cleaned.append(line)
        if identity:
            recent_cast = identity
            recent_separator_count = 0
        elif is_non_event_separator(line) and recent_cast is not None:
            recent_separator_count += 1
        else:
            recent_cast = None
            recent_separator_count = 0
    return cleaned


def remove_repeated_cast_blocks(lines: list[str]) -> list[str]:
    """Remove adjacent repeated cast blocks emitted by low-fidelity updates."""
    cleaned = []
    index = 0
    while index < len(lines):
        if not cast_line_identity(lines[index]):
            cleaned.append(lines[index])
            index += 1
            continue

        max_block_len = min(6, (len(lines) - index) // 2)
        for block_len in range(max_block_len, 1, -1):
            first = lines[index : index + block_len]
            second = lines[index + block_len : index + (block_len * 2)]
            first_identities = [cast_line_identity(line) for line in first]
            second_identities = [cast_line_identity(line) for line in second]
            if (
                all(first_identities)
                and first_identities == second_identities
                and len(set(first_identities)) == block_len
            ):
                cleaned.extend(first)
                index += block_len * 2
                break
        else:
            cleaned.append(lines[index])
            index += 1
    return cleaned


def transcript_diagnostics(lines: list[str]) -> list[str]:
    """Return suspicious transcript artifacts that should be reviewed by a human."""
    warnings = []
    patterns = [
        ("raw grpId leaked", re.compile(r"\bgrpId \d+\b")),
        ("raw instance id leaked", re.compile(r"\binstance \d+\b")),
        ("unidentified permanent death", re.compile(r"\bA permanent dies\b")),
        ("negative library count", re.compile(r"\bLibrary: -\d+ cards?\b")),
        ("unknown keyword choice", re.compile(r"\bunknown keyword \d+\b")),
        ("unknown creature type choice", re.compile(r"\bunknown creature type \d+\b")),
    ]
    for line_number, line in enumerate(lines, start=1):
        for label, pattern in patterns:
            if pattern.search(line):
                warnings.append(f"line {line_number}: {label}: {line}")
                break
        if re.search(r"\+\d{2,}/\+\d{2,} from counters", line):
            warnings.append(f"line {line_number}: large counter summary, verify token/base stats: {line}")
    seen_cast = None
    separator_count = 0
    for line_number, line in enumerate(lines, start=1):
        identity = cast_line_identity(line)
        if identity and identity == seen_cast and separator_count <= 2:
            warnings.append(f"line {line_number}: near-duplicate cast line: {line}")
        if identity:
            seen_cast = identity
            separator_count = 0
        elif is_non_event_separator(line) and seen_cast is not None:
            separator_count += 1
        else:
            seen_cast = None
            separator_count = 0
    return warnings


def parse_damage_life_unit(lines: list[str], index: int) -> tuple[dict, int] | None:
    """Parse repeated damage/life units for compact storm-copy summaries."""
    if index + 1 >= len(lines):
        return None
    damage_match = re.fullmatch(r"(.+) deals (\d+) damage to (me|Opponent|Player \d+)", lines[index])
    life_match = re.fullmatch(r"(I|Opponent|Player \d+) loses? (\d+) life \((-?\d+)\)", lines[index + 1])
    if not damage_match or not life_match:
        return None

    source, damage, target = damage_match.groups()
    loser, life_loss, total = life_match.groups()
    expected_loser = "I" if target == "me" else target
    if loser != expected_loser or int(damage) != int(life_loss):
        return None

    consumed = 2
    resolve_line = None
    if index + 2 < len(lines) and lines[index + 2] == f"{source} resolves":
        resolve_line = lines[index + 2]
        consumed = 3

    return {
        "source": source,
        "damage": int(damage),
        "target": target,
        "loser": loser,
        "life_loss": int(life_loss),
        "total": int(total),
        "resolve_line": resolve_line,
    }, consumed


def combine_repeated_damage_life_sequences(lines: list[str]) -> list[str]:
    """Collapse repeated same damage/life-loss units while keeping final life total."""
    combined = []
    index = 0
    while index < len(lines):
        parsed = parse_damage_life_unit(lines, index)
        if not parsed:
            combined.append(lines[index])
            index += 1
            continue

        first_unit, consumed = parsed
        units = [first_unit]
        cursor = index + consumed
        while True:
            next_parsed = parse_damage_life_unit(lines, cursor)
            if not next_parsed:
                break
            next_unit, next_consumed = next_parsed
            comparable_keys = ("source", "damage", "target", "loser", "life_loss", "resolve_line")
            if any(first_unit[key] != next_unit[key] for key in comparable_keys):
                break
            units.append(next_unit)
            cursor += next_consumed

        if len(units) == 1:
            combined.extend(lines[index : index + consumed])
        else:
            total_damage = first_unit["damage"] * len(units)
            combined.append(
                f"{len(units)}x {first_unit['source']} deals "
                f"{total_damage} damage to {first_unit['target']}"
            )
            loser_label = "Me" if first_unit["loser"] == "I" else first_unit["loser"]
            combined.append(
                phrase_player_action(
                    loser_label,
                    "lose",
                    f"{total_damage} life ({units[-1]['total']})",
                )
            )
            if first_unit["resolve_line"]:
                combined.append(f"{len(units)}x {first_unit['resolve_line']}")
        index = cursor
    return combined


def move_copy_resolves_after_revealed_returns(lines: list[str]) -> list[str]:
    """Keep copied bounce effects readable when Arena exposes the next target early."""
    reordered = []
    index = 0
    while index < len(lines):
        if index + 2 >= len(lines):
            reordered.append(lines[index])
            index += 1
            continue

        resolve_match = re.fullmatch(r"(A copy of .+) resolves", lines[index])
        reveal_match = re.fullmatch(r"(I|Opponent|Player \d+) reveals? (.+)", lines[index + 1])
        if not resolve_match or not reveal_match:
            reordered.append(lines[index])
            index += 1
            continue

        source = resolve_match.group(1)
        revealed_name = reveal_match.group(2)
        return_line = lines[index + 2]
        return_pattern = re.escape(source) + r": (I|Opponent|Player \d+) returns? " + re.escape(revealed_name) + r" to hand"
        if not re.fullmatch(return_pattern, return_line):
            reordered.append(lines[index])
            index += 1
            continue

        reordered.extend([lines[index + 1], return_line])
        cursor = index + 3
        while cursor < len(lines) and re.fullmatch(r"(I|Opponent|Player \d+|\d+x (?:I|Opponent|Player \d+)) draws? .+", lines[cursor]):
            reordered.append(lines[cursor])
            cursor += 1
        reordered.append(lines[index])
        index = cursor
    return reordered


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


def counter_summary_parts(
    counter_totals: Counter,
    counter_names: dict[int, str],
    *,
    current_power: int | None = None,
    current_toughness: int | None = None,
    printed_power: int | None = None,
    printed_toughness: int | None = None,
) -> list[str]:
    """Render object counters as board-state modifier fragments."""
    parts = []
    for raw_type, amount in sorted(counter_totals.items(), key=lambda item: str(item[0])):
        if amount <= 0:
            continue
        name = counter_names.get(raw_type, f"counter {raw_type}")
        amount = capped_power_toughness_counter_amount(
            name,
            amount,
            current_power,
            current_toughness,
            printed_power,
            printed_toughness,
        )
        if amount <= 0:
            continue
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
        ("curse", "enchanted by"),
        ("aura", "enchanted by"),
        ("equipment", "equipped with"),
        ("other", "attached to"),
    ):
        phrase = grouped_name_phrase(attachment_names_by_kind.get(kind, []))
        if phrase:
            parts.append(f"{verb} {phrase}")
    return parts


def attached_to_player_summary_parts(attachment_names_by_kind: dict[str, list[str]]) -> list[str]:
    """Render attachments that are attached directly to a player."""
    parts = []
    for kind, label in (
        ("curse", "Curses"),
        ("aura", "Auras"),
        ("equipment", "Equipment"),
        ("other", "Other"),
    ):
        phrase = grouped_name_phrase(attachment_names_by_kind.get(kind, []))
        if phrase:
            parts.append(f"{label}: {phrase}")
    return parts


def emblem_summary_line(names: list[str]) -> str | None:
    """Render emblems as a player-state line when Arena exposes emblem objects."""
    phrase = grouped_name_phrase(names)
    return f"Emblems: {phrase}" if phrase else None


def concise_engine_keyword_labels(
    ability_name: str | None,
    important_keywords: set[str] | None = None,
) -> list[str]:
    """Extract concise, unconditional keywords from one localized ability.

    CARDDB category 3 also contains complete rules sentences. Only exact
    keyword labels, combinations of exact labels, and short parameterized
    keyword labels are safe to attach directly to a permanent. In particular,
    this avoids claiming that an anthem saying "Creatures you control have
    flying" itself has flying.
    """
    important_keywords = important_keywords or IMPORTANT_ENGINE_KEYWORDS
    clean_name = clean_localized_enum_name(ability_name)
    if not clean_name:
        return []
    normalized = re.sub(r"\s+", " ", clean_name).strip().rstrip(".").casefold()
    if normalized in important_keywords:
        return [normalized]

    # Arena sometimes combines multiple bare keywords in one ability record.
    parts = [part.strip() for part in re.split(r"\s*(?:,|\band\b)\s*", normalized)]
    if len(parts) > 1 and all(part in important_keywords for part in parts):
        return list(dict.fromkeys(parts))

    # Preserve the useful qualifier/cost for compact parameterized keywords,
    # while rejecting sentences and continuous effects that merely mention one.
    if len(normalized) <= 80 and not any(mark in normalized for mark in ('"', ";")):
        for keyword in PARAMETERIZED_ENGINE_KEYWORDS & important_keywords:
            if re.match(rf"^{re.escape(keyword)}(?:\s+|[—-])", normalized):
                return [normalized]
    return []


def engine_keyword_summary(
    abilities: list[dict],
    keyword_names: dict[int, str],
    important_keywords: set[str] | None = None,
) -> list[str]:
    """Return important engine-exposed keywords from object uniqueAbilities."""
    important_keywords = important_keywords or IMPORTANT_ENGINE_KEYWORDS
    names = []
    for ability in abilities or []:
        name = keyword_names.get(ability.get("grpId"))
        for label in concise_engine_keyword_labels(name, important_keywords):
            if label not in names:
                names.append(label)
    return names


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


def preferred_zone_transfer_name(detail_name: str | None, fallback_name: str | None) -> str:
    """Prefer explicit ZoneTransfer card identity over possibly stale object state."""
    if detail_name and not detail_name.startswith("grpId "):
        return detail_name
    return fallback_name or detail_name or "unknown card"


def annotation_details(details: list[dict] | None) -> dict:
    """Convert Arena's typed annotation details into ordinary values."""
    result = {}
    for detail in details or []:
        value = None
        for key in ("valueString", "valueInt32", "valueBool", "valueFloat", "valueDouble"):
            values = detail.get(key)
            if values:
                value = values[0] if len(values) == 1 else values
                break
        result[detail.get("key")] = value
    return result


def reconcile_zone_transfer_objects(
    objects: dict,
    annotations: list[dict] | None,
    known_zones: dict | None = None,
) -> dict:
    """Apply and remember annotation-only zone moves in event order."""
    known_zones = known_zones if known_zones is not None else {}
    for annotation in annotations or []:
        if "AnnotationType_ZoneTransfer" not in set(annotation.get("type") or []):
            continue
        destination = annotation_details(annotation.get("details")).get("zone_dest")
        if destination is None:
            continue
        for instance_id in annotation.get("affectedIds") or []:
            known_zones[instance_id] = destination
    for instance_id, destination in known_zones.items():
        obj = objects.get(instance_id)
        if obj is not None:
            obj["zoneId"] = destination
    return known_zones


def disambiguated_object_label(instance_id: int, objects: dict, label_for_id) -> str:
    """Number same-name objects controlled by the same player in one zone."""
    label = label_for_id(instance_id)
    obj = objects.get(instance_id) or {}
    peers = []
    for peer_id, peer in objects.items():
        if peer.get("zoneId") != obj.get("zoneId"):
            continue
        if peer.get("controllerSeatId") != obj.get("controllerSeatId"):
            continue
        if label_for_id(peer_id) == label:
            peers.append(peer_id)
    peers.sort()
    if len(peers) < 2 or instance_id not in peers:
        return label
    return f"{label} #{peers.index(instance_id) + 1}"


def non_mana_tap_events(annotations: list[dict] | None) -> list[tuple[int, int, bool]]:
    """Return sourced tap changes, excluding mana and routine combat taps."""
    annotations = annotations or []
    mana_sources = {
        annotation.get("affectorId")
        for annotation in annotations
        if "AnnotationType_ManaPaid" in set(annotation.get("type") or [])
    }
    events = []
    for annotation in annotations:
        if "AnnotationType_TappedUntappedPermanent" not in set(annotation.get("type") or []):
            continue
        source_id = annotation.get("affectorId")
        if source_id is None:
            continue
        tapped = bool(annotation_details(annotation.get("details")).get("tapped"))
        for affected_id in annotation.get("affectedIds") or []:
            if affected_id not in mana_sources:
                events.append((source_id, affected_id, tapped))
    return events


def base_cast_name(cast_text: str) -> str:
    """Strip transcript suffixes so delayed casts can be matched by spell name."""
    return (
        cast_text.split(" targeting ", 1)[0]
        .split(" [spent ", 1)[0]
        .split(" from command zone", 1)[0]
        .split(";", 1)[0]
    )


def replace_base_cast_name(cast_text: str, new_name: str) -> str:
    """Keep cast suffixes while replacing stale spell identity."""
    old_name = base_cast_name(cast_text)
    if old_name and cast_text.startswith(old_name):
        return f"{new_name}{cast_text[len(old_name):]}"
    return new_name


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
    if set(obj.get("cardTypes") or []) & {"CardType_Land", "CardType_Dungeon"}:
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


def phrase_power_toughness_mod(source: str | None, target: str, power: int, toughness: int) -> str:
    """Render Arena's generic P/T modification annotation as readable rules text."""
    power_text = f"{power:+d}"
    toughness_text = f"{toughness:+d}"
    effect = f"{target} gets {power_text}/{toughness_text}"
    if source == target:
        source = None
    return phrase_source_action(source, effect)


def adventure_exile_line(card_name: str, adventure_name: str | None) -> str | None:
    """Explain that an adventure spell resolved by exiling its parent card."""
    if not adventure_name or adventure_name == card_name:
        return None
    return f"{card_name} is exiled on an adventure"


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


def library_delta_for_zone_transfer(
    category: str | None,
    source_zone_type: str | None,
    dest_zone_type: str | None,
    affected_count: int,
) -> int:
    """Return the library-size delta implied by one Arena ZoneTransfer annotation."""
    if affected_count <= 0:
        return 0
    source_is_library = source_zone_type == "ZoneType_Library"
    dest_is_library = dest_zone_type == "ZoneType_Library"
    if source_is_library and not dest_is_library:
        return -affected_count
    if dest_is_library and not source_is_library:
        return affected_count
    # Shuffle, reveal, and library reordering events can touch library objects
    # without changing the count. Treat library->library as a no-op.
    return 0


def library_movement_description(
    category: str | None,
    source_zone_type: str | None,
    dest_zone_type: str | None,
) -> str:
    """Describe the library-facing direction of a zone movement for debug logs."""
    source = clean_arena_enum(source_zone_type, "ZoneType_") or "unknown"
    dest = clean_arena_enum(dest_zone_type, "ZoneType_") or "unknown"
    category_text = category or "ZoneTransfer"
    if source_zone_type == "ZoneType_Library" and dest_zone_type != "ZoneType_Library":
        return f"{category_text}: library -> {dest}"
    if dest_zone_type == "ZoneType_Library" and source_zone_type != "ZoneType_Library":
        return f"{category_text}: {source} -> library"
    if source_zone_type == "ZoneType_Library" and dest_zone_type == "ZoneType_Library":
        return f"{category_text}: library -> library"
    return f"{category_text}: {source} -> {dest}"


def apply_authoritative_library_count(
    inferred_count: int | None,
    authoritative_count: int | None,
) -> tuple[int | None, bool]:
    """Prefer Arena's zone count when it disagrees with an inferred library count."""
    if authoritative_count is None:
        return inferred_count, False
    if inferred_count != authoritative_count:
        return authoritative_count, inferred_count is not None
    return inferred_count, False


def format_card_mana_cost(raw_cost: str | None) -> str | None:
    """Format Arena's compact mana text as a short human-readable cost."""
    if not raw_cost:
        return None
    parts = re.findall(r"o(\d+|[WUBRGCX])", raw_cost)
    if not parts:
        return None
    return "".join(parts)


def card_cost_suffix(metadata: dict | None) -> str:
    """Return a compact hand-zone mana cost suffix when known."""
    cost = format_card_mana_cost((metadata or {}).get("mana_cost"))
    if not cost:
        return ""
    return f" [cost {cost}]"


def mana_option_labels(options: set[int]) -> str:
    """Render one land's possible mana colours compactly."""
    return "/".join(MANA_SYMBOL_LABELS[value] for value in sorted(options) if value in MANA_SYMBOL_LABELS)


def mana_options_from_ability_texts(metadata: dict | None) -> set[int] | None:
    """Conservatively parse simple Add-one-mana land ability text from card metadata."""
    if not metadata:
        return None
    values = set()
    for text in metadata.get("ability_texts") or []:
        if "add" not in text.casefold():
            continue
        for symbol in re.findall(r"\{o([WUBRGC])\}", text):
            value = {"W": 1, "U": 2, "B": 3, "R": 4, "G": 5, "C": 6}.get(symbol)
            if value:
                values.add(value)
    return values or None


def mana_restriction_from_ability_texts(metadata: dict | None) -> str | None:
    """Return a short spending restriction when a mana ability is visibly limited."""
    if not metadata:
        return None
    for text in metadata.get("ability_texts") or []:
        lower = text.casefold()
        if "spend this mana only" not in lower:
            continue
        if "activate abilities" in lower:
            return "abilities only"
        if "cast creature" in lower or "creature spells" in lower:
            return "creatures only"
        if "cast" in lower and "spell" in lower:
            return "restricted spells only"
        return "restricted"
    return None


def land_mana_options(name: str, metadata: dict | None = None) -> set[int] | None:
    """Return known mana options for a land name, or None when it is an unknown source."""
    if name in NON_MANA_LAND_NAMES:
        return set()
    if name in LAND_MANA_OPTIONS:
        return set(LAND_MANA_OPTIONS[name])
    return mana_options_from_ability_texts(metadata)


def mana_value_from_color(color: str | None) -> int | None:
    """Convert an Arena mana colour enum to this module's compact colour value."""
    return MANA_COLOR_VALUES.get(color or "")


def mana_values_from_payment_option(option: dict) -> set[int]:
    """Collect possible colour values from one Arena mana payment option."""
    values = set()
    for mana in option.get("mana") or []:
        color = mana.get("color")
        if color == "ManaColor_AnyColor":
            values.update({1, 2, 3, 4, 5})
            continue
        value = mana_value_from_color(color)
        if value:
            values.add(value)
    return values


def mana_amount_from_payment_option(option: dict) -> int:
    """Return the amount of mana represented by one Arena payment option."""
    amount = 0
    for mana in option.get("mana") or []:
        try:
            amount += int(mana.get("count") or 1)
        except (TypeError, ValueError):
            amount += 1
    return amount


def mana_spec_types_from_option(option: dict | None) -> set[str]:
    """Collect Arena mana spec enum names from one payment option."""
    return {
        spec.get("type")
        for mana in (option or {}).get("mana") or []
        for spec in mana.get("specs") or []
        if spec.get("type")
    }


def mana_sources_from_option(option: dict | None) -> list[int]:
    """Return source object ids Arena attached to a mana payment option."""
    source_ids = []
    for mana in (option or {}).get("mana") or []:
        source_id = mana.get("srcInstanceId")
        if source_id is not None and source_id not in source_ids:
            source_ids.append(source_id)
    return source_ids


def action_mana_payment_options(action: dict | None) -> list[dict]:
    """Return all payment options Arena nested under a cast/action record.

    The normal manaCost field says what was paid. Arena's manaPaymentOptions and
    autoTapSolution records can also carry rules-relevant specs such as
    ManaSpecType_CantBeCountered, which is how sources like Delighted Halfling
    and Cavern of Souls expose their extra effect.
    """
    options = list((action or {}).get("manaPaymentOptions") or [])
    for auto_tap in ((action or {}).get("autoTapSolution") or {}).get("autoTapActions") or []:
        option = auto_tap.get("manaPaymentOption")
        if option:
            options.append(option)
    return options


def mana_amount_from_pool_entry(entry: dict) -> int:
    """Return the amount of mana represented by one Arena mana-pool entry."""
    try:
        return int(entry.get("count") or 1)
    except (TypeError, ValueError):
        return 1


def mana_cost_amount(cost_entries: list[dict] | None) -> int:
    """Return the total mana represented by one Arena spell manaCost list."""
    amount = 0
    for entry in cost_entries or []:
        try:
            amount += int(entry.get("count") or 0)
        except (TypeError, ValueError):
            pass
    return amount


def format_mana_cost_entries(cost_entries: list[dict] | None) -> str | None:
    """Format an Arena spell manaCost list as compact spent-mana text."""
    if not cost_entries:
        return None
    generic = 0
    symbols = []
    order = [
        ("ManaColor_White", "W"),
        ("ManaColor_Blue", "U"),
        ("ManaColor_Black", "B"),
        ("ManaColor_Red", "R"),
        ("ManaColor_Green", "G"),
        ("ManaColor_Colorless", "C"),
    ]
    symbol_by_color = dict(order)
    for entry in cost_entries:
        colors = entry.get("color") or []
        try:
            count = int(entry.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if "ManaColor_Generic" in colors:
            generic += count
            continue
        for color, symbol in order:
            if color in colors:
                symbols.extend([symbol] * max(count, 0))
                break
        else:
            for color in colors:
                if color in symbol_by_color:
                    symbols.extend([symbol_by_color[color]] * max(count, 0))
                    break
    text = f"{generic if generic else ''}{''.join(symbols)}"
    return text or None


def format_cast_mana_note(spent_text: str | None, cost_amount: int = 0, available_before: int | None = None) -> str:
    """Format the optional cast-line suffix for spent and remaining mana."""
    if not spent_text:
        return ""
    if available_before is None:
        return f" [spent {spent_text}]"
    remaining = max(int(available_before) - int(cost_amount or 0), 0)
    return f" [spent {spent_text}; remaining {remaining} mana]"


def should_carry_cast_marker(
    source_zone_type: str | None,
    destination_zone_type: str | None,
) -> bool:
    """Keep a cast marker only while the same spell is resolving.

    Arena changes object ids as cards move between zones.  A marker carried to
    hand, command, graveyard, or exile would make a later cast of that physical
    card look like a duplicate and suppress it from the transcript.
    """
    if (
        destination_zone_type == "ZoneType_Stack"
        and source_zone_type not in {"ZoneType_Stack", "ZoneType_Limbo"}
    ):
        return False
    return destination_zone_type in {
        "ZoneType_Stack",
        "ZoneType_Limbo",
        "ZoneType_Battlefield",
        "ZoneType_Graveyard",
        "ZoneType_Exile",
    }


def phrase_mana_tap(source: str, actor: str, permanent: str, mana: str) -> str:
    """Describe a mana ability that taps another permanent as its cost."""
    return (
        f"{source} ability: {subject_pronoun(actor)} "
        f"{present_tense_verb(actor, 'tap', 'taps')} {permanent} for {mana}"
    )


def mana_cost_text_amount(cost_text: str | None) -> int:
    """Return a conservative mana amount from formatted mana text like 2G or WWW."""
    if not cost_text:
        return 0
    amount = 0
    for part in re.findall(r"\d+|[WUBRGC]", cost_text):
        amount += int(part) if part.isdigit() else 1
    return amount


def mana_note_from_specs(option: dict) -> tuple[str | None, str | None]:
    """Split Arena mana specs into real restrictions and source-only tags."""
    ignored_specs = {"ManaSpecType_Predictive", "ManaSpecType_FromTreasure"}
    source_tag_specs = {"ManaSpecType_FromSnow"}
    spec_types = {
        spec.get("type")
        for mana in option.get("mana") or []
        for spec in mana.get("specs") or []
        if spec.get("type")
    }
    filtered = spec_types - ignored_specs
    source_tags = sorted(filtered & source_tag_specs)
    restricted = sorted(filtered - source_tag_specs)
    restriction = ", ".join(spec.replace("ManaSpecType_", "") for spec in restricted) or None
    source_tag = ", ".join(spec.replace("ManaSpecType_", "").removeprefix("From") for spec in source_tags) or None
    return restriction, source_tag


def mana_restriction_from_specs(option: dict) -> str | None:
    """Summarize true Arena mana spending restrictions."""
    restriction, _source_tag = mana_note_from_specs(option)
    return restriction


def format_mana_pool(mana_pool: list[dict] | None) -> str | None:
    """Format the current floating mana pool from Arena player state."""
    if not mana_pool:
        return None
    counts = Counter()
    restricted = []
    source_tags = Counter()
    for entry in mana_pool:
        value = mana_value_from_color(entry.get("color"))
        amount = mana_amount_from_pool_entry(entry)
        if value:
            counts[value] += amount
        restriction, source_tag = mana_note_from_specs({"mana": [entry]})
        if restriction:
            restricted.append(f"{amount} {MANA_SYMBOL_LABELS.get(value, '?')} [{restriction}]")
        if source_tag:
            source_tags[(amount, MANA_SYMBOL_LABELS.get(value, "?"), source_tag)] += 1
    parts = []
    for value in (1, 2, 3, 4, 5, 6):
        if counts[value]:
            parts.append(f"{counts[value]} {MANA_SYMBOL_LABELS[value]}")
    if restricted:
        parts.append(f"restricted: {', '.join(restricted)}")
    if source_tags:
        tag_parts = []
        for (amount, symbol, source_tag), count in sorted(source_tags.items()):
            prefix = f"{count}x " if count > 1 else ""
            tag_parts.append(f"{prefix}{amount} {symbol} [{source_tag}]")
        parts.append(f"source tags: {', '.join(tag_parts)}")
    return f"Floating mana: {'; '.join(parts)}" if parts else None


def mana_pool_count(mana_pool: list[dict] | None) -> int:
    """Count floating mana entries from Arena player state."""
    return sum(mana_amount_from_pool_entry(entry) for entry in mana_pool or [])


def format_available_mana_summary(
    total: int,
    known_options: list[set[int]],
    unknown_count: int = 0,
    restricted: list[str] | None = None,
    label: str = "Available mana",
) -> str | None:
    """Format a mana availability summary for turn-state snapshots."""
    restricted = restricted or []
    if total <= 0 and not restricted:
        return None
    union = set().union(*known_options) if known_options else set()
    color_options = union - {6}
    parts = []
    if color_options:
        parts.append(f"colors: {mana_option_labels(color_options)}")
    colorless_only = sum(1 for options in known_options if options == {6})
    if colorless_only:
        parts.append(f"{colorless_only} colorless-only")
    if unknown_count:
        parts.append(f"{unknown_count} unknown/flexible")
    if restricted:
        parts.append(f"restricted: {', '.join(restricted)}")
    detail = f" ({'; '.join(parts)})" if parts else ""
    if total <= 0 and restricted:
        return f"{label}: only restricted sources{detail}"
    total_label = "general" if restricted else "total"
    return f"{label}: {total} {total_label}{detail}"


def card_has_type(obj: dict | None, metadata: dict | None, arena_type: str, type_number: int) -> bool:
    """Check a card type using live object enums first, then card DB metadata."""
    if obj and arena_type in set(obj.get("cardTypes") or []):
        return True
    return bool(metadata and type_number in set(metadata.get("type_numbers") or []))


CARD_TYPE_NUMBERS = {
    "CardType_Artifact": 1,
    "CardType_Creature": 2,
    "CardType_Enchantment": 3,
    "CardType_Land": 5,
    "CardType_Planeswalker": 8,
    "CardType_Battle": 12,
}


def activated_ability_lock_types(metadata: dict | None) -> set[str]:
    """Return card types whose activated abilities are stopped by static text."""
    locked = set()
    for text in (metadata or {}).get("ability_texts") or []:
        lower = text.casefold()
        if "activated abilities of" not in lower or "can't be activated" not in lower:
            continue
        if "artifacts" in lower:
            locked.add("CardType_Artifact")
        if "creatures" in lower:
            locked.add("CardType_Creature")
        if "planeswalkers" in lower:
            locked.add("CardType_Planeswalker")
    return locked


def object_matches_any_card_type(
    obj: dict | None,
    metadata: dict | None,
    arena_types: set[str],
) -> bool:
    """Return true when an object has any of the requested card types."""
    return any(
        card_has_type(obj, metadata, arena_type, CARD_TYPE_NUMBERS[arena_type])
        for arena_type in arena_types
        if arena_type in CARD_TYPE_NUMBERS
    )


def card_is_land(obj: dict | None, metadata: dict | None) -> bool:
    """Return true when a card is known to be a land."""
    return card_has_type(obj, metadata, "CardType_Land", CARD_TYPE_NUMBERS["CardType_Land"])


def card_is_nonland_permanent(obj: dict | None, metadata: dict | None) -> bool:
    """Return true for known nonland permanent card types."""
    permanent_checks = (
        ("CardType_Artifact", CARD_TYPE_NUMBERS["CardType_Artifact"]),
        ("CardType_Creature", CARD_TYPE_NUMBERS["CardType_Creature"]),
        ("CardType_Enchantment", CARD_TYPE_NUMBERS["CardType_Enchantment"]),
        ("CardType_Planeswalker", CARD_TYPE_NUMBERS["CardType_Planeswalker"]),
        ("CardType_Battle", CARD_TYPE_NUMBERS["CardType_Battle"]),
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


def phrase_keyword_choice_value(value: int, decoded_value: str | None) -> str:
    """Render Arena StaticList_Keywords choices without hiding unknown ids."""
    if decoded_value:
        return decoded_value
    return f"unknown keyword {value}"


def phrase_keyword_choice_values(values: list[int], keyword_names: dict[int, str]) -> str:
    """Render one or more keyword choice ids in deterministic transcript style."""
    return "; ".join(phrase_keyword_choice_value(value, keyword_names.get(value)) for value in values)


def phrase_incomplete_game_notice(postgame_hint: str | None = None) -> list[str]:
    """Explain that Arena returned postgame data without final GRE results."""
    lines = [
        "Game appears to have ended, but no final Game Rules Engine (GRE) result was written to Player.log."
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
        "number": number, "match_id": match_id, "match_number": number,
        "game_index": 1, "lines": lines,
        "events": [], "debug_hits": [],
        "debug_seen_objects": set(),
        "choice_events": [], "target_events": [], "trigger_events": [],
        "library_events": [],
        "object_history": defaultdict(list),
        "has_result": False, "saw_postgame_payload": False,
        "postgame_hint": None, "finalized": False,
    }


def default_archive_db_path() -> Path:
    """Return the default archive database path in the current working directory."""
    return Path("mtga_games.sqlite3")


def cleaned_transcript_lines(lines: list[str]) -> list[str]:
    """Apply all transcript cleanup and compaction passes in display order."""
    return remove_redundant_match_winner_lines(
        combine_adjacent_attack_lines(
            combine_duplicate_transcript_lines(
                remove_repeated_cast_blocks(
                    remove_near_duplicate_cast_lines(
                        move_copy_resolves_after_revealed_returns(
                            combine_repeated_damage_life_sequences(lines)
                        )
                    )
                )
            )
        )
    )


def is_placeholder_transcript_lines(lines: list[str]) -> bool:
    """Return true for Arena shell records that are not real games."""
    return any("Seat None" in line or "Team None" in line for line in lines)


def transcript_with_game_header(lines: list[str], number: int, match_id: str) -> list[str]:
    """Return transcript lines with a stable archive game header for the given match."""
    header = f"===== GAME {number}: MATCH {match_id} ====="
    if lines and lines[0].startswith("===== GAME ") and f"MATCH {match_id}" in lines[0]:
        return [header, *lines[1:]]
    return [header, *lines]


def transcript_has_state_blocks(lines: list[str] | str) -> bool:
    """Return whether a transcript contains full player board-state snapshots."""
    text = lines if isinstance(lines, str) else "\n".join(lines)
    return bool(
        re.search(
            r"^(?:My|Opponent's|Player \d+'s) board:$",
            text,
            re.MULTILINE,
        )
    )


def transcript_is_reduced(lines: list[str] | str) -> bool:
    """Detect action-only output from parsing with turn-state snapshots disabled."""
    text = lines if isinstance(lines, str) else "\n".join(lines)
    return not transcript_has_state_blocks(text) and bool(
        re.search(r"^=== Turn \d+:", text, re.MULTILINE)
    )


def transcript_version_hash(content: str) -> str:
    """Hash immutable gameplay content without mutable archive display numbering."""
    lines = content.splitlines()
    if lines and lines[0].startswith("===== GAME "):
        lines = lines[1:]
    normalized = "\n".join(lines)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def ensure_archive_schema(con) -> None:
    """Create or migrate the SQLite archive schema to the version supported by this program."""
    current_version = con.execute("PRAGMA user_version").fetchone()[0]
    if current_version > ARCHIVE_SCHEMA_VERSION:
        raise ValueError(
            f"Archive database schema version {current_version} is newer than "
            f"this program supports ({ARCHIVE_SCHEMA_VERSION})."
        )

    legacy_games = []
    existing_tables = {
        row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "games" in existing_tables:
        columns = {row[1] for row in con.execute("PRAGMA table_info(games)")}
        if "transcript" in columns:
            legacy_games = list(
                con.execute(
                    """
                    SELECT
                        match_id,
                        COALESCE(archive_index, game_number),
                        1,
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
        elif current_version < 2 and "match_id" in columns and "archive_index" in columns:
            legacy_games = list(
                con.execute(
                    """
                    SELECT
                        g.match_id,
                        g.archive_index,
                        1,
                        g.game_type,
                        g.has_result,
                        t.line_count,
                        t.content,
                        g.first_seen_at,
                        g.last_seen_at
                    FROM games g
                    JOIN transcripts t ON t.game_id = g.id AND t.format = 'plain_text'
                    ORDER BY g.archive_index, g.first_seen_at
                    """
                )
            )
            con.execute("ALTER TABLE games RENAME TO games_legacy_v1")
            if "transcripts" in existing_tables:
                con.execute("ALTER TABLE transcripts RENAME TO transcripts_legacy_v1")

    con.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY,
            match_id TEXT NOT NULL UNIQUE,
            archive_index INTEGER NOT NULL UNIQUE,
            first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY,
            match_row_id INTEGER NOT NULL,
            game_index INTEGER NOT NULL,
            game_archive_index INTEGER NOT NULL UNIQUE,
            game_type TEXT,
            has_result INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(match_row_id, game_index),
            FOREIGN KEY(match_row_id) REFERENCES matches(id) ON DELETE CASCADE
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
        CREATE TABLE IF NOT EXISTS transcript_versions (
            id INTEGER PRIMARY KEY,
            game_id INTEGER NOT NULL,
            format TEXT NOT NULL DEFAULT 'plain_text',
            parser_version TEXT,
            line_count INTEGER NOT NULL,
            content_sha256 TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(game_id, format, content_sha256),
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
    con.execute("CREATE INDEX IF NOT EXISTS matches_v2_archive_index_idx ON matches(archive_index)")
    con.execute("CREATE INDEX IF NOT EXISTS games_v2_match_row_id_idx ON games(match_row_id)")
    con.execute("CREATE INDEX IF NOT EXISTS games_v2_archive_index_idx ON games(game_archive_index)")
    con.execute("CREATE INDEX IF NOT EXISTS transcripts_v2_game_id_idx ON transcripts(game_id)")
    con.execute(
        "CREATE INDEX IF NOT EXISTS transcript_versions_v3_game_id_idx "
        "ON transcript_versions(game_id)"
    )

    for (
        match_id,
        archive_index,
        game_index,
        game_type,
        has_result,
        line_count,
        transcript,
        first_seen_at,
        last_seen_at,
    ) in legacy_games:
        con.execute(
            """
            INSERT OR IGNORE INTO matches (
                match_id, archive_index, first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (match_id, archive_index, first_seen_at, last_seen_at),
        )
        match_row_id = con.execute(
            "SELECT id FROM matches WHERE match_id = ?",
            (match_id,),
        ).fetchone()[0]
        con.execute(
            """
            INSERT OR IGNORE INTO games (
                match_row_id, game_index, game_archive_index, game_type,
                has_result, first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match_row_id,
                game_index,
                archive_index,
                game_type,
                has_result,
                first_seen_at,
                last_seen_at,
            ),
        )
        game_id = con.execute(
            "SELECT id FROM games WHERE match_row_id = ? AND game_index = ?",
            (match_row_id, game_index),
        ).fetchone()[0]
        con.execute(
            """
            INSERT OR REPLACE INTO transcripts (
                game_id, format, parser_version, line_count, content, created_at, updated_at
            )
            VALUES (?, 'plain_text', NULL, ?, ?, ?, ?)
            """,
            (game_id, line_count, transcript, first_seen_at, last_seen_at),
        )
    # Seed immutable history from every canonical transcript in older schemas.
    # INSERT OR IGNORE makes repeated schema checks safe and append-only.
    for game_id, transcript_format, parser_version, line_count, content, created_at in con.execute(
        """
        SELECT game_id, format, parser_version, line_count, content, created_at
        FROM transcripts
        ORDER BY id
        """
    ):
        con.execute(
            """
            INSERT OR IGNORE INTO transcript_versions (
                game_id, format, parser_version, line_count,
                content_sha256, content, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                game_id,
                transcript_format,
                parser_version,
                line_count,
                transcript_version_hash(content),
                content,
                created_at,
            ),
        )
    for table in ("transcripts", "transcript_versions"):
        con.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_immutable_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} records are immutable');
            END
            """
        )
        con.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_immutable_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} records are immutable');
            END
            """
        )
    con.execute(f"PRAGMA user_version = {ARCHIVE_SCHEMA_VERSION}")
    con.commit()


def archive_seen_games(
    db_path: Path,
    matches: list[dict],
    log_paths: list[Path] | None = None,
) -> tuple[int, int, int]:
    """Store parsed transcripts and return inserted/refreshed/current archive counts."""
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

    records = []
    game_index_counts = Counter()
    for match in matches:
        if is_placeholder_transcript_lines(match["lines"]):
            continue
        match_id = match["match_id"]
        if match.get("game_index") is None:
            game_index_counts[match_id] += 1
            game_index = game_index_counts[match_id]
        else:
            game_index = int(match["game_index"])
        records.append((match, game_index))

    match_ids = sorted({match["match_id"] for match, _game_index in records})
    existing_matches = {}
    if match_ids:
        existing_matches = {
            match_id: (match_row_id, archive_index)
            for match_row_id, match_id, archive_index in con.execute(
                "SELECT id, match_id, archive_index FROM matches WHERE match_id IN (%s)"
                % ",".join("?" for _ in match_ids),
                match_ids,
            )
        }
    existing_games = {}
    if match_ids:
        existing_games = {
            (match_id, game_index): (game_id, game_archive_index)
            for game_id, match_id, game_index, game_archive_index in con.execute(
                """
                SELECT g.id, m.match_id, g.game_index, g.game_archive_index
                FROM games g
                JOIN matches m ON m.id = g.match_row_id
                WHERE m.match_id IN (%s)
                """
                % ",".join("?" for _ in match_ids),
                match_ids,
            )
        }
    next_match_archive_index = (
        con.execute("SELECT COALESCE(MAX(archive_index), 0) + 1 FROM matches").fetchone()[0]
    )
    next_game_archive_index = (
        con.execute("SELECT COALESCE(MAX(game_archive_index), 0) + 1 FROM games").fetchone()[0]
    )

    inserted = 0
    updated = 0
    refreshed_game_rows = []
    for match, game_index in records:
        match_id = match["match_id"]
        if match_id in existing_matches:
            match_row_id, _match_archive_index = existing_matches[match_id]
        else:
            match_archive_index = next_match_archive_index
            next_match_archive_index += 1
            con.execute(
                """
                INSERT INTO matches (match_id, archive_index)
                VALUES (?, ?)
                """,
                (match_id, match_archive_index),
            )
            match_row_id = con.execute(
                "SELECT id FROM matches WHERE match_id = ?",
                (match_id,),
            ).fetchone()[0]
            existing_matches[match_id] = (match_row_id, match_archive_index)

        game_key = (match_id, game_index)
        if game_key in existing_games:
            game_id, game_archive_index = existing_games[game_key]
            updated += 1
        else:
            game_archive_index = next_game_archive_index
            next_game_archive_index += 1
            game_id = None
            inserted += 1

        lines = cleaned_transcript_lines(match["lines"])
        if game_id is not None and not transcript_has_state_blocks(lines):
            existing_transcript = con.execute(
                """
                SELECT content
                FROM transcripts
                WHERE game_id = ? AND format = 'plain_text'
                """,
                (game_id,),
            ).fetchone()
            if existing_transcript and transcript_has_state_blocks(existing_transcript[0]):
                # Presentation flags such as --no-turn-state must never replace
                # a richer archived replay with a reduced transcript.
                lines = existing_transcript[0].splitlines()
        transcript_format = "plain_text_reduced" if transcript_is_reduced(lines) else "plain_text"
        # Stored rows retain their original header forever. Archive display
        # numbers are rewritten only in memory when a transcript is read.
        lines = transcript_with_game_header(lines, game_archive_index, match_id)
        game_type = next((line for line in lines if line.startswith("Game type:")), None)
        transcript = "\n".join(lines)
        con.execute(
            """
            INSERT INTO games (
                match_row_id, game_index, game_archive_index, game_type, has_result
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(match_row_id, game_index) DO UPDATE SET
                game_archive_index=excluded.game_archive_index,
                game_type=excluded.game_type,
                has_result=excluded.has_result,
                last_seen_at=CURRENT_TIMESTAMP
            """,
            (
                match_row_id,
                game_index,
                game_archive_index,
                game_type,
                1 if match.get("has_result") else 0,
            ),
        )
        if game_id is None:
            game_id = con.execute(
                "SELECT id FROM games WHERE match_row_id = ? AND game_index = ?",
                (match_row_id, game_index),
            ).fetchone()[0]
            existing_games[game_key] = (game_id, game_archive_index)
        refreshed_game_rows.append(
            {
                "game_id": game_id,
                "match_id": match_id,
                "lines": lines,
                "format": transcript_format,
            }
        )
        con.execute(
            """
            INSERT INTO transcripts (
                game_id, format, parser_version, line_count, content
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(game_id, format) DO NOTHING
            """,
            (game_id, transcript_format, ARCHIVE_SCHEMA_VERSION, len(lines), transcript),
        )
        con.execute(
            """
            INSERT OR IGNORE INTO transcript_versions (
                game_id, format, parser_version, line_count,
                content_sha256, content
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                game_id,
                transcript_format,
                ARCHIVE_SCHEMA_VERSION,
                len(lines),
                transcript_version_hash(transcript),
                transcript,
            ),
        )
        con.execute(
            "UPDATE matches SET last_seen_at = CURRENT_TIMESTAMP WHERE id = ?",
            (match_row_id,),
        )

    if refreshed_game_rows:
        # Archive rows are normally append-only, but default path discovery can
        # later add older rotated logs after newer current logs. Re-promote the
        # games seen in this parse to the end in parse order so --last N follows
        # the actual current log order instead of stale insertion order.
        #
        # Imported archives can already occupy the target tail indexes. Move
        # every game to a temporary negative index first, then compact games not
        # seen in this parse and finally append the refreshed rows. That keeps
        # game_archive_index unique while still making the current logs define
        # the end of the archive.
        refreshed_ids = [row["game_id"] for row in refreshed_game_rows]
        refreshed_id_set = set(refreshed_ids)
        all_game_ids = [
            row[0]
            for row in con.execute(
                "SELECT id FROM games ORDER BY game_archive_index, id"
            )
        ]
        total_after_refresh = len(all_game_ids)
        temporary_index = -(total_after_refresh + len(refreshed_game_rows) + 1000)
        for offset, game_id in enumerate(all_game_ids):
            con.execute(
                "UPDATE games SET game_archive_index = ? WHERE id = ?",
                (temporary_index - offset, game_id),
            )

        final_index_by_game_id = {}
        next_final_index = 1
        for game_id in all_game_ids:
            if game_id in refreshed_id_set:
                continue
            final_index_by_game_id[game_id] = next_final_index
            next_final_index += 1
        for row in refreshed_game_rows:
            final_index_by_game_id[row["game_id"]] = next_final_index
            next_final_index += 1

        for game_id, final_index in final_index_by_game_id.items():
            con.execute(
                "UPDATE games SET game_archive_index = ? WHERE id = ?",
                (final_index, game_id),
            )
    total = con.execute("SELECT count(*) FROM games").fetchone()[0]
    con.commit()
    con.close()
    return inserted, updated, total


def archived_transcript_matches(db_path: Path) -> list[dict]:
    """Load archived transcripts in stable archive order for normal output selection."""
    con = sqlite3.connect(db_path.expanduser())
    ensure_archive_schema(con)
    rows = list(
        con.execute(
            """
            SELECT
                g.game_archive_index,
                m.match_id,
                m.archive_index,
                g.game_index,
                g.has_result,
                tv.id,
                tv.content
            FROM games g
            JOIN matches m ON m.id = g.match_row_id
            JOIN transcript_versions tv
              ON tv.game_id = g.id AND tv.format = 'plain_text'
            ORDER BY g.game_archive_index, tv.id
            """
        )
    )
    con.close()
    best_rows = {}
    for row in rows:
        game_archive_index, match_id, match_archive_index, game_index, has_result, version_id, transcript = row
        score = (
            1 if transcript_has_state_blocks(transcript) else 0,
            1 if re.search(r"^(?:Winner|Match winner):", transcript, re.MULTILINE) else 0,
            version_id,
        )
        current = best_rows.get(game_archive_index)
        if current is None or score > current[0]:
            best_rows[game_archive_index] = (score, row)

    matches = []
    for game_archive_index in sorted(best_rows):
        (
            _score,
            (
                game_archive_index,
                match_id,
                match_archive_index,
                game_index,
                has_result,
                _version_id,
                transcript,
            ),
        ) = best_rows[game_archive_index]
        lines = transcript_with_game_header(
            transcript.splitlines(),
            game_archive_index,
            match_id,
        )
        if is_placeholder_transcript_lines(lines):
            continue
        matches.append(
            {
                "number": game_archive_index,
                "match_id": match_id,
                "match_number": match_archive_index,
                "game_index": game_index,
                "lines": lines,
                "events": [],
                "debug_hits": [],
                "debug_seen_objects": set(),
                "choice_events": [],
                "target_events": [],
                "trigger_events": [],
                "library_events": [],
                "object_history": defaultdict(list),
                "has_result": bool(has_result),
                "saw_postgame_payload": False,
                "postgame_hint": None,
                "finalized": True,
            }
        )
    return matches


def copy_archive_log_sources(destination, source) -> int:
    """Copy log source metadata from one archive connection into another."""
    rows = list(
        source.execute(
            """
            SELECT path, size_bytes, modified_at, first_seen_at, last_seen_at
            FROM log_sources
            ORDER BY id
            """
        )
    )
    for path, size_bytes, modified_at, first_seen_at, last_seen_at in rows:
        destination.execute(
            """
            INSERT INTO log_sources (
                path, size_bytes, modified_at, first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size_bytes=excluded.size_bytes,
                modified_at=excluded.modified_at,
                last_seen_at=excluded.last_seen_at
            """,
            (path, size_bytes, modified_at, first_seen_at, last_seen_at),
        )
    return len(rows)


def merge_archive_databases(destination_db: Path, source_dbs: list[Path]) -> tuple[int, int, int]:
    """Merge source archive databases into destination, deduping by match id and game index."""
    destination_db = destination_db.expanduser()
    destination_db.parent.mkdir(parents=True, exist_ok=True)
    normalized_destination = destination_db.resolve()

    all_matches = []
    copied_log_sources = 0
    for source_db in source_dbs:
        source_db = source_db.expanduser()
        if not source_db.exists():
            raise FileNotFoundError(f"Archive database does not exist: {source_db}")
        if source_db.resolve() == normalized_destination:
            raise ValueError(f"Cannot merge archive database into itself: {source_db}")

        all_matches.extend(archived_transcript_matches(source_db))
        destination = sqlite3.connect(destination_db)
        source = sqlite3.connect(source_db)
        try:
            ensure_archive_schema(destination)
            ensure_archive_schema(source)
            copied_log_sources += copy_archive_log_sources(destination, source)
            destination.commit()
        finally:
            source.close()
            destination.close()

    inserted, updated, total = archive_seen_games(destination_db, all_matches)
    return inserted, updated, total


def extract_game_plays(
    player_log: Path,
    grp_to_name: dict[int, str],
    *,
    debug_annotations: bool = False,
    debug_grp_ids: set[int] | None = None,
    debug_choices: bool = False,
    debug_targets: bool = False,
    debug_triggers: bool = False,
    debug_library: bool = False,
    debug_objects: set[int] | None = None,
    diagnostics: bool = False,
    nth_from_start: int | None = None,
    last_games: int | None = None,
    first_games: int | None = None,
    nth_from_end: int | None = None,
    game_range: tuple[int, int] | None = None,
    show_progress: bool | None = None,
    show_resolves: bool = False,
    show_turn_state: bool = True,
    show_phases: bool = True,
    live: bool = False,
    live_state_only: bool = False,
    live_state_path: Path | None = None,
    enum_value_names: dict[str, dict[int, str]] | None = None,
    keyword_value_names: dict[int, str] | None = None,
    card_metadata: dict[int, dict] | None = None,
    ability_texts: dict[int, str] | None = None,
    color_mode: str = "never",
    archive_db_path: Path | None = None,
) -> None:
    """Extract a readable play transcript from MTGA Player.log."""
    zones = {}
    objects = {}
    annotation_object_zones = {}
    players = {}
    team_to_seats = {}
    player_names_by_match = {}
    seen_annotations = set()
    seen_combat = set()
    seen_life_changes = set()
    seen_mulligans = set()
    seen_results = set()
    seen_choices = set()
    seen_state_events = set()
    seen_damage_events = set()
    seen_no_combat_damage = set()
    seen_enters_attacking = set()
    seen_revealed_cards = set()
    pending_revealed_cards = []
    remembered_object_labels = {}
    stack_display_names = {}
    emitted_cast_instance_ids = set()
    emitted_cast_line_instance_ids = set()
    ability_trigger_ids = set()
    ability_source_names = {}
    pending_stack_casts = {}
    cast_mana_by_instance = {}
    seen_mana_paid_annotations = set()
    pending_mill_group = None
    pending_life_groups = {}
    pending_death_events = []
    active_effects = {}
    commander_grps_by_seat = {}
    commander_instance_ids = set()
    commander_cast_counts = Counter()
    player_counters = Counter()
    object_counters = defaultdict(Counter)
    keyword_choices_by_source = {}
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
    current_actions = []
    current_phase_section = None
    emitted_phase_section = None
    suppress_phase_heading = False
    current_match_lines = None
    current_match_record = None
    transcript_matches = []
    event_index = 0
    seen_match_ids = set()
    skipping_duplicate_match = False
    live_archived_match_ids = set()
    debug_grp_ids = debug_grp_ids or set()
    debug_objects = debug_objects or set()
    library_authoritative_counts = {}
    library_inferred_counts = {}
    library_snapshot_changes = []
    current_gsm_library_deltas = Counter()
    current_gsm_library_events = []
    pending_keyword_selects = {}
    live_state_caught_up = False
    last_live_state_snapshot = None

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
    keyword_value_names = keyword_value_names or dict(BASE_KEYWORD_VALUE_NAMES)
    card_metadata = card_metadata or {}
    ability_texts = ability_texts or {}
    color_enabled = should_color_output(color_mode, sys.stdout.isatty())
    card_name_colors = build_card_name_colors(card_metadata)
    card_name_pattern = build_card_name_pattern(card_name_colors)
    subtype_names = enum_value_names.get("SubType") or {}
    counter_type_names = enum_value_names.get("CounterType") or {}
    card_type_names = enum_value_names.get("CardType") or {}
    choice_value_names = build_choice_value_names(subtype_names, card_type_names)

    def turn_phase_key(turn_info: dict | None) -> tuple | None:
        """Return the rules boundary where floating mana empties.

        Arena sends player records as diffs. When a later diff omits manaPool,
        a plain dict merge would keep the previous pool even though Magic rules
        empty mana as turns, phases, and steps change. This key lets us clear
        stale floating mana before applying the next Arena diff.
        """
        if not turn_info:
            return None
        return (
            turn_info.get("turnNumber"),
            turn_info.get("phase"),
            turn_info.get("step"),
        )

    def clear_floating_mana_pools() -> None:
        """Forget floating mana at a rules boundary unless Arena re-sends it."""
        for player in players.values():
            player.pop("manaPool", None)

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

    def append_phase_heading(section: str, no_events: bool = False):
        """Append a phase heading directly without recursing through emit()."""
        nonlocal emitted_phase_section
        if not show_phases or suppress_phase_heading or not section:
            return
        if current_match_lines is None:
            return
        if current_match_lines and current_match_lines[-1]:
            current_match_lines.append("")
            if live and not live_state_only:
                print("", flush=True)
        suffix = " (no events)" if no_events else ""
        heading = f"-- {section}{suffix} --"
        current_match_lines.append(heading)
        if live and not live_state_only:
            print(
                colorize_transcript_line(
                    heading,
                    color_enabled,
                    card_name_colors,
                    card_name_pattern,
                ),
                flush=True,
            )
        emitted_phase_section = section

    def emit_phase_transition(section: str | None):
        """Emit quiet phases crossed before the active phase gets a real event.

        Arena reports phase changes before it reports the gameplay actions that
        happen in those phases. If we print the active phase immediately, a
        phase with no transcriptable events looks like a blank section. Instead
        we only print phases that were crossed without events here; the active
        phase heading is printed later by emit_phase_heading_if_needed().
        """
        if not section or not show_phases or suppress_phase_heading:
            return
        if section == emitted_phase_section:
            return
        new_index = PHASE_SECTION_INDEX.get(section)
        old_index = PHASE_SECTION_INDEX.get(emitted_phase_section)
        if new_index is None:
            append_phase_heading(section)
            return
        if old_index is None:
            for skipped in PHASE_SECTION_ORDER[:new_index]:
                append_phase_heading(skipped, no_events=True)
        elif old_index < new_index:
            for skipped in PHASE_SECTION_ORDER[old_index + 1:new_index]:
                append_phase_heading(skipped, no_events=True)

    def set_current_phase_section(section: str | None):
        """Set the active phase and immediately list quiet phases crossed."""
        nonlocal current_phase_section
        new_index = PHASE_SECTION_INDEX.get(section)
        old_index = PHASE_SECTION_INDEX.get(emitted_phase_section)
        if new_index is not None and old_index is not None and new_index < old_index:
            return
        if section == "Cleanup" and current_phase_section != "Cleanup":
            flush_all_pending_stack_casts(section_override=current_phase_section)
        current_phase_section = section
        emit_phase_transition(section)

    def emit_phase_heading_if_needed(line: str):
        """Emit the current phase heading before a delayed event if needed."""
        current_index = PHASE_SECTION_INDEX.get(current_phase_section)
        emitted_index = PHASE_SECTION_INDEX.get(emitted_phase_section)
        if (
            not show_phases
            or suppress_phase_heading
            or not current_phase_section
            or emitted_phase_section == current_phase_section
            or (
                current_index is not None
                and emitted_index is not None
                and current_index < emitted_index
            )
            or not transcript_line_needs_phase_heading(line)
        ):
            return
        append_phase_heading(current_phase_section)

    def emit(line=""):
        """Append a transcript line and print it immediately in live mode."""
        emit_phase_heading_if_needed(line)
        if current_match_lines is not None:
            if (
                live
                and current_match_lines
                and current_match_lines[-1] == line
                and should_combine_transcript_line(line)
            ):
                current_match_lines[-1] = f"2x {line}"
                return
            if (
                live
                and current_match_lines
                and should_combine_transcript_line(line)
            ):
                duplicate_match = re.fullmatch(r"(\d+)x (.+)", current_match_lines[-1])
                if duplicate_match and duplicate_match.group(2) == line:
                    current_match_lines[-1] = f"{int(duplicate_match.group(1)) + 1}x {line}"
                    return
            current_match_lines.append(line)
            if live and not live_state_only:
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

    def is_cleanup_discard_before_new_turn(ann, gsm, new_turn_info):
        """Detect discard-down-to-hand-size events Arena reports with the next turn."""
        if "AnnotationType_ZoneTransfer" not in set(ann.get("type") or []):
            return False
        details = detail_dict(ann.get("details"))
        if details.get("category") != "Discard":
            return False
        if new_turn_info.get("phase") != "Phase_Beginning":
            return False
        affected_ids = ann.get("affectedIds") or []
        instance_id = affected_ids[0] if affected_ids else None
        discarding_seat = zone_owner(details.get("zone_src")) or object_owner(instance_id)
        new_active = new_turn_info.get("activePlayer")
        return discarding_seat is not None and new_active is not None and discarding_seat != new_active

    def emit_cleanup_discards_before_turn_header(gsm, msg, new_turn_info):
        """Emit previous-turn cleanup discards before printing the next turn header."""
        nonlocal current_phase_section
        cleanup_annotations = [
            ann
            for ann in gsm.get("annotations", [])
            if is_cleanup_discard_before_new_turn(ann, gsm, new_turn_info)
        ]
        if not cleanup_annotations:
            return
        for ann in cleanup_annotations:
            for instance_id in ann.get("affectedIds") or []:
                update_pending_stack_cast_name(instance_id, card_label(instance_id))
                flush_pending_stack_cast(instance_id)
        saved_section = current_phase_section
        set_current_phase_section("Cleanup")
        try:
            for ann in cleanup_annotations:
                handle_annotation(ann, gsm, msg)
            flush_pending_event_groups()
        finally:
            current_phase_section = saved_section

    def mark_postgame_payload(root):
        """Remember postgame account/course blobs that appear after GRE stops."""
        if current_match_record is None or current_match_record.get("has_result"):
            return
        if "InventoryInfo" in root or "UpdatedGraphs" in root:
            current_match_record["saw_postgame_payload"] = True
        reason = str(root.get("reason") or root.get("Reason") or "")
        if reason:
            folded_reason = reason.casefold()
            if "closed by remote" in folded_reason and not current_match_record.get("postgame_hint"):
                current_match_record["saw_postgame_payload"] = True
                current_match_record["postgame_hint"] = (
                    "Network log says the match connection was closed by the remote end."
                )
            elif "matchmanager.reset" in folded_reason and not current_match_record.get("postgame_hint"):
                current_match_record["saw_postgame_payload"] = True
                current_match_record["postgame_hint"] = (
                    "Network log says MatchManager reset the match connection."
                )
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

    def archive_live_match_if_complete(record):
        """Persist completed live games as soon as their final result is known."""
        if not live or not archive_db_path or not record or not record.get("has_result"):
            return
        match_id = record.get("match_id")
        if match_id in live_archived_match_ids:
            return
        inserted, updated, total = archive_seen_games(archive_db_path, [record], [player_log])
        live_archived_match_ids.add(match_id)
        action = "recorded" if inserted else "refreshed" if updated else "checked"
        print(
            (
                f"Live archive: {action} match {match_id}. "
                f"Archive now contains {total} total game(s) at {archive_db_path.expanduser()}."
            ),
            file=sys.stderr,
        )

    def warn_live_match_not_archived(record):
        """Explain when live exits before a game is complete enough to archive."""
        if not live or not archive_db_path or not record or record.get("has_result"):
            return
        match_id = record.get("match_id") or "unknown match"
        print(
            (
                f"Warning: live match {match_id} was not recorded in "
                f"{archive_db_path.expanduser()} because no final game result was seen "
                "before live mode stopped."
            ),
            file=sys.stderr,
        )

    def finalize_current_match():
        """Finalize transcript notices and live archive writes for the current match."""
        flush_pending_event_groups()
        if current_match_record is None or current_match_record.get("finalized"):
            return
        current_match_record["finalized"] = True
        if current_match_record.get("has_result"):
            archive_live_match_if_complete(current_match_record)
            return
        if not current_match_record.get("saw_postgame_payload"):
            warn_live_match_not_archived(current_match_record)
            return
        emit("")
        for line in phrase_incomplete_game_notice(current_match_record.get("postgame_hint")):
            emit(line)
        warn_live_match_not_archived(current_match_record)

    def reset_match_state(gsm):
        """Clear state that belongs only to the newly started Arena match."""
        nonlocal current_turn, current_turn_info, last_game_state_id
        nonlocal current_game_has_commanders, known_local_seat, event_index
        nonlocal pending_mill_group, current_phase_section, emitted_phase_section
        nonlocal library_snapshot_changes, current_gsm_library_deltas, current_gsm_library_events
        nonlocal pending_keyword_selects
        nonlocal last_live_state_snapshot

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
            annotation_object_zones,
            players,
            team_to_seats,
            remembered_object_labels,
            stack_display_names,
            ability_source_names,
            pending_stack_casts,
            cast_mana_by_instance,
            pending_life_groups,
            active_effects,
            commander_grps_by_seat,
            player_counters,
            object_counters,
            keyword_choices_by_source,
            persistent_annotations,
            known_targets_by_source,
            known_target_names_by_source,
            known_target_ability_ids_by_source,
        )
        clear_all(
            seen_combat,
            seen_damage_events,
            seen_no_combat_damage,
            seen_enters_attacking,
            seen_revealed_cards,
            pending_revealed_cards,
            seen_mana_paid_annotations,
            emitted_cast_instance_ids,
            emitted_cast_line_instance_ids,
            ability_trigger_ids,
            commander_instance_ids,
            commander_cast_counts,
            pending_death_events,
            library_authoritative_counts,
            library_inferred_counts,
            pending_keyword_selects,
        )
        pending_mill_group = None
        library_snapshot_changes = []
        current_gsm_library_deltas = Counter()
        current_gsm_library_events = []
        last_live_state_snapshot = None

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

    def zone_label(zone_id):
        """Return a short readable zone name for movement transcript lines."""
        zone_type = zones.get(zone_id, {}).get("type")
        labels = {
            "ZoneType_Battlefield": "the battlefield",
            "ZoneType_Command": "command zone",
            "ZoneType_Exile": "exile",
            "ZoneType_Graveyard": "graveyard",
            "ZoneType_Hand": "hand",
            "ZoneType_Library": "library",
            "ZoneType_Revealed": "revealed cards",
            "ZoneType_Stack": "the stack",
        }
        return labels.get(zone_type, clean_arena_enum(zone_type, "ZoneType_") or "unknown zone")

    def instance_zone_type(instance_id):
        """Return an object's current zone type, including sparse diff objects."""
        zone_id = (objects.get(instance_id) or {}).get("zoneId")
        if zone_id is not None:
            return zones.get(zone_id, {}).get("type")
        for zone in zones.values():
            if instance_id in (zone.get("objectInstanceIds") or []):
                return zone.get("type")
        return None

    def is_exile_zone(zone_id):
        """Return true when an Arena zone id currently points at exile."""
        return zones.get(zone_id, {}).get("type") == "ZoneType_Exile"

    def is_stack_or_limbo_zone(zone_id):
        """Return true for transient spell/ability zones used while resolving."""
        return zones.get(zone_id, {}).get("type") in {"ZoneType_Stack", "ZoneType_Limbo"}

    def zone_move_owner(ann, details):
        """Prefer the player whose destination changed for draw/seek/put style events."""
        return owner_label(zone_move_owner_seat(ann, details))

    def zone_move_owner_seat(ann, details):
        """Prefer the raw seat whose destination changed for zone movement events."""
        return (
            zone_owner(details.get("zone_dest"))
            or zone_owner(details.get("zone_src"))
            or object_owner((ann.get("affectedIds") or [None])[0])
            or object_owner(ann.get("affectorId"))
            or current_turn_info.get("activePlayer")
        )

    def visible_to_player(iid, seat):
        """Return true when a moved card's identity is visible enough to print."""
        if seat is None:
            return False
        if owner_label(seat) == "Me":
            return True
        name = card_label_for_snapshot(iid)
        return name not in {"unknown card", "a face-down card"}

    def readable_move_name(iid, category):
        """Hide raw Arena ids for cards whose names are not currently known."""
        name = card_label(iid)
        if (
            not name
            or name in {"unknown card", "a face-down card"}
            or name.startswith(("instance ", "grpId "))
        ):
            if category == "Conjure":
                return "a conjured card"
            if category == "Draft":
                return "a drafted card"
            return "a card"
        return name

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
            return f"Token {subtype}"
        return "Token"

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
        source_zone_type = instance_zone_type(orig_id)
        destination_zone_type = instance_zone_type(new_id)
        carry_cast_marker = should_carry_cast_marker(
            source_zone_type,
            destination_zone_type,
        )
        carry_cast_mana = destination_zone_type in {
            "ZoneType_Stack",
            "ZoneType_Limbo",
            "ZoneType_Battlefield",
        }
        if orig_id in cast_mana_by_instance and carry_cast_mana:
            # Cast mana is learned from legal action messages before the
            # ZoneTransfer CastSpell event. Carry it across the same id changes
            # as pending stack casts.
            cast_mana_by_instance[new_id] = cast_mana_by_instance.pop(orig_id)
        elif orig_id in cast_mana_by_instance:
            cast_mana_by_instance.pop(orig_id, None)
            cast_mana_by_instance.pop(new_id, None)
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
        if orig_id in emitted_cast_instance_ids and carry_cast_marker:
            # Arena may change the stack object's instance id between the
            # hidden cast and the resolve. Preserve the emitted-cast marker so
            # the fallback does not narrate the same spell twice.
            emitted_cast_instance_ids.add(new_id)
        else:
            emitted_cast_instance_ids.discard(new_id)
        if orig_id in emitted_cast_line_instance_ids and carry_cast_marker:
            emitted_cast_line_instance_ids.add(new_id)
        else:
            emitted_cast_line_instance_ids.discard(new_id)

    def card_label(instance_id, fallback_grp_id=None):
        """Return a readable card label for an instance, falling back to grpId."""
        obj = objects.get(instance_id, {})
        return (
            object_label_from_object(obj)
            or remembered_object_labels.get(instance_id)
            or card_name_from_grp(fallback_grp_id)
            or f"instance {instance_id}"
        )

    def record_object_history(instance_id, kind, summary, payload=None):
        """Store compact lifecycle notes for --debug-object instance tracking."""
        if (
            current_match_record is None
            or instance_id is None
            or int(instance_id) not in debug_objects
        ):
            return
        current_match_record["object_history"][int(instance_id)].append(
            {
                "event_index": event_index,
                "turn": current_turn,
                "kind": kind,
                "summary": summary,
                "payload": payload,
            }
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

    def cast_action_for_instance(instance_id):
        """Return Arena's legal cast action for a stack object when still available."""
        for action_wrapper in current_actions:
            action = action_wrapper.get("action") or action_wrapper
            if action.get("actionType") == "ActionType_Cast" and action.get("instanceId") == instance_id:
                return action
        return None

    def cast_mana_effect_note_from_action(action):
        """Summarize Arena mana specs that change how the cast spell behaves."""
        source_names = []
        saw_cant_be_countered = False
        for option in action_mana_payment_options(action):
            specs = mana_spec_types_from_option(option)
            if "ManaSpecType_CantBeCountered" not in specs:
                continue
            saw_cant_be_countered = True
            # This source id comes from Arena's payment record, not card-name
            # guessing. It lets us explain Delighted Halfling, Cavern of Souls,
            # and similar mana abilities with the same generic path.
            for source_id in mana_sources_from_option(option):
                source_name = confident_target_name(source_id) or card_label(source_id)
                if (
                    source_name
                    and source_name not in {"unknown card", "a face-down card"}
                    and not source_name.startswith(("instance ", "grpId "))
                    and source_name not in source_names
                ):
                    source_names.append(source_name)
        if not saw_cant_be_countered:
            return None
        if source_names:
            return f"can't be countered via {'; '.join(source_names)} mana"
        return "can't be countered"

    def append_cast_mana_effect_note(note, effect_note):
        """Add rules-relevant payment notes inside the existing cast suffix."""
        if not effect_note:
            return note
        if note.endswith("]"):
            return f"{note[:-1]}; {effect_note}]"
        return f" [{effect_note}]"

    def remember_cast_mana_actions():
        """Cache legal cast-action mana before the later stack event consumes it."""
        for action_wrapper in current_actions:
            action = action_wrapper.get("action") or action_wrapper
            if action.get("actionType") != "ActionType_Cast":
                continue
            instance_id = action.get("instanceId")
            if instance_id is None:
                continue
            spent_text = format_mana_cost_entries(action.get("manaCost"))
            if not spent_text:
                continue
            seat = (
                action_wrapper.get("seatId")
                or action.get("seatId")
                or object_controller(instance_id)
                or action.get("controllerSeatId")
            )
            available_before = available_mana_count_from_actions(seat) if seat is not None else None
            cast_mana_by_instance[instance_id] = {
                "spent_text": spent_text,
                "cost_amount": mana_cost_amount(action.get("manaCost")),
                "available_before": available_before,
                "seat": seat,
                "name": card_label(instance_id),
                "effect_note": cast_mana_effect_note_from_action(action),
                "payment_from_action": True,
                "mana_source_names": [],
            }

    def note_mana_paid(ann):
        """Recover an opponent's actual payment from ManaPaid annotations."""
        affected = ann.get("affectedIds") or []
        if not affected:
            return
        details = detail_dict(ann.get("details"))
        color = details.get("color")
        symbol = MANA_SYMBOL_LABELS.get(color)
        if not symbol:
            return
        instance_id = affected[0]
        key = (
            current_match,
            ann.get("id"),
            ann.get("affectorId"),
            instance_id,
            color,
            details.get("id"),
        )
        if key in seen_mana_paid_annotations:
            return
        seen_mana_paid_annotations.add(key)

        seat = object_controller(instance_id) or object_owner(instance_id)
        cached = cast_mana_by_instance.setdefault(
            instance_id,
            {
                "spent_text": None,
                "cost_amount": 0,
                "available_before": None,
                "seat": seat,
                "name": card_label(instance_id),
                "effect_note": None,
                "payment_from_action": False,
                "mana_source_names": [],
                "paid_symbols": [],
            },
        )
        if not cached.get("payment_from_action"):
            symbols = cached.setdefault("paid_symbols", [])
            symbols.append(symbol)
            counts = Counter(symbols)
            cached["spent_text"] = "".join(
                mana_symbol * counts[mana_symbol]
                for mana_symbol in ("W", "U", "B", "R", "G", "C")
            )
            cached["cost_amount"] = len(symbols)
            remaining = remaining_mana_count(seat)
            cached["available_before"] = (
                None if remaining is None else remaining + len(symbols)
            )

        source_id = ann.get("affectorId")
        source_obj = objects.get(source_id, {})
        source_name = source_label(source_id)
        if (
            source_name
            and not card_is_land(source_obj, metadata_for_object(source_obj))
            and source_name not in cached["mana_source_names"]
        ):
            cached["mana_source_names"].append(source_name)

    def cached_cast_effect_note(cached):
        """Combine payment restrictions with strategically useful mana sources."""
        notes = []
        if cached.get("effect_note"):
            notes.append(cached["effect_note"])
        source_names = cached.get("mana_source_names") or []
        if source_names:
            notes.append(f"mana via {'; '.join(source_names)}")
        return "; ".join(notes) or None

    def cast_mana_suffix(instance_id, seat, spell_name=None):
        """Describe mana spent and estimated remaining mana for one cast action."""
        obj = objects.get(instance_id, {})
        metadata = metadata_for_object(obj)
        if card_is_land(obj, metadata):
            return ""
        action = cast_action_for_instance(instance_id)
        if action:
            spent_text = format_mana_cost_entries(action.get("manaCost"))
            if spent_text:
                cost_amount = mana_cost_amount(action.get("manaCost"))
                remaining = remaining_mana_count(seat)
                available = None if remaining is None else remaining + cost_amount
                return append_cast_mana_effect_note(
                    format_cast_mana_note(spent_text, cost_amount, available),
                    cast_mana_effect_note_from_action(action),
                )
        cached = cast_mana_by_instance.get(instance_id)
        if cached:
            cached_name = cached.get("name")
            expected_name = base_cast_name(spell_name or "")
            if (
                cached_name
                and expected_name
                and cached_name != expected_name
                and not str(cached_name).startswith(("instance ", "grpId "))
            ):
                return ""
            remaining = remaining_mana_count(seat if seat is not None else cached.get("seat"))
            if remaining is not None:
                return append_cast_mana_effect_note(
                    format_cast_mana_note(
                        cached.get("spent_text"),
                        cached.get("cost_amount", 0),
                        int(cached.get("cost_amount") or 0) + remaining,
                    ),
                    cached_cast_effect_note(cached),
                )
            return append_cast_mana_effect_note(
                format_cast_mana_note(
                    cached.get("spent_text"),
                    cached.get("cost_amount", 0),
                    cached.get("available_before"),
                ),
                cached_cast_effect_note(cached),
            )
        cost_text = format_card_mana_cost((metadata or {}).get("mana_cost"))
        if cost_text:
            available = None
            if seat is not None:
                remaining = remaining_mana_count(seat)
                if remaining is not None:
                    available = remaining + mana_cost_text_amount(cost_text)
            return format_cast_mana_note(cost_text, mana_cost_text_amount(cost_text), available)
        return ""

    def cast_text_with_mana(instance_id, text, seat):
        """Append spell targets, modal text, and Arena-exposed mana spend details."""
        cast_text = cast_text_with_targets(instance_id, text)
        return f"{cast_text}{cast_mana_suffix(instance_id, seat, cast_text)}"

    def source_label(instance_id):
        """Return a source card name only when it is more useful than a raw id."""
        if instance_id is None:
            return None
        # AbilityInstanceCreated annotations can name a later ability object even
        # when the object snapshot is gone by the time ModifiedLife points at it.
        # Check that remembered label before falling back to "instance N".
        if instance_id in remembered_object_labels and not objects.get(instance_id):
            label = remembered_object_labels[instance_id]
            return None if label.startswith("grpId ") else label
        obj = objects.get(instance_id, {})
        if obj.get("type") == "GameObjectType_Ability":
            label = object_label_from_object(obj) or remembered_object_labels.get(instance_id)
            if label and not label.startswith("grpId "):
                return label
            return None
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
        base = token_board_name(base, obj)
        modifier_parts = []
        metadata = metadata_for_object(obj) or {}
        current_power = arena_value_int(obj.get("power"))
        current_toughness = arena_value_int(obj.get("toughness"))
        printed_power = printed_power_toughness_value(metadata.get("power"))
        printed_toughness = printed_power_toughness_value(metadata.get("toughness"))
        power_toughness = power_toughness_summary(obj)
        if power_toughness:
            modifier_parts.append(power_toughness)
        modifier_parts.extend(engine_keyword_summary(obj.get("uniqueAbilities") or [], keyword_value_names))
        counter_parts = counter_summary_parts(
            object_counters[instance_id],
            counter_type_names,
            current_power=current_power,
            current_toughness=current_toughness,
            printed_power=printed_power,
            printed_toughness=printed_toughness,
        )
        modifier_parts.extend(counter_parts)
        effect_modifier = effect_power_toughness_modifier(
            current_power,
            current_toughness,
            printed_power,
            printed_toughness,
            counter_parts,
        )
        if effect_modifier:
            modifier_parts.append(effect_modifier)
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
            card_types = set(obj.get("cardTypes") or [])
            state = "summoning sick" if "CardType_Creature" in card_types else "entered this turn"
            label = f"{label} ({state})"
        return label

    def attachment_kind(attachment_id):
        """Classify an attachment by subtype for player and permanent summaries."""
        attachment = objects.get(attachment_id, {})
        subtypes = set(attachment.get("subtypes") or [])
        if "SubType_Curse" in subtypes:
            return "curse"
        if "SubType_Aura" in subtypes:
            return "aura"
        if "SubType_Equipment" in subtypes:
            return "equipment"
        return "other"

    def attachments_for_permanent(instance_id):
        """Collect auras/equipment explicitly attached to this permanent."""
        attachment_names_by_kind = defaultdict(list)
        for ann in persistent_annotations.values():
            if "AnnotationType_Attachment" not in set(ann.get("type") or []):
                continue
            if instance_id not in (ann.get("affectedIds") or []):
                continue
            attachment_id = ann.get("affectorId")
            name = card_label_for_snapshot(attachment_id)
            if not name or name == "unknown card":
                continue
            attachment_names_by_kind[attachment_kind(attachment_id)].append(name)
        return attachment_names_by_kind

    def attachments_for_player(seat):
        """Collect attachments whose affected object is a player seat."""
        attachment_names_by_kind = defaultdict(list)
        for ann in persistent_annotations.values():
            if "AnnotationType_Attachment" not in set(ann.get("type") or []):
                continue
            if seat not in (ann.get("affectedIds") or []):
                continue
            attachment_id = ann.get("affectorId")
            name = card_label_for_snapshot(attachment_id)
            if not name or name == "unknown card":
                continue
            attachment_names_by_kind[attachment_kind(attachment_id)].append(name)
        return attachment_names_by_kind

    def emblems_for_player(seat):
        """Collect visible emblem-like objects controlled or owned by a player."""
        names = []
        for obj in objects.values():
            if "CardType_Emblem" not in set(obj.get("cardTypes") or []):
                continue
            controller = obj.get("controllerSeatId") or obj.get("ownerSeatId")
            if controller != seat:
                continue
            name = card_label_for_snapshot(obj.get("instanceId"))
            if name and name != "unknown card":
                names.append(name)
        return sorted(set(names))

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

    def mana_restriction_for_source(instance_id):
        """Return a known source-level mana spending restriction."""
        return mana_restriction_from_ability_texts(metadata_for_object(objects.get(instance_id, {})))

    def activated_ability_locked_types():
        """Return card types currently unable to activate abilities."""
        battlefield_zone_ids = zone_ids_by_type("ZoneType_Battlefield")
        locked = set()
        for obj in objects.values():
            if obj.get("zoneId") not in battlefield_zone_ids:
                continue
            locked.update(activated_ability_lock_types(metadata_for_object(obj)))
        return locked

    def mana_source_is_locked(obj, locked_types):
        """Return true when a static effect stops this source's mana ability."""
        if not locked_types:
            return False
        return object_matches_any_card_type(obj, metadata_for_object(obj), locked_types)

    def available_mana_line_from_actions(seat):
        """Summarize currently activatable mana sources from Arena legal actions."""
        total, known_options, unknown_count, restricted = available_mana_snapshot_from_actions(seat)
        return format_available_mana_summary(total, known_options, unknown_count, restricted)

    def available_mana_snapshot_from_actions(seat):
        """Return available mana details from Arena legal mana actions."""
        total = 0
        unknown_count = 0
        known_options = []
        restricted = []
        seen_sources = set()
        locked_types = activated_ability_locked_types()
        for action_wrapper in current_actions:
            action = action_wrapper.get("action") or action_wrapper
            if action.get("actionType") != "ActionType_Activate_Mana":
                continue
            source_id = action.get("instanceId")
            if source_id in seen_sources:
                continue
            source_obj = objects.get(source_id, {})
            controller = source_obj.get("controllerSeatId") or source_obj.get("ownerSeatId")
            if controller != seat:
                continue
            if mana_source_is_locked(source_obj, locked_types):
                continue
            options = action.get("manaPaymentOptions") or []
            if not options:
                continue
            seen_sources.add(source_id)
            best_amount = max(mana_amount_from_payment_option(option) for option in options)
            source_options = set()
            source_restrictions = set()
            for option in options:
                source_options.update(mana_values_from_payment_option(option))
                restriction = mana_restriction_from_specs(option)
                if restriction:
                    source_restrictions.add(restriction)
            text_restriction = mana_restriction_for_source(source_id)
            if text_restriction and source_options != {6}:
                source_restrictions.add(text_restriction)
            if source_restrictions:
                name = card_label_for_snapshot(source_id)
                labels = mana_option_labels(source_options) if source_options else "unknown"
                restricted.append(
                    f"{name} {best_amount} {labels} [{', '.join(sorted(source_restrictions))}]"
                )
                continue
            total += best_amount
            if source_options:
                known_options.append(source_options)
            else:
                unknown_count += best_amount
        return total, known_options, unknown_count, restricted

    def available_mana_count_from_actions(seat):
        """Return a conservative count of mana sources currently exposed as activatable."""
        total, _known_options, unknown_count, restricted = available_mana_snapshot_from_actions(seat)
        restricted_total = 0
        for item in restricted:
            match = re.search(r" (\d+) ", item)
            if match:
                restricted_total += int(match.group(1))
        return total + unknown_count + restricted_total

    def available_mana_line_from_board(seat):
        """Fallback mana summary from untapped permanents controlled by this seat."""
        total, known_options, unknown_count, restricted = available_mana_snapshot_from_board(seat)
        return format_available_mana_summary(total, known_options, unknown_count, restricted)

    def available_mana_snapshot_from_board(seat):
        """Return board-derived mana details from untapped visible permanents."""
        battlefield_zone_ids = zone_ids_by_type("ZoneType_Battlefield")
        total = 0
        unknown_count = 0
        known_options = []
        restricted = []
        locked_types = activated_ability_locked_types()
        for obj in objects.values():
            if obj.get("zoneId") not in battlefield_zone_ids:
                continue
            if obj.get("type") not in {"GameObjectType_Card", "GameObjectType_Token"}:
                continue
            controller = obj.get("controllerSeatId") or obj.get("ownerSeatId")
            if controller != seat or obj.get("isTapped"):
                continue
            if mana_source_is_locked(obj, locked_types):
                continue
            is_land = battlefield_row_for_object(obj) == "lands"
            is_creature = "CardType_Creature" in set(obj.get("cardTypes") or [])
            if is_creature and obj.get("hasSummoningSickness"):
                continue
            name = card_label_for_snapshot(obj.get("instanceId"))
            metadata = metadata_for_object(obj)
            options = land_mana_options(name, metadata) if is_land else mana_options_from_ability_texts(metadata)
            if options == set():
                continue
            if not is_land and not options:
                continue
            restriction = mana_restriction_from_ability_texts(metadata)
            if restriction and options != {6}:
                labels = mana_option_labels(options) if options else "unknown"
                restricted.append(f"{name} 1 {labels} [{restriction}]")
                continue
            total += 1
            if options:
                known_options.append(options)
            else:
                unknown_count += 1
        return total, known_options, unknown_count, restricted

    def available_mana_count_from_board(seat):
        """Return a rough count of untapped visible mana sources on the board."""
        total, _known_options, unknown_count, restricted = available_mana_snapshot_from_board(seat)
        restricted_total = 0
        for item in restricted:
            match = re.search(r" (\d+) ", item)
            if match:
                restricted_total += int(match.group(1))
        return total + unknown_count + restricted_total

    def remaining_mana_count(seat):
        """Estimate currently available mana after an event has updated state."""
        if seat is None:
            return None
        action_count = available_mana_count_from_actions(seat)
        board_count = available_mana_count_from_board(seat)
        floating_count = mana_pool_count(players.get(seat, {}).get("manaPool"))
        return max(action_count, board_count) + floating_count

    def available_mana_line(seat):
        """Prefer Arena legal actions, falling back to board-derived mana sources."""
        return available_mana_line_from_actions(seat) or available_mana_line_from_board(seat)

    def board_row_line(label, row):
        """Render one board row, grouping permanents by tapped state."""
        parts = []
        if row["untapped"]:
            parts.append(f"Untapped: {compact_names(row['untapped'])}")
        if row["tapped"]:
            parts.append(f"Tapped: {compact_names(row['tapped'])}")
        if not parts:
            return None
        return f"  {label}: {'; '.join(parts)}"

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
                name = card_label_for_snapshot(instance_id)
                if name not in {"unknown card", "a face-down card"}:
                    name = f"{name}{card_cost_suffix(metadata_for_object(objects.get(instance_id, {})))}"
                names.append(name)
        return names

    def library_count(seat):
        """Return the current number of objects in a player's library zone."""
        for zone in zones.values():
            if zone.get("type") == "ZoneType_Library" and zone.get("ownerSeatId") == seat:
                return len(zone.get("objectInstanceIds") or [])
        return None

    def note_library_zone_snapshots(gsm):
        """Record authoritative library counts from Arena zone snapshots."""
        if not debug_library:
            return
        for zone in gsm.get("zones", []):
            if zone.get("type") != "ZoneType_Library":
                continue
            seat = zone.get("ownerSeatId")
            if seat is None:
                continue
            count = len(zone.get("objectInstanceIds") or [])
            previous = library_authoritative_counts.get(seat)
            library_authoritative_counts[seat] = count
            if seat not in library_inferred_counts:
                library_inferred_counts[seat] = count
            if previous != count:
                library_snapshot_changes.append(
                    {
                        "kind": "snapshot",
                        "turn": current_turn,
                        "player": owner_label(seat),
                        "player_seat": seat,
                        "event": "Arena zone snapshot",
                        "previous": previous,
                        "new": count,
                        "delta": None if previous is None else count - previous,
                        "gameStateId": gsm.get("gameStateId"),
                    }
                )

    def note_library_zone_transfer(ann, gsm, details, category, affected):
        """Track inferred library movement from ZoneTransfer annotations for debugging."""
        if not debug_library:
            return
        source_zone_id = details.get("zone_src")
        dest_zone_id = details.get("zone_dest")
        source_zone_type = zones.get(source_zone_id, {}).get("type")
        dest_zone_type = zones.get(dest_zone_id, {}).get("type")
        affected_count = len(affected)
        delta = library_delta_for_zone_transfer(
            category,
            source_zone_type,
            dest_zone_type,
            affected_count,
        )
        if delta == 0 and source_zone_type != "ZoneType_Library" and dest_zone_type != "ZoneType_Library":
            return
        seat = None
        if source_zone_type == "ZoneType_Library":
            seat = zone_owner(source_zone_id)
        elif dest_zone_type == "ZoneType_Library":
            seat = zone_owner(dest_zone_id)
        if seat is None:
            seat = zone_move_owner_seat(ann, details)
        name = readable_move_name(affected[0], category) if affected else None
        previous = library_inferred_counts.get(seat)
        if previous is not None and seat is not None:
            previous += current_gsm_library_deltas.get(seat, 0)
        new = previous + delta if previous is not None else None
        if seat is not None and delta:
            current_gsm_library_deltas[seat] += delta
        current_gsm_library_events.append(
            {
                "kind": "movement",
                "turn": current_turn,
                "player": owner_label(seat) if seat is not None else None,
                "player_seat": seat,
                "event": library_movement_description(category, source_zone_type, dest_zone_type),
                "category": category,
                "card": name,
                "affectedIds": list(affected),
                "previous": previous,
                "new": new,
                "delta": delta,
                "sourceZoneId": source_zone_id,
                "destZoneId": dest_zone_id,
                "gameStateId": gsm.get("gameStateId"),
                "annotationId": ann.get("id"),
            }
        )

    def finalize_library_debug_for_gsm(gsm):
        """Apply inferred library deltas and warn when Arena snapshots disagree."""
        if not debug_library or current_match_record is None:
            return
        warnings = []
        for seat, delta in current_gsm_library_deltas.items():
            previous = library_inferred_counts.get(seat)
            if previous is not None:
                library_inferred_counts[seat] = previous + delta
        for zone in gsm.get("zones", []):
            if zone.get("type") != "ZoneType_Library":
                continue
            seat = zone.get("ownerSeatId")
            if seat is None:
                continue
            authoritative = len(zone.get("objectInstanceIds") or [])
            inferred = library_inferred_counts.get(seat)
            corrected, drifted = apply_authoritative_library_count(inferred, authoritative)
            # Arena starts Brawl libraries at deck size, then removes opening
            # hands before the first turn. That setup transition is expected,
            # not parser drift.
            if drifted and current_turn is not None:
                warnings.append(
                    {
                        "kind": "warning",
                        "turn": current_turn,
                        "player": owner_label(seat),
                        "event": "library drift detected; using Arena zone snapshot",
                        "previous": inferred,
                        "new": authoritative,
                        "delta": authoritative - inferred,
                        "gameStateId": gsm.get("gameStateId"),
                    }
                )
            library_inferred_counts[seat] = corrected
            library_authoritative_counts[seat] = authoritative
        records = []
        records.extend(library_snapshot_changes)
        records.extend(current_gsm_library_events)
        records.extend(warnings)
        if records:
            current_match_record["library_events"].append(
                {
                    "event_index": event_index,
                    "turn": current_turn,
                    "gameStateId": gsm.get("gameStateId"),
                    "records": records,
                }
            )
        library_snapshot_changes.clear()
        current_gsm_library_events.clear()
        current_gsm_library_deltas.clear()

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
        flush_pending_battlefield_permanent_casts()
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
        emit_player_attachments(seat)
        emit(f"  {phrase_library_count('Library', library_count(seat))}")
        if current_game_has_commanders:
            emit_optional_zone_line("Command", command_zone_names(seat))
        emit_optional_zone_line("Graveyard", zone_names("ZoneType_Graveyard", seat))
        emit_optional_zone_line("Exile", zone_names("ZoneType_Exile", seat))
        for line in available_resource_lines(available_resources_for_seat(seat)):
            emit(f"  {line}")

    def emit_optional_zone_line(label, names):
        """Print a labelled zone row only when the zone has visible contents."""
        compacted = compact_names(names)
        if compacted != "(empty)":
            emit(f"  {label}: {compacted}")

    def emit_player_attachments(seat):
        """Print attachments such as Curses that are attached directly to a player."""
        parts = attached_to_player_summary_parts(attachments_for_player(seat))
        if parts:
            emit(f"  Attached: {'; '.join(parts)}")
        emblem_line = emblem_summary_line(emblems_for_player(seat))
        if emblem_line:
            emit(f"  {emblem_line}")

    def emit_board_rows(seat):
        """Print board rows in the requested land/noncreature/creature order."""
        rows = battlefield_rows(seat)
        floating_line = format_mana_pool(players.get(seat, {}).get("manaPool"))
        if floating_line:
            emit(f"  {floating_line}")
        for label, row in (
            ("Lands", rows["lands"]),
            ("Artifacts/Enchantments", rows["artifacts_enchantments"]),
            ("Creatures", rows["creatures"]),
        ):
            line = board_row_line(label, row)
            if line:
                emit(line)
                if label == "Lands":
                    mana_line = available_mana_line(seat)
                    if mana_line:
                        emit(f"  {mana_line}")
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

        for source_id, choice in sorted(
            keyword_choices_by_source.items(),
            key=lambda item: item[1]["source"],
        ):
            if not is_battlefield_zone((objects.get(source_id) or {}).get("zoneId")):
                continue
            lines.append(f"{choice['source']} chose {choice['text']} from an Arena keyword prompt")
        return lines

    def live_state_player_label(seat):
        """Return a short, stable player label for LLM-oriented snapshots."""
        owner = owner_label(seat)
        if owner == "Opponent":
            opponent_name = player_names_by_match.get(current_match, {}).get(seat)
            if opponent_name:
                return f"OPP({opponent_name})"
        return {
            "Me": "ME",
            "Opponent": "OPP",
            "Player 1": "P1",
            "Player 2": "P2",
        }.get(owner, f"P{seat}")

    def live_state_named_opponent_text(text):
        """Add the known opponent name to narrative text inside a state block."""
        if known_local_seat not in (1, 2):
            return text
        opponent_seat = 1 if known_local_seat == 2 else 2
        opponent_name = player_names_by_match.get(current_match, {}).get(opponent_seat)
        if not opponent_name:
            return text
        return text.replace("Opponent", f"Opponent ({opponent_name})")

    def live_state_names(names):
        """Render a compact collection while making an empty zone explicit."""
        value = compact_names(names)
        return "-" if value == "(empty)" else value

    def live_state_board_value(row):
        """Render the tapped and untapped halves of one battlefield row."""
        parts = []
        if row["untapped"]:
            parts.append(f"u={{{live_state_names(row['untapped'])}}}")
        if row["tapped"]:
            parts.append(f"t={{{live_state_names(row['tapped'])}}}")
        return ",".join(parts) or "-"

    def build_live_state_snapshot():
        """Build a compact, self-contained current-state block for LLM ingestion."""
        if current_match is None:
            return ()

        active_seat = current_turn_info.get("activePlayer")
        decision_seat = current_turn_info.get("decisionPlayer")
        phase = current_phase_section or phase_section_label(current_turn_info) or "setup"
        phase_key = re.sub(r"[^a-z0-9]+", "_", phase.lower()).strip("_")
        turn_value = current_turn if current_turn is not None else 0
        lines = [
            (
                f"STATE game={current_match_number} turn={turn_value} phase={phase_key} "
                f"active={live_state_player_label(active_seat) if active_seat else '-'} "
                f"priority={live_state_player_label(decision_seat) if decision_seat else '-'}"
            )
        ]

        for seat in turn_state_seat_order():
            label = live_state_player_label(seat)
            player = players.get(seat, {})
            life = player.get("lifeTotal", "?")
            hand = hand_names(seat)
            lines.append(
                f"{label} life={life} hand={len(hand)} {{{live_state_names(hand)}}}"
            )

            rows = battlefield_rows(seat)
            lines.append(
                f"{label} board "
                f"land[{live_state_board_value(rows['lands'])}] "
                f"noncreature[{live_state_board_value(rows['artifacts_enchantments'])}] "
                f"creature[{live_state_board_value(rows['creatures'])}] "
                f"other[{live_state_board_value(rows['other'])}]"
            )

            mana_parts = []
            floating = format_mana_pool(player.get("manaPool"))
            available = available_mana_line(seat)
            if floating:
                mana_parts.append(floating.removeprefix("Floating mana: "))
            if available:
                mana_parts.append(available.removeprefix("Available mana: "))
            lines.append(f"{label} mana={' | '.join(mana_parts) if mana_parts else '-'}")

            lines.append(
                f"{label} zones library={library_count(seat) if library_count(seat) is not None else '?'} "
                f"command={{{live_state_names(command_zone_names(seat))}}} "
                f"graveyard={{{live_state_names(zone_names('ZoneType_Graveyard', seat))}}} "
                f"exile={{{live_state_names(zone_names('ZoneType_Exile', seat))}}}"
            )

            attachment_parts = attached_to_player_summary_parts(attachments_for_player(seat))
            emblem = emblem_summary_line(emblems_for_player(seat))
            if emblem:
                attachment_parts.append(emblem)
            if attachment_parts:
                lines.append(f"{label} attached={{{'; '.join(attachment_parts)}}}")

            resources = available_resources_for_seat(seat)
            resource_cards = resources.get("potential_graveyard_exile_plays") or []
            if resource_cards:
                lines.append(f"{label} resources={{{'; '.join(resource_cards)}}}")

        stack = zone_names("ZoneType_Stack")
        if stack:
            lines.append(f"STACK {{{live_state_names(stack)}}}")
        if active_effects:
            effects = sorted(effect["text"] for effect in active_effects.values())
            lines.append(f"EFFECTS {{{'; '.join(effects)}}}")
        strategy = strategic_state_lines()
        if strategy:
            named_strategy = [live_state_named_opponent_text(line) for line in strategy]
            lines.append(f"NOTES {{{'; '.join(named_strategy)}}}")
        lines.append("END_STATE")
        return tuple(lines)

    def print_live_state_snapshot(force=False):
        """Print one changed state block after the initial live catch-up."""
        nonlocal last_live_state_snapshot
        if not live_state_only or not live_state_caught_up:
            return
        snapshot = build_live_state_snapshot()
        if not snapshot or (not force and snapshot == last_live_state_snapshot):
            return
        rendered = "\n".join(snapshot) + "\n"
        if live_state_path is not None:
            destination = live_state_path.expanduser()
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.tmp")
            temporary.write_text(rendered, encoding="utf-8")
            temporary.replace(destination)
        else:
            if last_live_state_snapshot is not None:
                print(flush=True)
            print(rendered, end="", flush=True)
        last_live_state_snapshot = snapshot

    def flush_pending_stack_cast(instance_id, section_override=None):
        """Emit a delayed speculative cast once Arena confirms it mattered."""
        pending = pending_stack_casts.pop(instance_id, None)
        if pending:
            if instance_id in emitted_cast_instance_ids:
                return
            emitted_cast_instance_ids.add(instance_id)
            emitted_cast_line_instance_ids.add(instance_id)
            text = cast_text_with_mana(
                pending.get("source_id", instance_id),
                pending["text"],
                pending.get("seat"),
            )
            emit_in_phase_section(
                phrase_player_action(pending["owner"], "cast", text),
                section_override if section_override is not None else pending.get("section"),
            )

    def update_pending_stack_cast_name(instance_id, name):
        """Retitle a delayed cast when a later stack update exposes better identity."""
        pending = pending_stack_casts.get(instance_id)
        if not pending or not name or name.startswith("grpId "):
            return
        pending["text"] = replace_base_cast_name(pending["text"], name)
        stack_display_names[instance_id] = name

    def flush_pending_battlefield_permanent_casts():
        """Emit delayed permanent casts once current state confirms battlefield identity."""
        for instance_id in list(pending_stack_casts):
            obj = objects.get(instance_id, {})
            if not is_pending_permanent_cast(instance_id) or not is_battlefield_zone(obj.get("zoneId")):
                continue
            update_pending_stack_cast_name(instance_id, card_label(instance_id))
            flush_pending_stack_cast(instance_id)

    def is_pending_permanent_cast(instance_id):
        """Return true for delayed casts whose resolving object should enter the battlefield."""
        obj = objects.get(instance_id, {})
        card_types = set(obj.get("cardTypes") or [])
        return bool(card_types & {
            "CardType_Artifact",
            "CardType_Battle",
            "CardType_Creature",
            "CardType_Enchantment",
            "CardType_Planeswalker",
        })

    def flush_all_pending_stack_casts(section_override=None):
        """Emit delayed cast lines before a game ends without resolving them."""
        for instance_id in list(pending_stack_casts):
            flush_pending_stack_cast(instance_id, section_override=section_override)

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
        emitted_cast_line_instance_ids.add(instance_id)
        emit(phrase_player_action(actor, "cast", cast_text_with_mana(instance_id, name, actor_seat)))

    def emit_missing_effect_source_cast(instance_id):
        """Emit a source spell cast before its generic effect annotation.

        Some adventure and modal spell paths expose the effect annotation before
        the parser has enough information to print the original CastSpell line.
        When the affector is itself a nonland card object and no cast line was
        emitted, this keeps the transcript from showing a spell effect with no
        spell cast. The card name still comes from CARDDB via the object's grpId.
        """
        if instance_id is None:
            return
        obj = objects.get(instance_id)
        name = card_label(instance_id)
        if not obj or obj.get("type") != "GameObjectType_Card":
            return
        if "CardType_Land" in (obj.get("cardTypes") or []):
            return
        if not is_stack_or_limbo_zone(obj.get("zoneId")):
            return
        if obj.get("isCopy"):
            return
        if not should_emit_resolve_line(name, instance_id):
            return
        if current_match_record is not None:
            cast_needle = f" casts {name}"
            if any(cast_needle in line for line in current_match_record.get("lines", [])):
                emitted_cast_line_instance_ids.add(instance_id)
                return
        elif instance_id in emitted_cast_line_instance_ids:
            return
        actor_seat = object_controller(instance_id) or object_owner(instance_id)
        if not actor_seat:
            return
        emitted_cast_instance_ids.add(instance_id)
        emitted_cast_line_instance_ids.add(instance_id)
        pending_stack_casts.pop(instance_id, None)
        emit(phrase_player_action(owner_label(actor_seat), "cast", cast_text_with_mana(instance_id, name, actor_seat)))

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

    def flush_pending_revealed_cards():
        """Emit reveals that were not merely the public prelude to a cast."""
        if not pending_revealed_cards:
            return
        events = list(pending_revealed_cards)
        pending_revealed_cards.clear()
        for event in events:
            emit_in_phase_section(
                phrase_player_action(event["owner"], "reveal", event["name"]),
                event.get("section"),
            )

    def suppress_matching_pending_reveal(owner, name):
        """Drop Arena's synthetic reveal when that same card is immediately cast."""
        for index in range(len(pending_revealed_cards) - 1, -1, -1):
            event = pending_revealed_cards[index]
            if event["owner"] == owner and event["name"] == name:
                pending_revealed_cards.pop(index)
                return True
        return False

    def flush_pending_event_groups():
        """Flush grouped event summaries before emitting an unrelated event."""
        flush_pending_revealed_cards()
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

        note_library_zone_transfer(ann, gsm, details, category, affected)
        iid = affected[0]
        owner = event_owner(ann, details)
        detail_name = card_name_from_grp(details.get("grpid"))
        name = preferred_zone_transfer_name(detail_name, card_label(iid))
        from_command = is_command_zone(details.get("zone_src"))

        # Returning a known opponent card to a hidden hand creates a synthetic
        # RevealedCard object.  The Return line already explains that movement;
        # do not narrate the visibility bookkeeping as a separate reveal.
        if category == "Return":
            suppress_matching_pending_reveal(owner, name)

        if category not in {"CastSpell", "Resolve", "Copy"} and iid in pending_stack_casts:
            flush_pending_event_groups()
            update_pending_stack_cast_name(iid, name)
            flush_pending_stack_cast(iid)

        if category == "PlayLand":
            flush_pending_event_groups()
            # Cards cast or played from exile can be owned by one player and
            # controlled by another. Prefer controller for the actor so Gonti
            # and similar effects do not attribute stolen-card casts to the
            # original owner.
            actor = owner_label(object_controller(iid)) if object_controller(iid) else owner
            emit(phrase_player_action(actor, "play", name))
        elif category == "CastSpell":
            pending_stack_casts.pop(iid, None)
            stack_display_names[iid] = name
            if iid in emitted_cast_instance_ids:
                return
            suffix = " from command zone" if from_command else ""
            if from_command:
                commander_text = note_commander_cast(iid, details.get("grpid"))
                if commander_text:
                    suffix = f"{suffix}; {commander_text}"
            cast_base_text = f"{name}{suffix}"
            cast_text = cast_text_with_targets(iid, cast_base_text)
            # See PlayLand above: the caster is the current controller, not
            # necessarily the owner of the source zone or card.
            caster_seat = object_controller(iid)
            owner = owner_label(caster_seat) if caster_seat else owner
            suppress_matching_pending_reveal(owner, name)
            flush_pending_event_groups()
            if is_low_fidelity_update_without_turn(gsm) or cast_text == cast_base_text:
                # TargetSpec annotations can arrive just after CastSpell in
                # the same stack sequence. Delay targetless cast lines until
                # the spell resolves or produces an effect, then re-check
                # TargetSpec so cards like Fading Hope can name their target.
                pending_stack_casts[iid] = {
                    "owner": owner,
                    "seat": caster_seat,
                    "text": cast_base_text,
                    "source_id": iid,
                    "section": current_phase_section,
                }
            else:
                emitted_cast_instance_ids.add(iid)
                emitted_cast_line_instance_ids.add(iid)
                emit(phrase_player_action(owner, "cast", f"{cast_text}{cast_mana_suffix(iid, caster_seat, cast_text)}"))
        elif category == "Resolve":
            flush_pending_event_groups()
            adventure_name = stack_display_names.get(iid)
            card_name_before_resolve = card_label(iid)
            if detail_name and not detail_name.startswith("grpId "):
                update_pending_stack_cast_name(iid, detail_name)
            else:
                name = resolve_stack_name(iid, name, stack_display_names)
            if not is_pending_permanent_cast(iid):
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
                emitted_cast_line_instance_ids.add(iid)
                actor_seat = object_controller(iid)
                emit(phrase_player_action(actor, "cast", cast_text_with_mana(iid, name, actor_seat)))
            effect_text = active_effect_for_resolved_permanent(name, owner)
            if effect_text:
                add_active_effect(("resolved_effect", iid, name), effect_text, source_id=iid)
            if show_resolves and should_emit_resolve_line(name, iid):
                # Anonymous stack objects are usually triggered/copy ability
                # bookkeeping. Emitting raw ids is less useful than silence.
                emit(f"{name} resolves")
            if is_exile_zone(details.get("zone_dest")):
                # Adventure spells use a separate stack grpId/name, then Arena
                # changes the resolving object to the parent card in exile.
                # If we carried a distinct stack name across ObjectIdChanged,
                # explain that exile movement without hard-coding card names.
                line = adventure_exile_line(card_name_before_resolve, adventure_name)
                if line:
                    emit(line)
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
            source = source_label(ann.get("affectorId"))
            if source == name or ann.get("affectorId") == iid:
                source = None
            emit(phrase_sacrifice(source, owner, name))
            remove_active_effects_for_source(iid)
        elif category == "Mill":
            source = mill_source_from_affector(ann.get("affectorId"))
            mill_owner = owner_label(zone_owner(details.get("zone_src")) or object_owner(iid))
            if not add_pending_mill(source, mill_owner):
                flush_pending_event_groups()
        elif category == "Draw":
            flush_pending_event_groups()
            seat = zone_owner(details.get("zone_dest")) or object_owner(iid)
            label = owner_label(seat)
            visible = visible_to_player(iid, seat)
            emit(phrase_draw(label, readable_move_name(iid, category), visible))
        elif category in {"Put", "Seek"}:
            flush_pending_event_groups()
            source = source_label(ann.get("affectorId"))
            label = zone_move_owner(ann, details)
            verb = "seek" if category == "Seek" else "put"
            emit(
                phrase_move_to_zone(
                    source,
                    label,
                    verb,
                    readable_move_name(iid, category),
                    zone_label(details.get("zone_dest")),
                    "into",
                )
            )
        elif category == "Return":
            flush_pending_event_groups()
            source = source_label(ann.get("affectorId"))
            label = zone_move_owner(ann, details)
            emit(
                phrase_move_to_zone(
                    source,
                    label,
                    "return",
                    readable_move_name(iid, category),
                    zone_label(details.get("zone_dest")),
                )
            )
        elif category == "Conjure":
            flush_pending_event_groups()
            source = source_label(ann.get("affectorId"))
            label = zone_move_owner(ann, details)
            emit(
                phrase_move_to_zone(
                    source,
                    label,
                    "conjure",
                    readable_move_name(iid, category),
                    zone_label(details.get("zone_dest")),
                    "into",
                )
            )
        elif category == "Surveil":
            flush_pending_event_groups()
            source = source_label(ann.get("affectorId"))
            label = zone_move_owner(ann, details)
            emit(
                phrase_source_action(
                    source,
                    phrase_player_action(
                        label,
                        "surveil",
                        f"{readable_move_name(iid, category)} into {zone_label(details.get('zone_dest'))}",
                    ),
                )
            )
        elif category == "Draft":
            flush_pending_event_groups()
            source = source_label(ann.get("affectorId"))
            label = zone_move_owner(ann, details)
            emit(
                phrase_source_action(
                    source,
                    phrase_player_action(label, "draft", readable_move_name(iid, category)),
                )
            )
        elif category == "SBA_Commander":
            flush_pending_event_groups()
            emit(f"{name} moves to command zone")
        elif category == "nil":
            flush_pending_event_groups()
            emit(f"{name} moves to {zone_label(details.get('zone_dest'))}")

    def emit_life_change(ann, gsm):
        """Emit life gain/loss lines from ModifiedLife annotations."""
        flush_pending_cast_for_affector(ann.get("affectorId"))
        details = detail_dict(ann.get("details"))
        delta = details.get("life")
        affected = ann.get("affectedIds") or []
        if not affected or not isinstance(delta, int) or delta == 0:
            return

        seat = affected[0]
        game_state_id = gsm.get("gameStateId")

        def life_key(life_ann, life_seat, life_delta):
            """Build a de-duplication key for one life-change annotation."""
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

    def emit_power_toughness_mod(ann, gsm):
        """Emit source-attributed temporary/permanent P/T modifications.

        Arena exposes many spell effects as generic PowerToughnessModCreated
        annotations. Using the affector and affected ids here is more robust
        than card-name special cases, and also gives delayed cast lines a
        confident target before the spell resolves.
        """
        details = detail_dict(ann.get("details"))
        power = details.get("power")
        toughness = details.get("toughness")
        affected = ann.get("affectedIds") or []
        affector = ann.get("affectorId")
        if not affected or not isinstance(power, int) or not isinstance(toughness, int):
            return
        target_id = affected[0]
        if affector is not None and target_id not in known_targets_by_source.get(affector, []):
            known_targets_by_source[affector].append(target_id)
            target_name = confident_target_name(target_id)
            if target_name:
                known_target_names_by_source[affector].append(target_name)
        flush_pending_event_groups()
        flush_pending_cast_for_affector(affector)
        emit_missing_effect_source_cast(affector)
        emit(phrase_power_toughness_mod(source_label(affector), card_label(target_id), power, toughness))

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

    def note_keyword_select_request(msg):
        """Remember Arena keyword prompts so later SelectNResp ids can be decoded."""
        if msg.get("type") != "GREMessageType_SelectNReq":
            return
        select_req = msg.get("selectNReq") or {}
        if select_req.get("staticList") != "StaticList_Keywords":
            return
        response_id = msg.get("msgId")
        source_id = select_req.get("sourceId")
        if response_id is None or source_id is None:
            return
        options = [value for value in select_req.get("ids") or [] if isinstance(value, int)]
        hot_options = [value for value in select_req.get("hotIds") or [] if isinstance(value, int)]
        prompt = msg.get("prompt") or {}
        prompt_parameters = prompt.get("parameters") or []
        prompt_card_id = None
        for parameter in prompt_parameters:
            if parameter.get("parameterName") == "CardId":
                prompt_card_id = parameter.get("numberValue")
                break
        # Store the resolved label at prompt time because the source object can
        # leave the stack before the client response is logged.
        pending_keyword_selects[response_id] = {
            "gameStateId": msg.get("gameStateId"),
            "sourceId": source_id,
            "sourceName": card_label(source_id),
            "sourceOwner": object_owner(source_id),
            "options": options,
            "hotOptions": hot_options,
            "promptCardId": prompt_card_id,
        }
        if debug_choices and current_match_record is not None:
            current_match_record["choice_events"].append(
                {
                    "event_index": event_index,
                    "turn": current_turn,
                    "gameStateId": msg.get("gameStateId"),
                    "payload": {"greToClientMessage": msg},
                }
            )

    def emit_keyword_select_response(root):
        """Emit decoded keyword choices from ClientMessageType_SelectNResp blocks."""
        payload = root.get("payload") or {}
        if payload.get("type") != "ClientMessageType_SelectNResp":
            return
        response_id = payload.get("respId")
        prompt = pending_keyword_selects.pop(response_id, None)
        if not prompt:
            return
        select_resp = payload.get("selectNResp") or {}
        selected_values = [
            value for value in select_resp.get("ids") or [] if isinstance(value, int)
        ]
        if not selected_values:
            return
        source_id = prompt.get("sourceId")
        source = prompt.get("sourceName") or card_label(source_id)
        chooser = owner_label(prompt.get("sourceOwner") or object_owner(source_id))
        value_text = phrase_keyword_choice_values(selected_values, keyword_value_names)
        keyword_choices_by_source[source_id] = {
            "source": source,
            "chooser": chooser,
            "values": tuple(selected_values),
            "text": value_text,
        }
        key = (
            current_match,
            payload.get("gameStateId"),
            response_id,
            source_id,
            tuple(selected_values),
        )
        if key in seen_choices:
            return
        seen_choices.add(key)
        flush_pending_event_groups()
        emit(
            phrase_player_action(
                chooser,
                "choose",
                f"{value_text} for {source} (keyword)",
                third_person="chooses",
            )
        )
        if debug_choices and current_match_record is not None:
            current_match_record["choice_events"].append(
                {
                    "event_index": event_index,
                    "turn": current_turn,
                    "gameStateId": payload.get("gameStateId"),
                    "payload": root,
                }
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

            if obj.get("blockState") == "BlockState_Blocking":
                attacker_ids = (obj.get("blockInfo") or {}).get("attackerIds") or []
                for attacker_id in attacker_ids:
                    key = (current_match, current_turn, "block", iid, attacker_id)
                    if key not in seen_combat:
                        seen_combat.add(key)
                        flush_pending_event_groups()
                        blocker = disambiguated_object_label(iid, objects, card_label)
                        attacker = disambiguated_object_label(attacker_id, objects, card_label)
                        emit(f"{blocker} blocks {attacker}")

    def emit_tap_effect_events(gsm):
        """Explain effect-driven tap changes that alter combat availability."""
        for source_id, affected_id, tapped in non_mana_tap_events(gsm.get("annotations")):
            source = source_label(source_id)
            if not source:
                continue
            key = (current_match, gsm.get("gameStateId"), "tap", source_id, affected_id, tapped)
            if key in seen_state_events:
                continue
            seen_state_events.add(key)
            flush_pending_event_groups()
            verb = "taps" if tapped else "untaps"
            emit(f"{source} {verb} {card_label(affected_id)}")

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

    def emit_damage(ann, gsm):
        """Emit generic damage lines for player and permanent damage records."""
        details = detail_dict(ann.get("details"))
        damage = details.get("damage")
        affected = ann.get("affectedIds") or []
        source_id = ann.get("affectorId")
        if not isinstance(damage, int) or damage <= 0 or not affected:
            return

        target_id = affected[0]
        key = (current_match, gsm.get("gameStateId"), ann.get("id"), source_id, target_id, damage)
        if key in seen_damage_events:
            return
        seen_damage_events.add(key)

        if target_id in (1, 2):
            target = object_pronoun(owner_label(target_id))
        else:
            target_obj = objects.get(target_id, {})
            target = card_label(target_id)
            target = f"{target}{damage_mark_suffix(target_obj.get('damage'), target_obj.get('toughness'))}"
        flush_pending_event_groups()
        emit(phrase_damage(source_label(source_id), damage, target))

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

    def emit_scry(ann, gsm):
        """Emit scry choices when Arena exposes the final top/bottom card ids."""
        details = detail_dict(ann.get("details"))
        top_ids = details.get("topIds") or []
        bottom_ids = details.get("bottomIds") or []
        if isinstance(top_ids, int):
            top_ids = [top_ids]
        if isinstance(bottom_ids, int):
            bottom_ids = [bottom_ids]
        key = (
            current_match,
            gsm.get("gameStateId"),
            ann.get("id"),
            tuple(top_ids),
            tuple(bottom_ids),
        )
        if key in seen_state_events:
            return
        seen_state_events.add(key)
        chooser_seat = object_owner(ann.get("affectorId"))
        if chooser_seat is None and ann.get("affectedIds"):
            chooser_seat = object_owner(ann.get("affectedIds")[0])
        if chooser_seat is None:
            chooser_seat = current_turn_info.get("activePlayer")
        flush_pending_event_groups()
        emit(
            phrase_scry(
                owner_label(chooser_seat),
                [readable_move_name(iid, "Scry") for iid in top_ids],
                [readable_move_name(iid, "Scry") for iid in bottom_ids],
            )
        )

    def emit_shuffle(ann, gsm):
        """Emit library shuffles without listing every remapped library id."""
        affected = ann.get("affectedIds") or []
        if not affected:
            return
        seat = affected[0] if affected[0] in (1, 2) else object_owner(affected[0])
        key = (current_match, gsm.get("gameStateId"), ann.get("id"), seat)
        if key in seen_state_events:
            return
        seen_state_events.add(key)
        flush_pending_event_groups()
        emit(phrase_shuffle(source_label(ann.get("affectorId")), owner_label(seat)))

    def emit_revealed_card(ann, gsm):
        """Delay reveals so a following cast can replace Arena's synthetic reveal."""
        affected = ann.get("affectedIds") or []
        if not affected:
            return
        iid = affected[0]
        name = readable_move_name(iid, "Reveal")
        seat = object_owner(iid) or object_owner(ann.get("affectorId"))
        if seat is None:
            seat = current_turn_info.get("activePlayer")
        if name in {"unknown card", "a face-down card"} or (name == "a card" and seat is None):
            return
        key = (current_match, iid, name)
        if key in seen_revealed_cards:
            return
        seen_revealed_cards.add(key)
        pending_revealed_cards.append(
            {
                "owner": owner_label(seat),
                "name": name,
                "section": current_phase_section,
            }
        )

    def emit_attachment_created(ann, gsm):
        """Emit newly created attachments such as Auras and Equipment."""
        attachment_id = ann.get("affectorId")
        targets = ann.get("affectedIds") or []
        if attachment_id is None or not targets:
            return
        attachment = card_label(attachment_id)
        target = target_label(targets[0])
        key = (current_match, gsm.get("gameStateId"), ann.get("id"), attachment_id, targets[0])
        if key in seen_state_events:
            return
        seen_state_events.add(key)
        flush_pending_event_groups()
        emit(f"{attachment} becomes attached to {target}")

    def emit_mana_tap(ann, gsm):
        """Show mana abilities that tap a different permanent as their cost."""
        details = detail_dict(ann.get("details"))
        if details.get("tapped") != 1:
            return
        affected = ann.get("affectedIds") or []
        if not affected:
            return
        ability_obj = objects.get(ann.get("affectorId"), {})
        ability_grp_id = ability_obj.get("grpId")
        ability_text = (ability_texts.get(ability_grp_id) or "").casefold()
        if "tap an untapped" not in ability_text or "add one mana" not in ability_text:
            return
        source_id = ability_source_instance_id(ability_obj)
        permanent_id = affected[0]
        if source_id is None or source_id == permanent_id:
            return
        seat = object_controller(permanent_id) or object_owner(permanent_id)
        mana_counts = Counter()
        for entry in players.get(seat, {}).get("manaPool") or []:
            if entry.get("srcInstanceId") != source_id:
                continue
            if entry.get("abilityGrpId") not in {None, ability_grp_id}:
                continue
            value = mana_value_from_color(entry.get("color"))
            if value:
                mana_counts[value] += mana_amount_from_pool_entry(entry)
        mana = "".join(
            MANA_SYMBOL_LABELS[value] * mana_counts[value]
            for value in (1, 2, 3, 4, 5, 6)
            if mana_counts[value]
        )
        source = source_label(source_id)
        if not source or not mana:
            return
        flush_pending_event_groups()
        emit(phrase_mana_tap(source, owner_label(seat), card_label(permanent_id), mana))

    def emit_controller_changed(ann, gsm):
        """Emit control changes using the new controller from the current state."""
        affected = ann.get("affectedIds") or []
        if not affected:
            return
        iid = affected[0]
        controller = object_controller(iid)
        if controller is None:
            return
        key = (current_match, gsm.get("gameStateId"), ann.get("id"), iid, controller)
        if key in seen_state_events:
            return
        seen_state_events.add(key)
        flush_pending_event_groups()
        emit(
            phrase_player_action(
                owner_label(controller),
                "gain",
                f"control of {card_label(iid)}",
            )
        )

    def emit_replacement_effect(ann, gsm):
        """Emit replacement/prevention effects when Arena marks one as applied."""
        source = source_label(ann.get("affectorId"))
        if not source:
            return
        affected = ann.get("affectedIds") or []
        target = target_label(affected[0]) if affected else None
        key = (
            current_match,
            gsm.get("gameStateId"),
            ann.get("id"),
            ann.get("affectorId"),
            tuple(affected),
        )
        if key in seen_state_events:
            return
        seen_state_events.add(key)
        suffix = f" to {target}" if target else ""
        flush_pending_event_groups()
        emit(f"{source} replacement effect applies{suffix}")

    def clear_stale_block_state(gsm):
        """Clear cached block-only fields before applying final combat-step diffs."""
        turn_info = gsm.get("turnInfo") or {}
        if turn_info.get("phase") != "Phase_Combat":
            return
        if turn_info.get("step") not in {
            "Step_DeclareBlock",
            "Step_CombatDamage",
            "Step_EndCombat",
        }:
            return
        for obj in objects.values():
            obj.pop("blockState", None)
            obj.pop("blockInfo", None)

    def update_state(gsm):
        """Merge a full/diff game-state message into the local state model."""
        note_library_zone_snapshots(gsm)
        for team in gsm.get("teams", []):
            team_id = team.get("id")
            if team_id is not None:
                team_to_seats[team_id] = list(team.get("playerIds") or [])

        for zone in gsm.get("zones", []):
            if zone.get("zoneId") is not None:
                zones[zone["zoneId"]] = zone

        clear_stale_block_state(gsm)

        for obj in gsm.get("gameObjects", []):
            if obj.get("instanceId") is not None:
                objects[obj["instanceId"]] = obj
                record_object_history(
                    obj["instanceId"],
                    "object",
                    f"{object_label_from_object(obj) or card_name_from_grp(obj.get('grpId')) or 'object'} in {zone_label(obj.get('zoneId'))}",
                    {"gameStateId": gsm.get("gameStateId"), "object": obj},
                )
                if obj.get("type") == "GameObjectType_Ability":
                    source_name = card_name_from_grp(obj.get("objectSourceGrpId"))
                    if not source_name and obj.get("parentId") is not None:
                        source_name = source_label(obj.get("parentId"))
                    if source_name and not source_name.startswith("grpId "):
                        # Ability instances may be deleted in the same update
                        # that reports their zone-change effects. Store the
                        # source before deletion so later annotations still
                        # have a readable source.
                        ability_source_names[obj["instanceId"]] = source_name
                label = object_label_from_object(obj)
                if label:
                    remembered_object_labels[obj["instanceId"]] = label

        # Diff messages can report a move only as a ZoneTransfer annotation.
        # Reconcile it before emitting a turn snapshot so departed permanents
        # do not linger and newly resolved permanents appear immediately.
        reconcile_zone_transfer_objects(objects, gsm.get("annotations"), annotation_object_zones)

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
            record_object_history(
                deleted_id,
                "delete",
                f"{card_label(deleted_id)} deleted from game state",
                {"gameStateId": gsm.get("gameStateId")},
            )
            objects.pop(deleted_id, None)
            annotation_object_zones.pop(deleted_id, None)

    def emit_match_results(gsm):
        """Emit game/match result lines once per unique Arena result record."""
        emitted_result = False
        for result in gsm.get("gameInfo", {}).get("results") or []:
            scope = result.get("scope")
            winning_team_id = result.get("winningTeamId")
            reason = result.get("reason")
            result_type = result.get("result")
            if winning_team_id is None:
                continue
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
            emitted_result = True
        if emitted_result:
            archive_live_match_if_complete(current_match_record)

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
        for object_id in [ann.get("affectorId"), *(ann.get("affectedIds") or [])]:
            record_object_history(
                object_id,
                "annotation",
                ",".join(ann.get("type") or []),
                {"gameStateId": gsm.get("gameStateId"), "annotation": ann},
            )

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
        elif "AnnotationType_PowerToughnessModCreated" in ann_types:
            emit_power_toughness_mod(ann, gsm)
        elif "AnnotationType_ChoiceResult" in ann_types:
            emit_choice_result(ann, gsm)
        elif "AnnotationType_DamageDealt" in ann_types:
            emit_damage(ann, gsm)
            emit_infect_damage(ann, gsm)
            emit_no_combat_damage(ann, gsm)
        elif (
            "AnnotationType_CounterAdded" in ann_types
            or "AnnotationType_CounterRemoved" in ann_types
        ):
            emit_counter_change(ann, gsm)
        elif "AnnotationType_Scry" in ann_types:
            emit_scry(ann, gsm)
        elif "AnnotationType_Shuffle" in ann_types:
            emit_shuffle(ann, gsm)
        elif "AnnotationType_RevealedCardCreated" in ann_types:
            emit_revealed_card(ann, gsm)
        elif "AnnotationType_AttachmentCreated" in ann_types:
            emit_attachment_created(ann, gsm)
        elif "AnnotationType_TappedUntappedPermanent" in ann_types:
            emit_mana_tap(ann, gsm)
        elif "AnnotationType_ControllerChanged" in ann_types:
            emit_controller_changed(ann, gsm)
        elif "AnnotationType_ReplacementEffectApplied" in ann_types:
            emit_replacement_effect(ann, gsm)

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
        """Render the input-log read progress bar to stderr when progress output is enabled."""
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
            f"\rReading input logs [{bar}] {ratio:6.1%} "
            f"(Read: {read_mb:.1f}/{total_mb:.1f} MB)",
            end="",
            file=sys.stderr,
            flush=True,
        )

    def parse_json_root_from_line(first_line, file_obj):
        """Parse one-line JSON logs or compact multi-line client JSON blocks."""
        nonlocal read_bytes
        try:
            return json.loads(first_line)
        except json.JSONDecodeError:
            pass
        if first_line != "{":
            return None

        buffer = [first_line]
        # This deliberately handles the small pretty-printed ClientToGRE blocks
        # Arena writes for UI responses. Large startup/cache blobs are usually
        # one-line JSON and still go through the existing filters.
        for _ in range(500):
            raw_line = file_obj.readline()
            if not raw_line:
                break
            read_bytes += len(raw_line)
            render_progress()
            text = raw_line.decode("utf-8", errors="ignore").strip()
            buffer.append(text)
            if text == "}":
                try:
                    return json.loads("\n".join(buffer))
                except json.JSONDecodeError:
                    continue
        return None

    try:
        for log_path in log_paths:
            with log_path.open("rb") as f:
                if live:
                    # Live mode starts with the current game, meaning the last match
                    # Arena has started in Player.log, then behaves like tail -f.
                    live_start, live_game_number = find_last_game_start(
                        player_log,
                        player_names_by_match,
                    )
                    f.seek(live_start)
                    read_bytes = live_start
                    current_match_number = max(0, live_game_number - 1)

                while True:
                    raw_line = f.readline()
                    if not raw_line:
                        if live:
                            if live_state_only and not live_state_caught_up:
                                live_state_caught_up = True
                                print_live_state_snapshot(force=True)
                            time.sleep(0.25)
                            continue
                        break

                    read_bytes += len(raw_line)
                    render_progress()

                    line = raw_line.decode("utf-8", errors="ignore")
                    line = line.strip()
                    if not line.startswith("{"):
                        continue

                    root = parse_json_root_from_line(line, f)
                    if root is None:
                        continue

                    room_match_id, room_player_names = match_room_player_names(root)
                    if room_match_id and room_player_names:
                        player_names_by_match[room_match_id] = room_player_names

                    for msg in root.get("greToClientEvent", {}).get("greToClientMessages", []):
                        gsm = msg.get("gameStateMessage")
                        if not gsm:
                            actions_req = msg.get("actionsAvailableReq") or {}
                            if actions_req.get("actions"):
                                # ActionsAvailableReq often has richer
                                # autoTapSolution/manaPaymentOption data than
                                # the later game-state diff. Cache it before
                                # the actual cast event consumes those details.
                                current_actions = actions_req.get("actions") or []
                                remember_cast_mana_actions()
                            if not skipping_duplicate_match:
                                note_keyword_select_request(msg)
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

                        turn_info = gsm.get("turnInfo", {})
                        if turn_phase_key(turn_info) != turn_phase_key(current_turn_info):
                            clear_floating_mana_pools()

                        current_actions = gsm.get("actions") or []
                        remember_cast_mana_actions()
                        update_state(gsm)
                        if known_local_seat is None:
                            known_local_seat = infer_local_seat()

                        if "turnNumber" in turn_info and turn_info["turnNumber"] != current_turn:
                            flush_pending_event_groups()
                            was_suppressed = suppress_phase_heading
                            suppress_phase_heading = True
                            flush_pending_battlefield_permanent_casts()
                            suppress_phase_heading = was_suppressed
                            flush_all_pending_stack_casts(section_override=current_phase_section)
                            if current_turn is not None:
                                emit_cleanup_discards_before_turn_header(gsm, msg, turn_info)
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
                            current_turn_info = turn_info
                        if turn_info:
                            set_current_phase_section(phase_section_label(turn_info))

                        event_index += 1
                        record_gameplay_event(msg, gsm)
                        emit_continuous_effect_events(gsm)
                        emit_enters_attacking_events(gsm)
                        emit_tap_effect_events(gsm)
                        emit_combat_events(gsm)

                        # ManaPaid follows CastSpell in Arena's annotation list.
                        # Cache it first so even immediately emitted cast lines
                        # can show an opponent's actual reduced payment/source.
                        for ann in gsm.get("annotations", []):
                            if "AnnotationType_ManaPaid" in set(ann.get("type") or []):
                                note_mana_paid(ann)

                        for ann in gsm.get("annotations", []):
                            handle_annotation(ann, gsm, msg)

                        # A targetless permanent may have no later annotation
                        # to trigger its delayed cast line. Once this complete
                        # update places it on the battlefield, emit it now.
                        flush_pending_battlefield_permanent_casts()

                        finalize_library_debug_for_gsm(gsm)
                        emit_match_results(gsm)

                    if not root.get("greToClientEvent"):
                        emit_keyword_select_response(root)
                        mark_postgame_payload(root)
                    print_live_state_snapshot()
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
        inserted, updated, total = archive_seen_games(
            archive_db_path,
            transcript_matches,
            log_paths,
        )
        current_log_count = inserted + updated
        print(
            (
                f"Archive refresh: found {current_log_count} game(s) in the current logs "
                f"({inserted} new, {updated} already archived). "
                f"Archive now contains {total} total game(s) at {archive_db_path.expanduser()}."
            ),
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

    if debug_objects:
        print("\nDebug object histories:", file=sys.stderr)
        for match in selected_matches:
            print(
                f"\n===== OBJECT HISTORY GAME {match['number']}: MATCH {match['match_id']} =====",
                file=sys.stderr,
            )
            for object_id in sorted(debug_objects):
                history = match.get("object_history", {}).get(object_id, [])
                print(f"\n--- object {object_id} ---", file=sys.stderr)
                if not history:
                    print("No history recorded for this object in selected game.", file=sys.stderr)
                    continue
                for item in history:
                    print(
                        f"event={item['event_index']} turn={item['turn']} "
                        f"{item['kind']}: {item['summary']}",
                        file=sys.stderr,
                    )
                    if item.get("payload"):
                        print(
                            json.dumps(item["payload"], sort_keys=True, separators=(",", ":"))[:600],
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

    if diagnostics:
        print("\nTranscript diagnostics:", file=sys.stderr)
        for match in selected_matches:
            warnings = transcript_diagnostics(cleaned_transcript_lines(match["lines"]))
            print(
                f"\n===== DIAGNOSTICS GAME {match['number']}: MATCH {match['match_id']} =====",
                file=sys.stderr,
            )
            if not warnings:
                print("No suspicious transcript artifacts found.", file=sys.stderr)
                continue
            for warning in warnings:
                print(warning, file=sys.stderr)

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

    if debug_library:
        print("\nDebug library-count events:", file=sys.stderr)
        for match in selected_matches:
            print(
                f"\n===== LIBRARY GAME {match['number']}: MATCH {match['match_id']} =====",
                file=sys.stderr,
            )
            if not match["library_events"]:
                print("No library count changes or library-facing movements found.", file=sys.stderr)
                continue
            for event in match["library_events"]:
                print(
                    f"\n### event {event['event_index']} "
                    f"turn={event['turn']} gameStateId={event['gameStateId']}",
                    file=sys.stderr,
                )
                for record in event.get("records", []):
                    player = record.get("player") or "unknown player"
                    previous = record.get("previous")
                    new = record.get("new")
                    delta = record.get("delta")
                    delta_text = "" if delta is None else f" delta={delta:+d}"
                    card_text = f" card={record['card']!r}" if record.get("card") else ""
                    print(
                        (
                            f"  [{record.get('kind')}] {player}: {record.get('event')} "
                            f"{previous}->{new}{delta_text}{card_text}"
                        ),
                        file=sys.stderr,
                    )


def main() -> None:
    """Parse command-line arguments and run the MTG Arena transcript extractor."""
    parser = argparse.ArgumentParser(
        description=(
            "Extract readable MTG Arena game transcripts from Player.log. "
            "If paths are not provided, the script checks LOG/CARDDB "
            "environment variables and common local MTG Arena locations."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  Print the most recent game:
    python3 mtga_extract_games.py --last 1

  Print every archived game, after refreshing the archive from available logs:
    python3 mtga_extract_games.py --all

  Save the last three games to a text file:
    python3 mtga_extract_games.py --last 3 > mtga_transcript.txt

  Print only game 4 from the oldest archived game:
    python3 mtga_extract_games.py --nth-from-start 4

  Print the next-to-last game:
    python3 mtga_extract_games.py --nth-from-end 2

  Print games 3 through 5:
    python3 mtga_extract_games.py --range 3 5

  Debug where Arena records a card's choices:
    python3 mtga_extract_games.py --last 1 --debug-card "Serra's Emissary"

  Watch for new games as Arena writes Player.log:
    python3 mtga_extract_games.py --live

  Watch only compact, changed current-state snapshots for an LLM:
    python3 mtga_extract_games.py --live-state

  Highlight my lines and opponent lines in a terminal:
    python3 mtga_extract_games.py --last 1 --colour always

  Show path discovery diagnostics:
    python3 mtga_extract_games.py --find-paths

  List owned cards if Arena has written a full collection payload to the logs:
    python3 mtga_extract_games.py --list-owned-cards

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
        "--find-paths",
        action="store_true",
        help="print path discovery diagnostics and exit",
    )
    parser.add_argument(
        "--list-owned-cards",
        action="store_true",
        help=(
            "scan available Arena logs for a full owned-card collection payload "
            "and print owned card counts resolved through CARDDB"
        ),
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
    parser.add_argument(
        "--debug-library",
        action="store_true",
        help="advanced: print library count changes, inferred deltas, and drift warnings",
    )
    parser.add_argument(
        "--debug-object",
        action="append",
        type=int,
        default=[],
        metavar="INSTANCE_ID",
        help="advanced: print lifecycle notes for a specific Arena object instance id",
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="print suspicious transcript artifacts such as raw ids or duplicate casts to stderr",
    )
    selection_group = parser.add_mutually_exclusive_group()
    selection_group.add_argument(
        "--all",
        action="store_true",
        help=(
            "output every archived game after refreshing from available logs; "
            "with --no-archive-db, output every game in the parsed logs"
        ),
    )
    selection_group.add_argument(
        "--nth-from-start",
        "--select",
        dest="select",
        type=int,
        metavar="N",
        help=(
            "output only game N, counting from the oldest archived game; "
            "with --no-archive-db, count from the start of the parsed logs; "
            "--select is an alias"
        ),
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
        help=(
            "output games X through Y, inclusive, counting from the oldest archived game; "
            "with --no-archive-db, count from the start of the parsed logs"
        ),
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
        "--resolves",
        dest="show_resolves",
        action="store_true",
        default=False,
        help="show routine 'resolves' transcript lines",
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
    live_group = parser.add_mutually_exclusive_group()
    live_group.add_argument(
        "--live",
        action="store_true",
        help=(
            "print the current game from its start, watch Player.log for new lines, "
            "and archive completed games"
        ),
    )
    live_group.add_argument(
        "--live-state",
        action="store_true",
        help=(
            "silently catch up to the current game, then print only compact, "
            "deduplicated state snapshots for LLM ingestion"
        ),
    )
    parser.add_argument(
        "--live-state-file",
        type=Path,
        metavar="PATH",
        help=(
            "follow the current game and atomically overwrite PATH with only the "
            "newest compact state snapshot"
        ),
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
            "default path when no PATH is given: ./mtga_games.sqlite3"
        ),
    )
    parser.add_argument(
        "--no-archive-db",
        action="store_true",
        help="read and print directly from the available logs without updating the archive",
    )
    parser.add_argument(
        "--merge-archive",
        "--import-db",
        dest="merge_archive",
        nargs="+",
        type=Path,
        metavar="PATH",
        help=(
            "import one or more existing archive SQLite databases into --archive-db "
            "without reading Arena logs"
        ),
    )
    args = parser.parse_args()

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
    live_state_mode = args.live_state or args.live_state_file is not None
    if args.live and args.live_state_file is not None:
        parser.error("--live cannot be combined with --live-state-file")
    live_mode = args.live or live_state_mode
    if live_mode:
        live_option = "--live-state-file" if args.live_state_file else "--live-state" if args.live_state else "--live"
        if has_live_selection_conflict(args):
            parser.error(f"{live_option} cannot be combined with game selection options")
        if is_numeric_cli_token(args.player_log) or is_numeric_cli_token(args.carddb):
            parser.error(
                f"{live_option} does not take a game number. Use {live_option} to follow the current game, "
                "or --last 1 to print the most recent completed game."
            )
        if args.progress:
            parser.error(f"{live_option} cannot be combined with --progress")
    if args.no_archive_db:
        args.archive_db = None

    if args.find_paths:
        diagnostics = discover_input_paths(args.player_log, args.carddb)
        print(format_path_diagnostics(diagnostics))
        return

    if args.merge_archive:
        if args.archive_db is None:
            parser.error("--import-db requires an archive destination; do not use --no-archive-db")
        try:
            inserted, updated, total = merge_archive_databases(
                args.archive_db,
                args.merge_archive,
            )
        except (FileNotFoundError, ValueError) as exc:
            parser.exit(2, f"{exc}\n")
        print(
            (
                f"Archive merge: imported {inserted + updated} game(s) "
                f"({inserted} new, {updated} already archived). "
                f"Archive now contains {total} total game(s) at {args.archive_db.expanduser()}."
            ),
            file=sys.stderr,
        )
        return

    try:
        player_log, carddb, path_warning = resolve_input_paths(
            args.player_log,
            args.carddb,
            live=live_mode,
        )
    except FileNotFoundError as exc:
        parser.exit(2, f"{exc}\n")
    if path_warning:
        print(path_warning, file=sys.stderr)

    if args.list_owned_cards:
        log_paths = player_log_paths_for_reading(player_log, live=False)
        raise SystemExit(print_owned_cards(log_paths, carddb))

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
        # Card-type choice result annotations use the Arena CardType enum.
        "CardType": load_enum_value_names(carddb, "CardType"),
    }
    keyword_value_names = load_keyword_value_names(carddb)
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
        debug_library=args.debug_library,
        debug_objects=set(args.debug_object),
        diagnostics=args.diagnostics,
        nth_from_start=args.select,
        last_games=args.last,
        first_games=args.first,
        nth_from_end=args.nth_from_end,
        game_range=tuple(args.range) if args.range is not None else None,
        show_progress=False if live_mode else True if args.progress else False if args.no_progress else None,
        show_resolves=args.show_resolves,
        show_turn_state=not args.no_turn_state,
        show_phases=not args.no_phases,
        live=live_mode,
        live_state_only=live_state_mode,
        live_state_path=args.live_state_file,
        enum_value_names=enum_value_names,
        keyword_value_names=keyword_value_names,
        card_metadata=card_metadata,
        ability_texts=ability_texts,
        color_mode=args.colour,
        archive_db_path=args.archive_db,
    )


if __name__ == "__main__":
    main()
