# MTG Arena Game Extractor

MTG Arena Game Extractor is an open-source MTG Arena log parser that reconstructs complete games from Arena's Player.log and UTC log files and produces human-readable text transcripts.

The tool follows Arena game-state updates, translates internal card IDs into card names using the local Arena card database, and generates turn-by-turn game replays showing board state, hands, combat, spells, commander activity, life totals, graveyards, exile zones, and match results.

I originally wrote it because I wanted a simple way to review MTG Arena games in plain text and feed those transcripts into other tools. I could not find anything that produced detailed text replays from Arena logs, so I built one.

## Table Of Contents

- [Why?](#why)
- [Known Gaps](#known-gaps)
- [Basic Usage](#basic-usage)
- [Finding The Card Database](#finding-the-card-database)
- [Common Options](#common-options)
- [Example Transcript](#example-transcript)
- [Debugging Choices](#debugging-choices)
- [Fair Use And Intent](#fair-use-and-intent)
- [License](#license)

A complete sample transcript is included in [Example Transcript](#example-transcript).

It can also print board state, hands, graveyards, exile, commanders, attacks, blocks, damage, draws, reveals, scry/surveil/seek/conjure movements, attachments, control changes, life changes, mulligans, and match results when those details are available in the log.

It also tries to capture player choices when Arena records them in the structured game events, such as card type, creature type, and colour choices.

The parser reports continuous effects only when Arena exposes a generic state change in the log, such as permanents phasing out or back in.

It also tracks commander recasts, the next commander tax, and commander combat damage when those events are visible in Arena's game log. Player counters such as poison, energy, and experience are wired into the state model, but the parser only reports them when Arena exposes an unambiguous player counter event.

## Why?

Arena games go by quickly, and the client does not give me a simple text transcript after the game. A plain text transcript is useful because I can:

- review a game without watching a replay
- search for key turns or cards
- compare what I thought happened with what actually happened
- feed the transcript into other tools
- debug weird board states or decisions

I looked briefly at alternatives but none did what I wanted and I sometimes had to pay quite a bit of money to find that out. 

The script is also useful if you are curious about how MTG Arena represents games internally. There are debug modes for inspecting annotations, raw game events, card objects, and player choice records.

## Known Gaps

I learn what events show up in the log from my own games. I do not play every card, and I do not see every combination of cards, so there will be gaps in what this tool understands.

That is part of the fun of it. It gives me a little extra motivation to play more games and see more weird board states.

## Basic Usage

You need Python 3. This program only uses Python's standard library, so there are no extra Python packages to install.

On macOS, Python may already be installed. Check with:

```bash
python3 --version
```

If that command is not found, install Python from:

```text
https://www.python.org/downloads/
```

The examples below are for macOS because I wrote the code on a Mac laptop. I'll get around to trying it out on windows. The code should work on Windows too, but the `Player.log` and card database paths will be different.

Usually you can just run:

```bash
python3 mtga_extract_games.py --last 1 --no-resolves
```

The program will look in the normal macOS locations for `Player.log` and the newest `Raw_CardDatabase_*.mtga` file. It reads macOS `UTC_Log` archives first when they are available, then `Player-prev.log`, then `Player.log`, and de-duplicates overlapping games by match ID. Parsed games are saved to `./mtga_seen_games.sqlite3` by default, and normal transcript output is selected from that database. If it cannot find the needed paths, it will tell you what paths to set.

You can also set paths yourself on macOS:

```bash
LOG="$HOME/Library/Logs/Wizards Of The Coast/MTGA/Player.log"
CARDDB="$HOME/Library/Application Support/com.wizards.mtga/Downloads/Raw/Raw_CardDatabase_18c90f36843327a3b136b3ec128ed020.mtga"
```

Then run:

```bash
python3 mtga_extract_games.py --last 1 --no-resolves
```

Save the last two games to a file:

```bash
python3 mtga_extract_games.py --last 2 --no-resolves > mtga_transcript.txt
```

Show only one game by number:

```bash
python3 mtga_extract_games.py --nth-from-start 3 --no-resolves
```

Show the next-to-last game:

```bash
python3 mtga_extract_games.py --nth-from-end 2 --no-resolves
```

Show games 3 through 5:

```bash
python3 mtga_extract_games.py --range 3 5 --no-resolves
```

Show the current game from its start and then keep watching while Arena is running:

```bash
python3 mtga_extract_games.py --live --no-resolves
```

Live mode records each completed game to the archive as soon as Arena writes a final game result. If you stop live mode before the current game finishes, it prints a warning that the unfinished game was not recorded in the database.

Show the built-in help page:

```bash
python3 mtga_extract_games.py --help
```

## Finding The Card Database

The Arena card database filename changes when Arena updates. The script tries to find the newest one automatically. If it cannot, look in:

```text
~/Library/Application Support/com.wizards.mtga/Downloads/Raw/
```

and use the current `Raw_CardDatabase_*.mtga` file.

On Windows, look for the Arena `Player.log` file and the `Downloads/Raw` folder under your MTG Arena install or user data folders. The exact location can change depending on how Arena was installed, but the important files are still:

```text
Player.log
Raw_CardDatabase_*.mtga
```

You can either put those full paths into the command, or set environment variables:

```bash
export LOG="$HOME/Library/Logs/Wizards Of The Coast/MTGA/Player.log"
export CARDDB="$HOME/Library/Application Support/com.wizards.mtga/Downloads/Raw/Raw_CardDatabase_....mtga"
```

## Common Options

Use this for a short transcript of the most recent game:

```bash
python3 mtga_extract_games.py --last 1 --no-resolves --no-turn-state
```

Use this for a fuller transcript with board state at the start of each turn:

```bash
python3 mtga_extract_games.py --last 1 --no-resolves
```

Use this for the last three games:

```bash
python3 mtga_extract_games.py --last 3 --no-resolves
```

Use this for the first three games in the log:

```bash
python3 mtga_extract_games.py --first 3 --no-resolves
```

Use this for every game in the available Arena logs:

```bash
python3 mtga_extract_games.py --all --no-resolves
```

By default, parsed games are saved before output is printed:

```bash
python3 mtga_extract_games.py --all --no-resolves
```

The default archive is `./mtga_seen_games.sqlite3` in the current directory. You can choose a different path:

```bash
python3 mtga_extract_games.py --all --no-resolves --archive-db arena_games.sqlite3
```

The archive keeps stable match identity, game ordering, and transcript text separately. The `matches` table is keyed by Arena match ID, `games` stores one row per game within a match, `transcripts` stores generated plain-text output, and `log_sources` records the log files seen during refreshes. This keeps the database usable for best-of-one and best-of-three matches, and leaves room for future transcript formats or metadata.

For one-off raw-log debugging without updating or reading from the archive:

```bash
python3 mtga_extract_games.py --all --no-resolves --no-archive-db
```

Use this to add terminal colours:

```bash
python3 mtga_extract_games.py --last 1 --no-resolves --colour always
```

The colour mode highlights transcript structure, Me/Opponent lines, results, and known card names. Card names use MTG-style colour accents from the Arena card database when possible. Lands use colour identity, while spells and nonland permanents use printed colours. Multicolour cards use a conservative ANSI blend such as cyan for white-blue, purple for blue-red, and pink/bright magenta for white-red. Colourless cards, artifacts with no printed colour, and neutral list words such as `and` use neutral gray. `--color` is accepted as an alias, but the documented spelling is `--colour`.

Use this to save output to a text file:

```bash
python3 mtga_extract_games.py --last 3 --no-resolves > mtga_transcript.txt
```

The most useful options are:

- `--last 1`: show the most recent game
- `--last 3`: show the last three games
- `--all`: show every game in the available Arena logs
- `--first 3`: show the first three games
- `--nth-from-start 4`: show only game 4 from the log
- `--nth-from-end 2`: show the next-to-last game
- `--range 3 5`: show games 3 through 5
- `--live`: show the current game from its start, print new transcript lines as Arena writes them, and archive completed games
- `--no-resolves`: hide routine "resolves" lines
- `--no-turn-state`: hide board and hand snapshots
- `--no-phases`: hide phase and step headings
- `--progress`: show a progress bar on stderr while parsing
- `--no-progress`: hide the progress bar
- `--colour never`: do not add ANSI colour escapes; this is the default
- `--colour auto`: add ANSI colour escapes only when stdout is a terminal
- `--colour always`: always add ANSI colour escapes
- `--color`: older/American spelling alias for `--colour`
- `--archive-db`: use the default archive path, `./mtga_seen_games.sqlite3`
- `--archive-db PATH`: save parsed games to a SQLite archive at `PATH`
- `--no-archive-db`: read and print directly from the available logs without updating the archive

`--select 4` still works as an older name for `--nth-from-start 4`.


## Example Transcript

This is the most recent game extracted with:

```bash
python3 mtga_extract_games.py --last 1 --no-resolves
```

```text
===== GAME 31: MATCH 51838338-69a5-4e3c-b9f6-d3ec836cf066 =====
Game type: Constructed Brawl (25 starting life)

=== Turn 1: Opponent ===
My hand: Authority of the Consuls; Emeria's Call; Exalted Sunborn; Linvala, Keeper of Silence; 2x Plains; Thraben Watcher
My board:
  Lands: (empty)
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Library: 92 cards
  Command: Giada, Font of Hope
  Graveyard: (empty)
  Exile: (empty)
Opponent's hand: 7 unknown cards
Opponent's board:
  Lands: (empty)
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Library: 92 cards
  Command: Dovin, Architect of Law
  Graveyard: (empty)
  Exile: (empty)
Opponent plays Plains

=== Turn 2: Me ===
My hand: Authority of the Consuls; Emeria's Call; Exalted Sunborn; Linvala, Keeper of Silence; 2x Plains; Thraben Watcher
My board:
  Lands: (empty)
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Library: 92 cards
  Command: Giada, Font of Hope
  Graveyard: (empty)
  Exile: (empty)
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Plains
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Library: 92 cards
  Command: Dovin, Architect of Law
  Graveyard: (empty)
  Exile: (empty)

-- Beginning - draw --
I draw Steel Seraph
I play Plains
I cast Authority of the Consuls

=== Turn 3: Opponent ===
My hand: Emeria's Call; Exalted Sunborn; Linvala, Keeper of Silence; Plains; Steel Seraph; Thraben Watcher
My board:
  Lands: Tapped: Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: (empty)
  Library: 91 cards
  Command: Giada, Font of Hope
  Graveyard: (empty)
  Exile: (empty)
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Plains
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Library: 92 cards
  Command: Dovin, Architect of Law
  Graveyard: (empty)
  Exile: (empty)

-- Beginning - draw --
Opponent draws a card
Opponent plays Capital City

=== Turn 4: Me ===
My hand: Emeria's Call; Exalted Sunborn; Linvala, Keeper of Silence; Plains; Steel Seraph; Thraben Watcher
My board:
  Lands: Untapped: Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: (empty)
  Library: 91 cards
  Command: Giada, Font of Hope
  Graveyard: (empty)
  Exile: (empty)
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Plains
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Library: 91 cards
  Command: Dovin, Architect of Law
  Graveyard: (empty)
  Exile: (empty)

-- Beginning - draw --
I draw Herald's Horn
I play Plains
I cast Giada, Font of Hope from command zone; commander cast #1; next commander tax +2

=== Turn 5: Opponent ===
My hand: Emeria's Call; Exalted Sunborn; Herald's Horn; Linvala, Keeper of Silence; Steel Seraph; Thraben Watcher
My board:
  Lands: Tapped: 2x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: Untapped: Giada, Font of Hope (summoning sick)
  Library: 90 cards
  Command: (empty)
  Graveyard: (empty)
  Exile: (empty)
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Plains
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Library: 91 cards
  Command: Dovin, Architect of Law
  Graveyard: (empty)
  Exile: (empty)

-- Beginning - draw --
Opponent draws a card
Opponent gains 3 life (28)
Opponent draws a card
Opponent casts Revitalize
Opponent plays Plains

=== Turn 6: Me ===
My hand: Emeria's Call; Exalted Sunborn; Herald's Horn; Linvala, Keeper of Silence; Steel Seraph; Thraben Watcher
My board:
  Lands: Untapped: 2x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: Untapped: Giada, Font of Hope
  Library: 90 cards
  Command: (empty)
  Graveyard: (empty)
  Exile: (empty)
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Plains; Tapped: Capital City; Plains
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Library: 89 cards
  Command: Dovin, Architect of Law
  Graveyard: Revitalize
  Exile: (empty)

-- Beginning - draw --
I draw Angel of Destiny
I play Emeria, Shattered Skyclave
I lose 3 life (22)
I choose Angel for Herald's Horn (creature type)
I cast Herald's Horn

-- Combat - attackers --
I attack Opponent with Giada, Font of Hope

-- Combat - damage --
Commander damage: Giada, Font of Hope deals 2 damage to Opponent (2 total)
Opponent loses 2 life (26)

=== Turn 7: Opponent ===
My hand: Angel of Destiny; Exalted Sunborn; Linvala, Keeper of Silence; Steel Seraph; Thraben Watcher
My board:
  Lands: Tapped: Emeria, Shattered Skyclave; 2x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls; Herald's Horn
  Creatures: Untapped: Giada, Font of Hope
  Library: 89 cards
  Command: (empty)
  Graveyard: (empty)
  Exile: (empty)
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; 2x Plains
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Library: 89 cards
  Command: Dovin, Architect of Law
  Graveyard: Revitalize
  Exile: (empty)
Current State:
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
Opponent draws a card
Opponent casts Invasion of Dominaria
Invasion of Dominaria trigger: Opponent gains 4 life (30)
Opponent draws a card

=== Turn 8: Me ===
My hand: Angel of Destiny; Exalted Sunborn; Linvala, Keeper of Silence; Steel Seraph; Thraben Watcher
My board:
  Lands: Untapped: Emeria, Shattered Skyclave; 2x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls; Herald's Horn
  Creatures: Untapped: Giada, Font of Hope
  Library: 89 cards
  Command: (empty)
  Graveyard: (empty)
  Exile: (empty)
Opponent's hand: 7 unknown cards
Opponent's board:
  Lands: Tapped: Capital City; 2x Plains
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 87 cards
  Command: Dovin, Architect of Law
  Graveyard: Revitalize
  Exile: (empty)
Current State:
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw Bishop of Wings
I cast Bishop of Wings
I cast Steel Seraph
Bishop of Wings trigger: I gain 4 life (26)

=== Turn 9: Opponent ===
My hand: Angel of Destiny; Exalted Sunborn; Linvala, Keeper of Silence; Thraben Watcher
My board:
  Lands: Tapped: Emeria, Shattered Skyclave; 2x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls; Herald's Horn
  Creatures: Untapped: Bishop of Wings (summoning sick); Steel Seraph (+1/+1 from counters) (summoning sick); Tapped: Giada, Font of Hope
  Library: 88 cards
  Command: (empty)
  Graveyard: (empty)
  Exile: (empty)
Opponent's hand: 7 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; 2x Plains
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 87 cards
  Command: Dovin, Architect of Law
  Graveyard: Revitalize
  Exile: (empty)
Current State:
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
Opponent draws a card
Opponent casts Vanish into Eternity targeting Herald's Horn
Vanish into Eternity exiles Herald's Horn

=== Turn 10: Me ===
My hand: Angel of Destiny; Exalted Sunborn; Linvala, Keeper of Silence; Thraben Watcher
My board:
  Lands: Untapped: Emeria, Shattered Skyclave; 2x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: Untapped: Bishop of Wings; Giada, Font of Hope; Steel Seraph (+1/+1 from counters)
  Library: 88 cards
  Command: (empty)
  Graveyard: (empty)
  Exile: Herald's Horn
Opponent's hand: 7 unknown cards
Opponent's board:
  Lands: Tapped: Capital City; 2x Plains
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 86 cards
  Command: Dovin, Architect of Law
  Graveyard: Revitalize; Vanish into Eternity
  Exile: (empty)
Current State:
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw Sheltered by Ghosts
I cast Thraben Watcher
Bishop of Wings trigger: I gain 4 life (30)

-- Combat - attackers --
I attack Opponent with Bishop of Wings and Steel Seraph

-- Combat - damage --
Bishop of Wings deals 2 damage to Opponent
Steel Seraph deals 5 damage to Opponent
Opponent loses 7 life (23)
I gain 5 life (35)

=== Turn 11: Opponent ===
My hand: Angel of Destiny; Exalted Sunborn; Linvala, Keeper of Silence; Sheltered by Ghosts
My board:
  Lands: Tapped: Emeria, Shattered Skyclave; 2x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: Untapped: Bishop of Wings; Steel Seraph (+1/+1 from counters); Thraben Watcher (+2/+2 from counters) (summoning sick); Tapped: Giada, Font of Hope
  Library: 87 cards
  Command: (empty)
  Graveyard: (empty)
  Exile: Herald's Horn
Opponent's hand: 7 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; 2x Plains
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 86 cards
  Command: Dovin, Architect of Law
  Graveyard: Revitalize; Vanish into Eternity
  Exile: (empty)
Current State:
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
Opponent draws a card
Opponent plays Command Tower
Opponent casts Sphinx's Tutelage

=== Turn 12: Me ===
My hand: Angel of Destiny; Exalted Sunborn; Linvala, Keeper of Silence; Sheltered by Ghosts
My board:
  Lands: Untapped: Emeria, Shattered Skyclave; 2x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: Untapped: Bishop of Wings; Giada, Font of Hope; Steel Seraph (+1/+1 from counters); Thraben Watcher (+2/+2 from counters)
  Library: 87 cards
  Command: (empty)
  Graveyard: (empty)
  Exile: Herald's Horn
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Plains; Tapped: Capital City; Command Tower; Plains
  Artifacts/Enchantments: Untapped: Sphinx's Tutelage
  Creatures: (empty)
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 85 cards
  Command: Dovin, Architect of Law
  Graveyard: Revitalize; Vanish into Eternity
  Exile: (empty)
Current State:
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw Bonders' Enclave
I play Bonders' Enclave
I cast Angel of Destiny
Bishop of Wings trigger: I gain 4 life (39)

-- Combat - attackers --
I attack Opponent with Bishop of Wings; Steel Seraph; and Thraben Watcher

-- Combat - damage --
Bishop of Wings deals 2 damage to Opponent
Steel Seraph deals 5 damage to Opponent
Thraben Watcher deals 4 damage to Opponent
Opponent loses 11 life (12)
I gain 5 life (44)
Angel of Destiny trigger: I gain 4 life
Angel of Destiny trigger: Opponent gains 4 life
Angel of Destiny trigger: I gain 5 life
Angel of Destiny trigger: Opponent gains 5 life
Angel of Destiny trigger: I gain 2 life (55)
Angel of Destiny trigger: Opponent gains 2 life (23)

=== Turn 13: Opponent ===
My hand: Exalted Sunborn; Linvala, Keeper of Silence; Sheltered by Ghosts
My board:
  Lands: Tapped: Bonders' Enclave; Emeria, Shattered Skyclave; 2x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: Untapped: Angel of Destiny (+3/+3 from counters) (summoning sick); Bishop of Wings; Steel Seraph (+1/+1 from counters); Thraben Watcher (+2/+2 from counters); Tapped: Giada, Font of Hope
  Library: 86 cards
  Command: (empty)
  Graveyard: (empty)
  Exile: Herald's Horn
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; 2x Plains
  Artifacts/Enchantments: Untapped: Sphinx's Tutelage
  Creatures: (empty)
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 85 cards
  Command: Dovin, Architect of Law
  Graveyard: Revitalize; Vanish into Eternity
  Exile: (empty)
Current State:
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
Opponent draws a card
Sphinx's Tutelage triggers resolve; I mill 2 cards
Opponent plays Radiant Fountain
Radiant Fountain ability: Opponent gains 2 life (25)
Opponent casts Starfall Invocation
Starfall Invocation destroys Giada, Font of Hope
Starfall Invocation destroys Bishop of Wings
Starfall Invocation destroys Steel Seraph
Starfall Invocation destroys Thraben Watcher
Starfall Invocation destroys Angel of Destiny
Giada, Font of Hope moves to command zone

=== Turn 14: Me ===
My hand: Exalted Sunborn; Linvala, Keeper of Silence; Sheltered by Ghosts
My board:
  Lands: Untapped: Bonders' Enclave; Emeria, Shattered Skyclave; 2x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: Untapped: 4x Spirit
  Library: 84 cards
  Command: Giada, Font of Hope [next commander tax +2]
  Graveyard: Angel of Destiny; Bishop of Wings; Boon-Bringer Valkyrie; Plains; Steel Seraph; Thraben Watcher
  Exile: Herald's Horn
Opponent's hand: 5 unknown cards
Opponent's board:
  Lands: Tapped: Capital City; Command Tower; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Sphinx's Tutelage
  Creatures: (empty)
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 84 cards
  Command: Dovin, Architect of Law
  Graveyard: Revitalize; Starfall Invocation; Vanish into Eternity
  Exile: (empty)
Current State:
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw The Immortal Sun
I cast Giada, Font of Hope from command zone; commander cast #2; next commander tax +4

-- Combat - attackers --
4x I attack Opponent with Spirit

-- Combat - damage --
4x Spirit deals 1 damage to Opponent
Opponent loses 4 life (21)

=== Turn 15: Opponent ===
My hand: Exalted Sunborn; Linvala, Keeper of Silence; Sheltered by Ghosts; The Immortal Sun
My board:
  Lands: Tapped: Bonders' Enclave; Emeria, Shattered Skyclave; 2x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: Untapped: Giada, Font of Hope (summoning sick); Tapped: 4x Spirit
  Library: 83 cards
  Command: (empty)
  Graveyard: Angel of Destiny; Bishop of Wings; Boon-Bringer Valkyrie; Plains; Steel Seraph; Thraben Watcher
  Exile: Herald's Horn
Opponent's hand: 5 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Sphinx's Tutelage
  Creatures: (empty)
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 84 cards
  Command: Dovin, Architect of Law
  Graveyard: Revitalize; Starfall Invocation; Vanish into Eternity
  Exile: (empty)
Current State:
  My next commander tax for Giada, Font of Hope is +4
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
Opponent draws a card
Sphinx's Tutelage triggers resolve; I mill 2 cards
Opponent casts Depopulate
4x Depopulate destroys Spirit
Depopulate destroys Giada, Font of Hope
Giada, Font of Hope moves to command zone

=== Turn 16: Me ===
My hand: Exalted Sunborn; Linvala, Keeper of Silence; Sheltered by Ghosts; The Immortal Sun
My board:
  Lands: Untapped: Bonders' Enclave; Emeria, Shattered Skyclave; 2x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: (empty)
  Library: 81 cards
  Command: Giada, Font of Hope [next commander tax +4]
  Graveyard: Angel of Destiny; Bishop of Wings; Boon-Bringer Valkyrie; Flowering of the White Tree; 2x Plains; Steel Seraph; Thraben Watcher
  Exile: Herald's Horn
Opponent's hand: 5 unknown cards
Opponent's board:
  Lands: Untapped: Command Tower; Tapped: Capital City; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Sphinx's Tutelage
  Creatures: (empty)
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 83 cards
  Command: Dovin, Architect of Law
  Graveyard: Depopulate; Revitalize; Starfall Invocation; Vanish into Eternity
  Exile: (empty)
Current State:
  My next commander tax for Giada, Font of Hope is +4
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw Plains
I play Plains
I cast Linvala, Keeper of Silence

=== Turn 17: Opponent ===
My hand: Exalted Sunborn; Sheltered by Ghosts; The Immortal Sun
My board:
  Lands: Untapped: Plains; Tapped: Bonders' Enclave; Emeria, Shattered Skyclave; 2x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: Untapped: Linvala, Keeper of Silence (summoning sick)
  Library: 80 cards
  Command: Giada, Font of Hope [next commander tax +4]
  Graveyard: Angel of Destiny; Bishop of Wings; Boon-Bringer Valkyrie; Flowering of the White Tree; 2x Plains; Steel Seraph; Thraben Watcher
  Exile: Herald's Horn
Opponent's hand: 5 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Sphinx's Tutelage
  Creatures: (empty)
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 83 cards
  Command: Dovin, Architect of Law
  Graveyard: Depopulate; Revitalize; Starfall Invocation; Vanish into Eternity
  Exile: (empty)
Current State:
  My next commander tax for Giada, Font of Hope is +4
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
Opponent draws a card

-- Ending - end step --
Sphinx's Tutelage triggers resolve; I mill 4 cards

=== Turn 18: Me ===
My hand: Exalted Sunborn; Sheltered by Ghosts; The Immortal Sun
My board:
  Lands: Untapped: Bonders' Enclave; Emeria, Shattered Skyclave; 3x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: Untapped: Linvala, Keeper of Silence
  Library: 76 cards
  Command: Giada, Font of Hope [next commander tax +4]
  Graveyard: Angel of Destiny; Bishop of Wings; Boon-Bringer Valkyrie; Esper Sentinel; Flowering of the White Tree; Path to Exile; 3x Plains; Steel Seraph; Thraben Watcher; Witch Enchanter
  Exile: Herald's Horn
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Sphinx's Tutelage
  Creatures: (empty)
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 82 cards
  Command: Dovin, Architect of Law
  Graveyard: Depopulate; Revitalize; Starfall Invocation; Vanish into Eternity
  Exile: (empty)
Current State:
  My next commander tax for Giada, Font of Hope is +4
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw Plains
I play Plains
I cast Giada, Font of Hope from command zone; commander cast #3; next commander tax +6

-- Combat - attackers --
I attack Opponent with Linvala, Keeper of Silence

-- Combat - damage --
Linvala, Keeper of Silence deals 3 damage to Opponent
Opponent loses 3 life (18)

=== Turn 19: Opponent ===
My hand: Exalted Sunborn; Sheltered by Ghosts; The Immortal Sun
My board:
  Lands: Tapped: Bonders' Enclave; Emeria, Shattered Skyclave; 4x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: Untapped: Giada, Font of Hope (summoning sick); Tapped: Linvala, Keeper of Silence
  Library: 75 cards
  Command: (empty)
  Graveyard: Angel of Destiny; Bishop of Wings; Boon-Bringer Valkyrie; Esper Sentinel; Flowering of the White Tree; Path to Exile; 3x Plains; Steel Seraph; Thraben Watcher; Witch Enchanter
  Exile: Herald's Horn
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Sphinx's Tutelage
  Creatures: (empty)
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 82 cards
  Command: Dovin, Architect of Law
  Graveyard: Depopulate; Revitalize; Starfall Invocation; Vanish into Eternity
  Exile: (empty)
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
Opponent draws a card
Sphinx's Tutelage triggers resolve; I mill 2 cards
Opponent casts Ultima
Ultima destroys Linvala, Keeper of Silence
Ultima destroys Giada, Font of Hope
Ultima is exiled

=== Turn 20: Me ===
My hand: Exalted Sunborn; Sheltered by Ghosts; The Immortal Sun
My board:
  Lands: Untapped: Bonders' Enclave; Emeria, Shattered Skyclave; 4x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: (empty)
  Library: 73 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Bishop of Wings; Boon-Bringer Valkyrie; Esper Sentinel; Flowering of the White Tree; Linvala, Keeper of Silence; Path to Exile; 4x Plains; Platinum Angel; Steel Seraph; Thraben Watcher; Witch Enchanter
  Exile: Herald's Horn
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Tapped: Capital City; Command Tower; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Sphinx's Tutelage
  Creatures: (empty)
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 81 cards
  Command: Dovin, Architect of Law
  Graveyard: Depopulate; Revitalize; Starfall Invocation; Vanish into Eternity
  Exile: Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - upkeep --
Giada, Font of Hope moves to command zone

-- Beginning - draw --
I draw Winds of Abandon
I cast The Immortal Sun

=== Turn 21: Opponent ===
My hand: Exalted Sunborn; Sheltered by Ghosts; Winds of Abandon
My board:
  Lands: Tapped: Bonders' Enclave; Emeria, Shattered Skyclave; 4x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls; The Immortal Sun
  Creatures: (empty)
  Library: 72 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Bishop of Wings; Boon-Bringer Valkyrie; Esper Sentinel; Flowering of the White Tree; Linvala, Keeper of Silence; Path to Exile; 4x Plains; Platinum Angel; Steel Seraph; Thraben Watcher; Witch Enchanter
  Exile: Herald's Horn
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Sphinx's Tutelage
  Creatures: (empty)
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 81 cards
  Command: Dovin, Architect of Law
  Graveyard: Depopulate; Revitalize; Starfall Invocation; Vanish into Eternity
  Exile: Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
Opponent draws a card
Sphinx's Tutelage triggers resolve; I mill 6 cards
Opponent plays Glacial Fortress
Opponent casts Felidar Sovereign
Authority of the Consuls trigger: I gain 1 life (56)

=== Turn 22: Me ===
My hand: Exalted Sunborn; Sheltered by Ghosts; Winds of Abandon
My board:
  Lands: Untapped: Bonders' Enclave; Emeria, Shattered Skyclave; 4x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls; The Immortal Sun
  Creatures: (empty)
  Library: 66 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of the Dire Hour; Bishop of Wings; Boon-Bringer Valkyrie; Cavern of Souls; Esper Sentinel; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 5x Plains; Platinum Angel; Reidane, God of the Worthy; Serra Paragon; Steel Seraph; Thraben Watcher; Witch Enchanter
  Exile: Herald's Horn
Opponent's hand: 5 unknown cards
Opponent's board:
  Lands: Tapped: Capital City; Command Tower; Glacial Fortress; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Sphinx's Tutelage
  Creatures: Tapped: Felidar Sovereign (summoning sick)
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 80 cards
  Command: Dovin, Architect of Law
  Graveyard: Depopulate; Revitalize; Starfall Invocation; Vanish into Eternity
  Exile: Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw Resplendent Angel
I draw Arcane Signet
I cast Resplendent Angel
I cast Sheltered by Ghosts targeting Resplendent Angel
Sheltered by Ghosts becomes attached to Resplendent Angel
Sheltered by Ghosts trigger exiles Sphinx's Tutelage
I cast Arcane Signet

=== Turn 23: Opponent ===
My hand: Exalted Sunborn; Winds of Abandon
My board:
  Lands: Untapped: 2x Plains; Tapped: Bonders' Enclave; Emeria, Shattered Skyclave; 2x Plains
  Artifacts/Enchantments: Untapped: Arcane Signet; Authority of the Consuls; Sheltered by Ghosts; The Immortal Sun
  Creatures: Untapped: Resplendent Angel (enchanted by Sheltered by Ghosts) (summoning sick)
  Library: 64 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of the Dire Hour; Bishop of Wings; Boon-Bringer Valkyrie; Cavern of Souls; Esper Sentinel; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 5x Plains; Platinum Angel; Reidane, God of the Worthy; Serra Paragon; Steel Seraph; Thraben Watcher; Witch Enchanter
  Exile: Herald's Horn
Opponent's hand: 5 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; Glacial Fortress; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: (empty)
  Creatures: Untapped: Felidar Sovereign
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 80 cards
  Command: Dovin, Architect of Law
  Graveyard: Depopulate; Revitalize; Starfall Invocation; Vanish into Eternity
  Exile: Sphinx's Tutelage; Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
Opponent draws a card

-- Combat - attackers --
Opponent attacks me with Felidar Sovereign

-- Combat - damage --
Felidar Sovereign deals 4 damage to me
I lose 4 life (52)
Opponent gains 4 life (22)

-- Postcombat main --
I draw Plains
Opponent casts Dawn's Truce
Opponent casts Armageddon
2x Armageddon destroys Plains
Armageddon destroys Emeria's Call
Armageddon destroys Bonders' Enclave
2x Armageddon destroys Plains

=== Turn 24: Me ===
My hand: Exalted Sunborn; Plains; Winds of Abandon
My board:
  Lands: (empty)
  Artifacts/Enchantments: Untapped: Arcane Signet; Authority of the Consuls; Sheltered by Ghosts; The Immortal Sun
  Creatures: Untapped: Resplendent Angel (enchanted by Sheltered by Ghosts)
  Library: 63 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of the Dire Hour; Bishop of Wings; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 9x Plains; Platinum Angel; Reidane, God of the Worthy; Serra Paragon; Steel Seraph; Thraben Watcher; Witch Enchanter
  Exile: Herald's Horn
Opponent's hand: 4 unknown cards
Opponent's board:
  Lands: Tapped: Capital City; Command Tower; Glacial Fortress; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: (empty)
  Creatures: Untapped: Felidar Sovereign
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 79 cards
  Command: Dovin, Architect of Law
  Graveyard: Armageddon; Dawn's Truce; Depopulate; Revitalize; Starfall Invocation; Vanish into Eternity
  Exile: Sphinx's Tutelage; Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw Labyrinth of Skophos
I draw Righteous Valkyrie
I play Plains
I cast Righteous Valkyrie

-- Combat - attackers --
I attack Opponent with Resplendent Angel

-- Combat - damage --
Resplendent Angel deals 7 damage to Opponent
Opponent loses 7 life (15)
I gain 7 life (59)

-- Ending - end step --
Righteous Valkyrie trigger: I gain 7 life (66)

=== Turn 25: Opponent ===
My hand: Exalted Sunborn; Labyrinth of Skophos; Winds of Abandon
My board:
  Lands: Tapped: Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls; Sheltered by Ghosts; The Immortal Sun; Tapped: Arcane Signet
  Creatures: Untapped: Angel (summoning sick); Righteous Valkyrie (summoning sick); Tapped: Resplendent Angel (enchanted by Sheltered by Ghosts)
  Library: 61 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of the Dire Hour; Bishop of Wings; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 9x Plains; Platinum Angel; Reidane, God of the Worthy; Serra Paragon; Steel Seraph; Thraben Watcher; Witch Enchanter
  Exile: Herald's Horn
Opponent's hand: 4 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; Glacial Fortress; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: (empty)
  Creatures: Untapped: Felidar Sovereign
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 79 cards
  Command: Dovin, Architect of Law
  Graveyard: Armageddon; Dawn's Truce; Depopulate; Revitalize; Starfall Invocation; Vanish into Eternity
  Exile: Sphinx's Tutelage; Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
Opponent draws a card
Opponent reveals Felidar Sovereign
Time Wipe: Opponent returns Felidar Sovereign to hand
Opponent casts Time Wipe
Time Wipe destroys Resplendent Angel
Time Wipe destroys Righteous Valkyrie
Time Wipe destroys Angel
My Sheltered by Ghosts dies
Sheltered by Ghosts trigger: Opponent returns Sphinx's Tutelage to the battlefield

=== Turn 26: Me ===
My hand: Exalted Sunborn; Labyrinth of Skophos; Winds of Abandon
My board:
  Lands: Untapped: Plains
  Artifacts/Enchantments: Untapped: Arcane Signet; Authority of the Consuls; The Immortal Sun
  Creatures: (empty)
  Library: 61 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of the Dire Hour; Bishop of Wings; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 9x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Steel Seraph; Thraben Watcher; Witch Enchanter
  Exile: Herald's Horn
Opponent's hand: 5 unknown cards
Opponent's board:
  Lands: Untapped: Glacial Fortress; Tapped: Capital City; Command Tower; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Sphinx's Tutelage
  Creatures: (empty)
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 78 cards
  Command: Dovin, Architect of Law
  Graveyard: Armageddon; Dawn's Truce; Depopulate; Revitalize; Starfall Invocation; Time Wipe; Vanish into Eternity
  Exile: Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw Starnheim Aspirant
I draw Archangel of Thune
I play Labyrinth of Skophos
I cast Starnheim Aspirant

=== Turn 27: Opponent ===
My hand: Archangel of Thune; Exalted Sunborn; Winds of Abandon
My board:
  Lands: Untapped: Plains; Tapped: Labyrinth of Skophos
  Artifacts/Enchantments: Untapped: Authority of the Consuls; The Immortal Sun; Tapped: Arcane Signet
  Creatures: Untapped: Starnheim Aspirant (summoning sick)
  Library: 59 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of the Dire Hour; Bishop of Wings; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 9x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Steel Seraph; Thraben Watcher; Witch Enchanter
  Exile: Herald's Horn
Opponent's hand: 5 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; Glacial Fortress; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Sphinx's Tutelage
  Creatures: (empty)
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 78 cards
  Command: Dovin, Architect of Law
  Graveyard: Armageddon; Dawn's Truce; Depopulate; Revitalize; Starfall Invocation; Time Wipe; Vanish into Eternity
  Exile: Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
Opponent draws a card
Sphinx's Tutelage triggers resolve; I mill 4 cards
Opponent casts Season of the Burrow targeting The Immortal Sun; Starnheim Aspirant
Season of the Burrow exiles The Immortal Sun
I draw Thalia, Heretic Cathar
Season of the Burrow exiles Starnheim Aspirant
I draw Tyrite Sanctum
Authority of the Consuls trigger: I gain 1 life (67)

=== Turn 28: Me ===
My hand: Archangel of Thune; Exalted Sunborn; Thalia, Heretic Cathar; Tyrite Sanctum; Winds of Abandon
My board:
  Lands: Untapped: Labyrinth of Skophos; Plains
  Artifacts/Enchantments: Untapped: Arcane Signet; Authority of the Consuls
  Creatures: (empty)
  Library: 53 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Thraben Watcher; Witch Enchanter
  Exile: Herald's Horn; Starnheim Aspirant; The Immortal Sun
Opponent's hand: 5 unknown cards
Opponent's board:
  Lands: Untapped: Glacial Fortress; Tapped: Capital City; Command Tower; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Sphinx's Tutelage
  Creatures: Tapped: Rabbit (summoning sick)
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 77 cards
  Command: Dovin, Architect of Law
  Graveyard: Armageddon; Dawn's Truce; Depopulate; Revitalize; Season of the Burrow; Starfall Invocation; Time Wipe; Vanish into Eternity
  Exile: Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw Stroke of Midnight
I play Tyrite Sanctum
I cast Stroke of Midnight targeting Sphinx's Tutelage
Stroke of Midnight destroys Sphinx's Tutelage
Authority of the Consuls trigger: I gain 1 life (68)

=== Turn 29: Opponent ===
My hand: Archangel of Thune; Exalted Sunborn; Thalia, Heretic Cathar; Winds of Abandon
My board:
  Lands: Untapped: Plains; Tapped: Labyrinth of Skophos; Tyrite Sanctum
  Artifacts/Enchantments: Untapped: Authority of the Consuls; Tapped: Arcane Signet
  Creatures: (empty)
  Library: 52 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; Thraben Watcher; Witch Enchanter
  Exile: Herald's Horn; Starnheim Aspirant; The Immortal Sun
Opponent's hand: 5 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; Glacial Fortress; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: (empty)
  Creatures: Untapped: Human; Rabbit
  Other: Untapped: Invasion of Dominaria (Defense counters: 5)
  Library: 77 cards
  Command: Dovin, Architect of Law
  Graveyard: Armageddon; Dawn's Truce; Depopulate; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Time Wipe; Vanish into Eternity
  Exile: Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
Opponent draws a card
Opponent casts Dovin, Architect of Law from command zone; commander cast #1; next commander tax +2
Dovin, Architect of Law ability: Opponent gains 2 life (17)
Opponent draws a card
Opponent plays Demolition Field

-- Combat - attackers --
Opponent attacks Invasion of Dominaria with Rabbit and Human

-- Combat - damage --
Rabbit deals 1 damage to Invasion of Dominaria
Human deals 1 damage to Invasion of Dominaria

=== Turn 30: Me ===
My hand: Archangel of Thune; Exalted Sunborn; Thalia, Heretic Cathar; Winds of Abandon
My board:
  Lands: Untapped: Labyrinth of Skophos; Plains; Tyrite Sanctum
  Artifacts/Enchantments: Untapped: Arcane Signet; Authority of the Consuls
  Creatures: (empty)
  Library: 52 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; Thraben Watcher; Witch Enchanter
  Exile: Herald's Horn; Starnheim Aspirant; The Immortal Sun
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Demolition Field; Tapped: Capital City; Command Tower; Glacial Fortress; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: (empty)
  Creatures: Tapped: Human; Rabbit
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 6); Invasion of Dominaria (Defense counters: 3)
  Library: 75 cards
  Command: (empty)
  Graveyard: Armageddon; Dawn's Truce; Depopulate; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Time Wipe; Vanish into Eternity
  Exile: Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw Fateful Absence
I cast Thalia, Heretic Cathar

=== Turn 31: Opponent ===
My hand: Archangel of Thune; Exalted Sunborn; Fateful Absence; Winds of Abandon
My board:
  Lands: Untapped: Plains; Tapped: Labyrinth of Skophos; Tyrite Sanctum
  Artifacts/Enchantments: Untapped: Authority of the Consuls; Tapped: Arcane Signet
  Creatures: Untapped: Thalia, Heretic Cathar (summoning sick)
  Library: 51 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; Thraben Watcher; Witch Enchanter
  Exile: Herald's Horn; Starnheim Aspirant; The Immortal Sun
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; Demolition Field; Glacial Fortress; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: (empty)
  Creatures: Untapped: Human; Rabbit
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 6); Invasion of Dominaria (Defense counters: 3)
  Library: 75 cards
  Command: (empty)
  Graveyard: Armageddon; Dawn's Truce; Depopulate; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Time Wipe; Vanish into Eternity
  Exile: Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
Opponent draws a card
Opponent plays Island
Opponent casts Ugin, Eye of the Storms
Ugin, Eye of the Storms trigger exiles Thalia, Heretic Cathar
Ugin, Eye of the Storms ability exiles Authority of the Consuls
Opponent casts Aetherflux Reservoir
Dovin, Architect of Law ability: Opponent gains 2 life (19)
Opponent draws a card

-- Combat - attackers --
Opponent attacks Invasion of Dominaria with Rabbit and Human

-- Combat - damage --
Rabbit deals 1 damage to Invasion of Dominaria
Human deals 1 damage to Invasion of Dominaria

=== Turn 32: Me ===
My hand: Archangel of Thune; Exalted Sunborn; Fateful Absence; Winds of Abandon
My board:
  Lands: Untapped: Labyrinth of Skophos; Plains; Tyrite Sanctum
  Artifacts/Enchantments: Untapped: Arcane Signet
  Creatures: (empty)
  Library: 51 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; Thraben Watcher; Witch Enchanter
  Exile: Authority of the Consuls; Herald's Horn; Starnheim Aspirant; Thalia, Heretic Cathar; The Immortal Sun
Opponent's hand: 5 unknown cards
Opponent's board:
  Lands: Tapped: Capital City; Command Tower; Demolition Field; Glacial Fortress; Island; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Aetherflux Reservoir
  Creatures: Tapped: Human; Rabbit
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 7); Invasion of Dominaria (Defense counter); Ugin, Eye of the Storms (Loyalty counters: 7)
  Library: 73 cards
  Command: (empty)
  Graveyard: Armageddon; Dawn's Truce; Depopulate; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Time Wipe; Vanish into Eternity
  Exile: Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw Archangel of Tithes
I cast Fateful Absence targeting Ugin, Eye of the Storms
Fateful Absence destroys Ugin, Eye of the Storms

=== Turn 33: Opponent ===
My hand: Archangel of Thune; Archangel of Tithes; Exalted Sunborn; Winds of Abandon
My board:
  Lands: Untapped: Plains; Tyrite Sanctum; Tapped: Labyrinth of Skophos
  Artifacts/Enchantments: Tapped: Arcane Signet
  Creatures: (empty)
  Library: 50 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Fateful Absence; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; Thraben Watcher; Witch Enchanter
  Exile: Authority of the Consuls; Herald's Horn; Starnheim Aspirant; Thalia, Heretic Cathar; The Immortal Sun
Opponent's hand: 5 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; Demolition Field; Glacial Fortress; Island; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Aetherflux Reservoir; Clue
  Creatures: Untapped: Human; Rabbit
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 7); Invasion of Dominaria (Defense counter)
  Library: 73 cards
  Command: (empty)
  Graveyard: Armageddon; Dawn's Truce; Depopulate; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Time Wipe; Ugin, Eye of the Storms; Vanish into Eternity
  Exile: Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
Opponent draws a card
Aetherflux Reservoir ability: Opponent gains 1 life (20)
Opponent casts Felidar Sovereign
Dovin, Architect of Law ability: Opponent gains 2 life (22)
Opponent draws a card

-- Combat - attackers --
Opponent attacks Invasion of Dominaria with Rabbit
Opponent attacks me with Human

-- Combat - blockers --
Clue trigger: Opponent sacrifices Clue
Opponent draws a card

-- Combat - damage --
Rabbit deals 1 damage to Invasion of Dominaria
Human deals 1 damage to me
I lose 1 life (67)
Invasion of Dominaria trigger exiles Invasion of Dominaria
Aetherflux Reservoir ability: Opponent gains 2 life (24)
Opponent casts Serra Faithkeeper

=== Turn 34: Me ===
My hand: Archangel of Thune; Archangel of Tithes; Exalted Sunborn; Winds of Abandon
My board:
  Lands: Untapped: Labyrinth of Skophos; Plains; Tyrite Sanctum
  Artifacts/Enchantments: Untapped: Arcane Signet
  Creatures: (empty)
  Library: 50 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Fateful Absence; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; Thraben Watcher; Witch Enchanter
  Exile: Authority of the Consuls; Herald's Horn; Starnheim Aspirant; Thalia, Heretic Cathar; The Immortal Sun
Opponent's hand: 7 unknown cards
Opponent's board:
  Lands: Tapped: Capital City; Command Tower; Demolition Field; Glacial Fortress; Island; 2x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Aetherflux Reservoir
  Creatures: Untapped: Felidar Sovereign (summoning sick); Serra Faithkeeper (summoning sick); Tapped: Human; Rabbit
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 8)
  Library: 70 cards
  Command: (empty)
  Graveyard: Armageddon; Dawn's Truce; Depopulate; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Time Wipe; Ugin, Eye of the Storms; Vanish into Eternity
  Exile: Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw Plains
I play Plains
I cast Winds of Abandon targeting Felidar Sovereign
Winds of Abandon exiles Felidar Sovereign
Winds of Abandon: Opponent puts Plains into the battlefield
Winds of Abandon: Opponent shuffles their library

=== Turn 35: Opponent ===
My hand: Archangel of Thune; Archangel of Tithes; Exalted Sunborn
My board:
  Lands: Untapped: 2x Plains; Tyrite Sanctum; Tapped: Labyrinth of Skophos
  Artifacts/Enchantments: Tapped: Arcane Signet
  Creatures: (empty)
  Library: 49 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Fateful Absence; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; Thraben Watcher; Winds of Abandon; Witch Enchanter
  Exile: Authority of the Consuls; Herald's Horn; Starnheim Aspirant; Thalia, Heretic Cathar; The Immortal Sun
Opponent's hand: 7 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; Demolition Field; Glacial Fortress; Island; 3x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Aetherflux Reservoir
  Creatures: Untapped: Human; Rabbit; Serra Faithkeeper
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 8)
  Library: 69 cards
  Command: (empty)
  Graveyard: Armageddon; Dawn's Truce; Depopulate; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Time Wipe; Ugin, Eye of the Storms; Vanish into Eternity
  Exile: Felidar Sovereign; Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
Opponent draws a card
Dovin, Architect of Law ability: Opponent gains 2 life (26)
Opponent draws a card
Aetherflux Reservoir ability: Opponent gains 1 life (27)
Opponent casts Faithbound Judge
Opponent plays Crystal Grotto
Opponent scries bottom: a card
Aetherflux Reservoir ability: Opponent gains 2 life (29)
Approach of the Second Sun: Opponent puts Approach of the Second Sun into library
Opponent gains 7 life (36)

-- Combat - attackers --
Opponent attacks me with Rabbit; Human; and Serra Faithkeeper

-- Combat - damage --
Rabbit deals 1 damage to me
Serra Faithkeeper deals 4 damage to me
Human deals 1 damage to me
I lose 6 life (61)

=== Turn 36: Me ===
My hand: Archangel of Thune; Archangel of Tithes; Exalted Sunborn
My board:
  Lands: Untapped: Labyrinth of Skophos; 2x Plains; Tyrite Sanctum
  Artifacts/Enchantments: Untapped: Arcane Signet
  Creatures: (empty)
  Library: 49 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Fateful Absence; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; Thraben Watcher; Winds of Abandon; Witch Enchanter
  Exile: Authority of the Consuls; Herald's Horn; Starnheim Aspirant; Thalia, Heretic Cathar; The Immortal Sun
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Tapped: Capital City; Command Tower; Crystal Grotto; Demolition Field; Glacial Fortress; Island; 3x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Aetherflux Reservoir
  Creatures: Untapped: Faithbound Judge (summoning sick); Serra Faithkeeper; Tapped: Human; Rabbit
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 9)
  Library: 68 cards
  Command: (empty)
  Graveyard: Armageddon; Dawn's Truce; Depopulate; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Time Wipe; Ugin, Eye of the Storms; Vanish into Eternity
  Exile: Felidar Sovereign; Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw Plains
I play Plains
I cast Archangel of Thune

=== Turn 37: Opponent ===
My hand: Archangel of Tithes; Exalted Sunborn
My board:
  Lands: Untapped: Plains; Tapped: Labyrinth of Skophos; 2x Plains; Tyrite Sanctum
  Artifacts/Enchantments: Tapped: Arcane Signet
  Creatures: Untapped: Archangel of Thune (summoning sick)
  Library: 48 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Fateful Absence; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; Thraben Watcher; Winds of Abandon; Witch Enchanter
  Exile: Authority of the Consuls; Herald's Horn; Starnheim Aspirant; Thalia, Heretic Cathar; The Immortal Sun
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; Crystal Grotto; Demolition Field; Glacial Fortress; Island; 3x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Aetherflux Reservoir
  Creatures: Untapped: Faithbound Judge; Human; Rabbit; Serra Faithkeeper
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 9)
  Library: 68 cards
  Command: (empty)
  Graveyard: Armageddon; Dawn's Truce; Depopulate; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Time Wipe; Ugin, Eye of the Storms; Vanish into Eternity
  Exile: Felidar Sovereign; Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
Opponent draws a card
Aetherflux Reservoir ability: Opponent gains 1 life (37)
Opponent casts Swords to Plowshares targeting Archangel of Thune
Swords to Plowshares exiles Archangel of Thune
I gain 3 life (64)
Dovin, Architect of Law ability: Opponent gains 2 life (39)
Opponent draws a card
Opponent plays Detection Tower

-- Combat - attackers --
Opponent attacks me with Rabbit; Human; and Serra Faithkeeper

-- Combat - damage --
Rabbit deals 1 damage to me
Serra Faithkeeper deals 4 damage to me
Human deals 1 damage to me
I lose 6 life (58)

=== Turn 38: Me ===
My hand: Archangel of Tithes; Exalted Sunborn
My board:
  Lands: Untapped: Labyrinth of Skophos; 3x Plains; Tyrite Sanctum
  Artifacts/Enchantments: Untapped: Arcane Signet
  Creatures: (empty)
  Library: 48 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Fateful Absence; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; Thraben Watcher; Winds of Abandon; Witch Enchanter
  Exile: Archangel of Thune; Authority of the Consuls; Herald's Horn; Starnheim Aspirant; Thalia, Heretic Cathar; The Immortal Sun
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; Crystal Grotto; Demolition Field; Detection Tower; Glacial Fortress; Island; 2x Plains; Radiant Fountain; Tapped: Plains
  Artifacts/Enchantments: Untapped: Aetherflux Reservoir
  Creatures: Untapped: Faithbound Judge (Judgment counter); Serra Faithkeeper; Tapped: Human; Rabbit
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 10)
  Library: 66 cards
  Command: (empty)
  Graveyard: Armageddon; Dawn's Truce; Depopulate; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Swords to Plowshares; Time Wipe; Ugin, Eye of the Storms; Vanish into Eternity
  Exile: Felidar Sovereign; Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw Plains
I play Plains
I cast Archangel of Tithes

-- Combat - beginning --
Demolition Field ability: Opponent sacrifices Demolition Field
Demolition Field ability destroys Tyrite Sanctum
Demolition Field ability: I put Plains into the battlefield
Demolition Field ability: I shuffle my library
Demolition Field ability: Opponent puts Plains into the battlefield
Demolition Field ability: Opponent shuffles their library
Aetherflux Reservoir ability: Opponent gains 1 life (40)
I reveal Arcane Signet
Cyclonic Rift: I return Arcane Signet to hand
I reveal Archangel of Tithes
Cyclonic Rift: I return Archangel of Tithes to hand
Opponent casts Cyclonic Rift

-- Postcombat main --
I cast Arcane Signet

=== Turn 39: Opponent ===
My hand: Archangel of Tithes; Exalted Sunborn
My board:
  Lands: Untapped: Plains; Tapped: Labyrinth of Skophos; 4x Plains
  Artifacts/Enchantments: Untapped: Arcane Signet
  Creatures: (empty)
  Library: 46 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Fateful Absence; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; Thraben Watcher; Tyrite Sanctum; Winds of Abandon; Witch Enchanter
  Exile: Archangel of Thune; Authority of the Consuls; Herald's Horn; Starnheim Aspirant; Thalia, Heretic Cathar; The Immortal Sun
Opponent's hand: 5 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; Crystal Grotto; Detection Tower; Glacial Fortress; Island; 4x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Aetherflux Reservoir
  Creatures: Untapped: Faithbound Judge (Judgment counter); Human; Rabbit; Serra Faithkeeper
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 10)
  Library: 65 cards
  Command: (empty)
  Graveyard: Armageddon; Cyclonic Rift; Dawn's Truce; Demolition Field; Depopulate; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Swords to Plowshares; Time Wipe; Ugin, Eye of the Storms; Vanish into Eternity
  Exile: Felidar Sovereign; Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
Opponent draws a card
Aetherflux Reservoir ability: Opponent gains 1 life (41)
Opponent casts Sigarda's Splendor
Dovin, Architect of Law ability: Opponent gains 2 life (43)
Opponent draws a card
Aetherflux Reservoir ability: Opponent gains 2 life
Sigarda's Splendor trigger: Opponent gains 1 life (46)
Opponent casts Emergency Eject targeting Arcane Signet
Emergency Eject destroys Arcane Signet

-- Combat - attackers --
Opponent attacks me with Rabbit; Human; and Serra Faithkeeper

-- Combat - damage --
Rabbit deals 1 damage to me
Serra Faithkeeper deals 4 damage to me
Human deals 1 damage to me
I lose 6 life (52)

-- Postcombat main --
Aetherflux Reservoir ability: Opponent gains 3 life
Sigarda's Splendor trigger: Opponent gains 1 life (50)
Opponent casts Supreme Verdict
Supreme Verdict destroys Rabbit
Supreme Verdict destroys Human
Supreme Verdict destroys Invasion of Dominaria
Supreme Verdict destroys Faithbound Judge

=== Turn 40: Me ===
My hand: Archangel of Tithes; Exalted Sunborn
My board:
  Lands: Untapped: Labyrinth of Skophos; 5x Plains
  Artifacts/Enchantments: Untapped: Lander
  Creatures: (empty)
  Library: 46 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Arcane Signet; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Fateful Absence; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; Thraben Watcher; Tyrite Sanctum; Winds of Abandon; Witch Enchanter
  Exile: Archangel of Thune; Authority of the Consuls; Herald's Horn; Starnheim Aspirant; Thalia, Heretic Cathar; The Immortal Sun
Opponent's hand: 4 unknown cards
Opponent's board:
  Lands: Tapped: Capital City; Command Tower; Crystal Grotto; Detection Tower; Glacial Fortress; Island; 4x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Aetherflux Reservoir; Sigarda's Splendor
  Creatures: (empty)
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 11)
  Library: 63 cards
  Command: (empty)
  Graveyard: Armageddon; Cyclonic Rift; Dawn's Truce; Demolition Field; Depopulate; Emergency Eject; Faithbound Judge; Invasion of Dominaria; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Supreme Verdict; Swords to Plowshares; Time Wipe; Ugin, Eye of the Storms; Vanish into Eternity
  Exile: Felidar Sovereign; Ultima
  Available Resources:
    Other playable cards:
      Faithbound Judge [disturb from graveyard, cost not checked]
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw Plains
I play Plains
I cast Archangel of Tithes
Lander trigger: I sacrifice Lander
Lander trigger: I put Plains into the battlefield
Lander trigger: I shuffle my library

=== Turn 41: Opponent ===
My hand: Exalted Sunborn
My board:
  Lands: Untapped: Plains; Tapped: Labyrinth of Skophos; 6x Plains
  Artifacts/Enchantments: (empty)
  Creatures: Untapped: Archangel of Tithes (summoning sick)
  Library: 44 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Arcane Signet; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Fateful Absence; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; Thraben Watcher; Tyrite Sanctum; Winds of Abandon; Witch Enchanter
  Exile: Archangel of Thune; Authority of the Consuls; Herald's Horn; Starnheim Aspirant; Thalia, Heretic Cathar; The Immortal Sun
Opponent's hand: 4 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; Crystal Grotto; Detection Tower; Glacial Fortress; Island; 4x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Aetherflux Reservoir; Sigarda's Splendor
  Creatures: (empty)
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 11)
  Library: 63 cards
  Command: (empty)
  Graveyard: Armageddon; Cyclonic Rift; Dawn's Truce; Demolition Field; Depopulate; Emergency Eject; Faithbound Judge; Invasion of Dominaria; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Supreme Verdict; Swords to Plowshares; Time Wipe; Ugin, Eye of the Storms; Vanish into Eternity
  Exile: Felidar Sovereign; Ultima
  Available Resources:
    Other playable cards:
      Faithbound Judge [disturb from graveyard, cost not checked]
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - upkeep --
Opponent draws a card

-- Beginning - draw --
Opponent draws a card
Dovin, Architect of Law ability: Opponent gains 2 life (52)
Opponent draws a card
Aetherflux Reservoir ability: Opponent gains 1 life
Sigarda's Splendor trigger: Opponent gains 1 life (54)
Opponent casts Descend upon the Sinful
Descend upon the Sinful exiles Archangel of Tithes
Opponent plays Rumble Arena
Opponent scries bottom: a card

=== Turn 42: Me ===
My hand: Exalted Sunborn
My board:
  Lands: Untapped: Labyrinth of Skophos; 7x Plains
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Library: 44 cards
  Command: Giada, Font of Hope [next commander tax +6]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Arcane Signet; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Fateful Absence; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; Thraben Watcher; Tyrite Sanctum; Winds of Abandon; Witch Enchanter
  Exile: Archangel of Thune; Archangel of Tithes; Authority of the Consuls; Herald's Horn; Starnheim Aspirant; Thalia, Heretic Cathar; The Immortal Sun
Opponent's hand: 5 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; Crystal Grotto; Detection Tower; Glacial Fortress; Rumble Arena; Tapped: Island; 4x Plains; Radiant Fountain
  Artifacts/Enchantments: Untapped: Aetherflux Reservoir; Sigarda's Splendor
  Creatures: Untapped: Angel (summoning sick)
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 12)
  Library: 60 cards
  Command: (empty)
  Graveyard: Armageddon; Cyclonic Rift; Dawn's Truce; Demolition Field; Depopulate; Descend upon the Sinful; Emergency Eject; Faithbound Judge; Invasion of Dominaria; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Supreme Verdict; Swords to Plowshares; Time Wipe; Ugin, Eye of the Storms; Vanish into Eternity
  Exile: Felidar Sovereign; Ultima
  Available Resources:
    Other playable cards:
      Faithbound Judge [disturb from graveyard, cost not checked]
Current State:
  My next commander tax for Giada, Font of Hope is +6
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw Plains
I play Plains
I cast Giada, Font of Hope from command zone; commander cast #4; next commander tax +8

=== Turn 43: Opponent ===
My hand: Exalted Sunborn
My board:
  Lands: Untapped: Plains; Tapped: Labyrinth of Skophos; 7x Plains
  Artifacts/Enchantments: (empty)
  Creatures: Untapped: Giada, Font of Hope (summoning sick)
  Library: 43 cards
  Command: (empty)
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Arcane Signet; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Fateful Absence; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; Thraben Watcher; Tyrite Sanctum; Winds of Abandon; Witch Enchanter
  Exile: Archangel of Thune; Archangel of Tithes; Authority of the Consuls; Herald's Horn; Starnheim Aspirant; Thalia, Heretic Cathar; The Immortal Sun
Opponent's hand: 5 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; Crystal Grotto; Detection Tower; Glacial Fortress; Island; 4x Plains; Radiant Fountain; Rumble Arena
  Artifacts/Enchantments: Untapped: Aetherflux Reservoir; Sigarda's Splendor
  Creatures: Untapped: Angel
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 12)
  Library: 60 cards
  Command: (empty)
  Graveyard: Armageddon; Cyclonic Rift; Dawn's Truce; Demolition Field; Depopulate; Descend upon the Sinful; Emergency Eject; Faithbound Judge; Invasion of Dominaria; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Supreme Verdict; Swords to Plowshares; Time Wipe; Ugin, Eye of the Storms; Vanish into Eternity
  Exile: Felidar Sovereign; Ultima
  Available Resources:
    Other playable cards:
      Faithbound Judge [disturb from graveyard, cost not checked]
Current State:
  My next commander tax for Giada, Font of Hope is +8
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - upkeep --
Opponent draws a card

-- Beginning - draw --
Opponent draws a card
Dovin, Architect of Law ability: Opponent gains 2 life (56)
Opponent draws a card
Opponent plays Zhalfirin Void
Opponent scries bottom: a card
Aetherflux Reservoir ability: Opponent gains 1 life
Sigarda's Splendor trigger: Opponent gains 1 life (58)
Opponent casts Sinner's Judgment targeting me
Sinner's Judgment becomes attached to me

-- Combat - attackers --
Opponent attacks me with Angel

-- Combat - damage --
Angel deals 4 damage to me
I lose 4 life (48)

-- Postcombat main --
Aetherflux Reservoir ability: Opponent gains 2 life
Sigarda's Splendor trigger: Opponent gains 1 life (61)
Opponent casts Fumigate
Fumigate destroys Angel
Fumigate destroys Giada, Font of Hope
Opponent gains 2 life (63)
Giada, Font of Hope moves to command zone

=== Turn 44: Me ===
My hand: Exalted Sunborn
My board:
  Lands: Untapped: Labyrinth of Skophos; 8x Plains
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Attached: Auras: Sinner's Judgment
  Library: 43 cards
  Command: Giada, Font of Hope [next commander tax +8]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Arcane Signet; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Fateful Absence; Flowering of the White Tree; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; Thraben Watcher; Tyrite Sanctum; Winds of Abandon; Witch Enchanter
  Exile: Archangel of Thune; Archangel of Tithes; Authority of the Consuls; Herald's Horn; Starnheim Aspirant; Thalia, Heretic Cathar; The Immortal Sun
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Detection Tower; Tapped: Capital City; Command Tower; Crystal Grotto; Glacial Fortress; Island; 4x Plains; Radiant Fountain; Rumble Arena; Zhalfirin Void
  Artifacts/Enchantments: Untapped: Aetherflux Reservoir; Sigarda's Splendor; Sinner's Judgment (Judgment counters: 2)
  Creatures: (empty)
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 13)
  Library: 57 cards
  Command: (empty)
  Graveyard: Armageddon; Cyclonic Rift; Dawn's Truce; Demolition Field; Depopulate; Descend upon the Sinful; Emergency Eject; Fumigate; Invasion of Dominaria; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Supreme Verdict; Swords to Plowshares; Time Wipe; Ugin, Eye of the Storms; Vanish into Eternity
  Exile: Felidar Sovereign; Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +8
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw Get Lost
I cast Get Lost targeting Sinner's Judgment; Faithbound Judge
Get Lost destroys Faithbound Judge

=== Turn 45: Opponent ===
My hand: Exalted Sunborn
My board:
  Lands: Untapped: Labyrinth of Skophos; 6x Plains; Tapped: 2x Plains
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Library: 42 cards
  Command: Giada, Font of Hope [next commander tax +8]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Arcane Signet; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Fateful Absence; Flowering of the White Tree; Get Lost; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; Thraben Watcher; Tyrite Sanctum; Winds of Abandon; Witch Enchanter
  Exile: Archangel of Thune; Archangel of Tithes; Authority of the Consuls; Herald's Horn; Starnheim Aspirant; Thalia, Heretic Cathar; The Immortal Sun
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; Crystal Grotto; Detection Tower; Glacial Fortress; Island; 4x Plains; Radiant Fountain; Rumble Arena; Zhalfirin Void
  Artifacts/Enchantments: Untapped: Aetherflux Reservoir; 2x Map; Sigarda's Splendor
  Creatures: (empty)
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 13)
  Library: 57 cards
  Command: (empty)
  Graveyard: Armageddon; Cyclonic Rift; Dawn's Truce; Demolition Field; Depopulate; Descend upon the Sinful; Emergency Eject; Fumigate; Invasion of Dominaria; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Supreme Verdict; Swords to Plowshares; Time Wipe; Ugin, Eye of the Storms; Vanish into Eternity
  Exile: Faithbound Judge; Felidar Sovereign; Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +8
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - upkeep --
Opponent draws a card

-- Beginning - draw --
Opponent draws a card
Aetherflux Reservoir ability: Opponent gains 1 life
Sigarda's Splendor trigger: Opponent gains 1 life (65)
Opponent casts Avacyn, Angel of Hope
Dovin, Architect of Law ability: Opponent gains 2 life (67)
Opponent draws a card
Aetherflux Reservoir ability: Opponent gains 2 life (69)
Opponent casts Arcane Signet
Opponent plays Plains

=== Turn 46: Me ===
My hand: Exalted Sunborn
My board:
  Lands: Untapped: Labyrinth of Skophos; 8x Plains
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Library: 42 cards
  Command: Giada, Font of Hope [next commander tax +8]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Arcane Signet; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Fateful Absence; Flowering of the White Tree; Get Lost; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; Thraben Watcher; Tyrite Sanctum; Winds of Abandon; Witch Enchanter
  Exile: Archangel of Thune; Archangel of Tithes; Authority of the Consuls; Herald's Horn; Starnheim Aspirant; Thalia, Heretic Cathar; The Immortal Sun
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Command Tower; Detection Tower; Glacial Fortress; Plains; Tapped: Capital City; Crystal Grotto; Island; 4x Plains; Radiant Fountain; Rumble Arena; Zhalfirin Void
  Artifacts/Enchantments: Untapped: Aetherflux Reservoir; Arcane Signet; 2x Map; Sigarda's Splendor
  Creatures: Untapped: Avacyn, Angel of Hope (summoning sick)
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 14)
  Library: 54 cards
  Command: (empty)
  Graveyard: Armageddon; Cyclonic Rift; Dawn's Truce; Demolition Field; Depopulate; Descend upon the Sinful; Emergency Eject; Fumigate; Invasion of Dominaria; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Supreme Verdict; Swords to Plowshares; Time Wipe; Ugin, Eye of the Storms; Vanish into Eternity
  Exile: Faithbound Judge; Felidar Sovereign; Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +8
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw Angel of Sanctions
I cast Angel of Sanctions
Angel of Sanctions trigger exiles Avacyn, Angel of Hope

=== Turn 47: Opponent ===
My hand: Exalted Sunborn
My board:
  Lands: Untapped: 4x Plains; Tapped: Labyrinth of Skophos; 4x Plains
  Artifacts/Enchantments: (empty)
  Creatures: Untapped: Angel of Sanctions (summoning sick)
  Library: 41 cards
  Command: Giada, Font of Hope [next commander tax +8]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Arcane Signet; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Emeria's Call; Esper Sentinel; Fateful Absence; Flowering of the White Tree; Get Lost; Grand Abolisher; Linvala, Keeper of Silence; Path to Exile; 10x Plains; Platinum Angel; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; Thraben Watcher; Tyrite Sanctum; Winds of Abandon; Witch Enchanter
  Exile: Archangel of Thune; Archangel of Tithes; Authority of the Consuls; Herald's Horn; Starnheim Aspirant; Thalia, Heretic Cathar; The Immortal Sun
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; Crystal Grotto; Detection Tower; Glacial Fortress; Island; 5x Plains; Radiant Fountain; Rumble Arena; Zhalfirin Void
  Artifacts/Enchantments: Untapped: Aetherflux Reservoir; Arcane Signet; 2x Map; Sigarda's Splendor
  Creatures: (empty)
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 14)
  Library: 54 cards
  Command: (empty)
  Graveyard: Armageddon; Cyclonic Rift; Dawn's Truce; Demolition Field; Depopulate; Descend upon the Sinful; Emergency Eject; Fumigate; Invasion of Dominaria; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Supreme Verdict; Swords to Plowshares; Time Wipe; Ugin, Eye of the Storms; Vanish into Eternity
  Exile: Avacyn, Angel of Hope; Faithbound Judge; Felidar Sovereign; Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +8
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - upkeep --
Opponent draws a card

-- Beginning - draw --
Opponent draws a card
Dovin, Architect of Law ability: Opponent gains 2 life (71)
Opponent draws a card
Aetherflux Reservoir ability: Opponent gains 1 life (72)
3x Opponent draws a card
Opponent casts Concentrate
Aetherflux Reservoir ability: Opponent gains 2 life
Sigarda's Splendor trigger: Opponent gains 1 life (75)
Opponent casts Hope Estheim
Opponent plays Plains
Map ability: Opponent sacrifices Map
Opponent reveals Reliquary Tower
Map ability: Opponent puts Reliquary Tower into hand
Map ability: Opponent sacrifices Map
Opponent reveals Plains
Map ability: Opponent puts Plains into hand
Aetherflux Reservoir ability: Opponent gains 3 life
Sigarda's Splendor trigger: Opponent gains 1 life (79)
Opponent casts Path to Exile targeting Angel of Sanctions
Path to Exile exiles Angel of Sanctions
Angel of Sanctions trigger: Opponent returns Avacyn, Angel of Hope to the battlefield
Path to Exile: I put Plains into the battlefield
Path to Exile: I shuffle my library
Aetherflux Reservoir ability: Opponent gains 4 life
Sigarda's Splendor trigger: Opponent gains 1 life (84)
Opponent casts Day of Judgment

-- Ending - cleanup --
Hope Estheim triggers resolve; I mill 15 cards

=== Turn 48: Me ===
My hand: Exalted Sunborn
My board:
  Lands: Untapped: Labyrinth of Skophos; 9x Plains
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Library: 25 cards
  Command: Giada, Font of Hope [next commander tax +8]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Arcane Signet; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Champions of Tyr; Emeria's Call; Enduring Angel; Esper Sentinel; Exemplar of Light; Fateful Absence; Flowering of the White Tree; Get Lost; Grand Abolisher; Herald of Vengeance; Linvala, Keeper of Silence; Lyra Dawnbringer; Metropolis Reformer; Mox Amber; Path to Exile; 14x Plains; Platinum Angel; Radiant Fountain; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; The Book of Exalted Deeds; Thraben Watcher; Tyrite Sanctum; Valorous Stance; Winds of Abandon; Witch Enchanter; Youthful Valkyrie
  Exile: Angel of Sanctions; Archangel of Thune; Archangel of Tithes; Authority of the Consuls; Herald's Horn; Starnheim Aspirant; Thalia, Heretic Cathar; The Immortal Sun
Opponent's hand: 7 unknown cards
Opponent's board:
  Lands: Untapped: Crystal Grotto; Detection Tower; Rumble Arena; Tapped: Capital City; Command Tower; Glacial Fortress; Island; 6x Plains; Radiant Fountain; Zhalfirin Void
  Artifacts/Enchantments: Untapped: Aetherflux Reservoir; Sigarda's Splendor; Tapped: Arcane Signet
  Creatures: Untapped: Avacyn, Angel of Hope (summoning sick); Hope Estheim (summoning sick)
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 15)
  Library: 46 cards
  Command: (empty)
  Graveyard: Armageddon; Concentrate; Cyclonic Rift; Dawn's Truce; Day of Judgment; Demolition Field; Depopulate; Descend upon the Sinful; Emergency Eject; Fumigate; Invasion of Dominaria; Path to Exile; 2x Plains; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Supreme Verdict; Swords to Plowshares; Time Wipe; Ugin, Eye of the Storms; Vanish into Eternity
  Exile: Faithbound Judge; Felidar Sovereign; Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +8
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - upkeep --
2x Opponent discards Plains

-- Beginning - draw --
I draw Plains
I play Plains
I cast Giada, Font of Hope

=== Turn 49: Opponent ===
My hand: Exalted Sunborn
My board:
  Lands: Untapped: Labyrinth of Skophos; 10x Plains
  Artifacts/Enchantments: (empty)
  Creatures: Untapped: Giada, Font of Hope (summoning sick)
  Library: 24 cards
  Command: Giada, Font of Hope [next commander tax +8]
  Graveyard: Angel of Destiny; Angel of Eternal Dawn; Angel of the Dire Hour; Arcane Signet; Bishop of Wings; Blind Obedience; Bonders' Enclave; Boon-Bringer Valkyrie; Cavern of Souls; Champions of Tyr; Emeria's Call; Enduring Angel; Esper Sentinel; Exemplar of Light; Fateful Absence; Flowering of the White Tree; Get Lost; Grand Abolisher; Herald of Vengeance; Linvala, Keeper of Silence; Lyra Dawnbringer; Metropolis Reformer; Mox Amber; Path to Exile; 14x Plains; Platinum Angel; Radiant Fountain; Reidane, God of the Worthy; Resplendent Angel; Righteous Valkyrie; Serra Paragon; Sheltered by Ghosts; Spectacular Tactics; Steel Seraph; Stroke of Midnight; The Book of Exalted Deeds; Thraben Watcher; Tyrite Sanctum; Valorous Stance; Winds of Abandon; Witch Enchanter; Youthful Valkyrie
  Exile: Angel of Sanctions; Archangel of Thune; Archangel of Tithes; Authority of the Consuls; Herald's Horn; Starnheim Aspirant; Thalia, Heretic Cathar; The Immortal Sun
Opponent's hand: 7 unknown cards
Opponent's board:
  Lands: Untapped: Capital City; Command Tower; Crystal Grotto; Detection Tower; Glacial Fortress; Island; 6x Plains; Radiant Fountain; Rumble Arena; Zhalfirin Void
  Artifacts/Enchantments: Untapped: Aetherflux Reservoir; Arcane Signet; Sigarda's Splendor
  Creatures: Untapped: Avacyn, Angel of Hope; Hope Estheim
  Other: Untapped: Dovin, Architect of Law (Loyalty counters: 15)
  Library: 46 cards
  Command: (empty)
  Graveyard: Armageddon; Concentrate; Cyclonic Rift; Dawn's Truce; Day of Judgment; Demolition Field; Depopulate; Descend upon the Sinful; Emergency Eject; Fumigate; Invasion of Dominaria; Path to Exile; 2x Plains; Revitalize; Season of the Burrow; Sphinx's Tutelage; Starfall Invocation; Supreme Verdict; Swords to Plowshares; Time Wipe; Ugin, Eye of the Storms; Vanish into Eternity
  Exile: Faithbound Judge; Felidar Sovereign; Ultima
Current State:
  My next commander tax for Giada, Font of Hope is +8
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - upkeep --
Opponent draws a card

-- Beginning - draw --
Opponent draws a card
Aetherflux Reservoir ability: Opponent gains 1 life
Sigarda's Splendor trigger: Opponent gains 1 life (86)
Opponent casts The Birth of Meletis
Opponent reveals Plains
The Birth of Meletis trigger: Opponent puts Plains into hand
The Birth of Meletis trigger: Opponent shuffles their library
Opponent plays Reliquary Tower
Aetherflux Reservoir ability: Opponent gains 2 life (88)
Opponent casts All-Fates Scroll
All-Fates Scroll trigger: Opponent sacrifices All-Fates Scroll
11x Opponent draws a card
Aetherflux Reservoir ability: Opponent gains 3 life (91)
Opponent casts Proft's Eidetic Memory
Opponent draws a card
Aetherflux Reservoir ability: Opponent gains 4 life (95)
2x Stock Up: Opponent puts a card into hand
Opponent casts Stock Up
Dovin, Architect of Law ability: Opponent gains 2 life (97)
Opponent draws a card

-- Combat - attackers --
Opponent attacks me with Hope Estheim and Avacyn, Angel of Hope

-- Combat - blockers --
Giada, Font of Hope blocks Hope Estheim

-- Combat - damage --
Hope Estheim deals 16 damage to Giada, Font of Hope (16/2 damage)
Giada, Font of Hope deals 2 damage to Hope Estheim (2/16 damage)
Avacyn, Angel of Hope deals 8 damage to me
I lose 8 life (40)
Opponent gains 16 life (113)
My Giada, Font of Hope dies
Giada, Font of Hope moves to command zone

-- Postcombat main --
Game appears to have ended, but no final GRE result was written to Player.log.
Postgame course/event data includes a loss count after this match.
Final life total is unavailable from the gameplay log.
```

## Debugging Choices

Arena records most gameplay as IDs and structured game state changes. Card names should come from the SQLite card database, not from the raw log.

To inspect a card by name:

```bash
python3 mtga_extract_games.py --last 1 --debug-card "Serra's Emissary"
```

To inspect a card by `grpId`:

```bash
python3 mtga_extract_games.py --last 1 --debug-grpid 75982
```

To look for events that may contain choices or selections:

```bash
python3 mtga_extract_games.py --last 1 --debug-choices
```

To look for target-like payloads for spells and abilities:

```bash
python3 mtga_extract_games.py --last 1 --debug-targets
```

To look for trigger-like events, including creatures entering the battlefield attacking:

```bash
python3 mtga_extract_games.py --last 1 --debug-triggers
```

To print annotation type/category counts and sample payloads:

```bash
python3 mtga_extract_games.py --last 1 --debug-annotations
```

This is meant to help find where Arena records choices like creature type, protection type, modal choices, triggered abilities, or similar decisions.

## Fair Use And Intent

This code was written to parse a plaintext log file that MTG Arena writes to my own machine.

There is no intent to decode, decrypt, or bypass anything. There is no intent to distribute copyrighted material, copy game assets, or change the game. The goal is only to turn my own local game log into a readable text transcript so I can review games I played.

## License

This project is released under the MIT License.

It is provided as-is, with no warranty. If you choose to use it, you are responsible for what you do with it. I am not liable for any damages or problems that come from using this software.
