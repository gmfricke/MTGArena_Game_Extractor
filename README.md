# MTGArena_Game_Extractor

I wanted a tool that gave me my MTG Arena games in plain text so I could review them later and pass them into other software. I could not find anything straightforward that did this, so I wrote this Python program.

Since this tools is reading logs from a game with frequent updates I expect it to need updating a lot to keep up. 

MTG Arena writes a lot of useful information into `Player.log`, but it is buried in large JSON messages and most of the gameplay events use internal IDs instead of card names. This script reads the log, follows the game state messages, and uses the local Arena card database to translate card `grpId` values into readable card names.

The result is a transcript that looks more like:

```text
===== GAME 24: MATCH afae4493-13bd-43c0-8465-9490c0a71329 =====
I mulligan (kept 7 cards)

=== Turn 1: Opponent ===
My side:
  Board:
    Lands: (empty)
    Artifacts/Enchantments: (empty)
    Creatures: (empty)
  Hand: Boon-Bringer Valkyrie; Esper Sentinel; 2x Plains; Stroke of Midnight; Swords to Plowshares; Thalia, Heretic Cathar
  Library: 92 cards
  Command: Giada, Font of Hope
  Graveyard: (empty)
  Exile: (empty)
Opponent:
  Board:
    Lands: (empty)
    Artifacts/Enchantments: (empty)
    Creatures: (empty)
  Hand: 7 unknown cards
  Library: 92 cards
  Command: Kefka, Court Mage
  Graveyard: (empty)
  Exile: (empty)
Opponent plays Raucous Theater

=== Turn 2: Me ===
My side:
  Board:
    Lands: (empty)
    Artifacts/Enchantments: (empty)
    Creatures: (empty)
  Hand: Boon-Bringer Valkyrie; Esper Sentinel; 2x Plains; Stroke of Midnight; Swords to Plowshares; Thalia, Heretic Cathar
  Library: 92 cards
  Command: Giada, Font of Hope
  Graveyard: (empty)
  Exile: (empty)
Opponent:
  Board:
    Lands: Tapped: Raucous Theater
    Artifacts/Enchantments: (empty)
    Creatures: (empty)
  Hand: 6 unknown cards
  Library: 91 cards
  Command: Kefka, Court Mage
  Graveyard: Mischievous Mystic
  Exile: (empty)
I play Plains
I cast Esper Sentinel
Esper Sentinel resolves

=== Turn 3: Opponent ===
My side:
  Board:
    Lands: Tapped: Plains
    Artifacts/Enchantments: (empty)
    Creatures: Untapped: Esper Sentinel (summoning sick)
  Hand: Boon-Bringer Valkyrie; Champions of Tyr; Plains; Stroke of Midnight; Swords to Plowshares; Thalia, Heretic Cathar
  Library: 91 cards
  Command: Giada, Font of Hope
  Graveyard: (empty)
  Exile: (empty)
Opponent:
  Board:
    Lands: Untapped: Raucous Theater
    Artifacts/Enchantments: (empty)
    Creatures: (empty)
  Hand: 6 unknown cards
  Library: 91 cards
  Command: Kefka, Court Mage
  Graveyard: Mischievous Mystic
  Exile: (empty)
Opponent plays Riverpyre Verge
Opponent casts Depressurize targeting Esper Sentinel
Depressurize destroys Esper Sentinel
Depressurize resolves

```

It can also print board state, hands, graveyards, exile, commanders, attacks, blocks, life changes, mulligans, and match results when those details are available in the log.

It also tries to capture player choices when Arena records them in the structured game events. For example, it can show Serra's Emissary choosing Creature, or Patchwork Banner, Vanquisher's Banner, and Cavern of Souls choosing Angel.

The parser is starting to track important continuous effects too. It can show active effects like protection from creatures from Serra's Emissary, and temporary effects from Teferi's Protection such as permanents phasing out, protection from everything, and the life total not changing until the next turn.

It also tracks commander recasts, the next commander tax, and commander combat damage when those events are visible in Arena's game log. Player counters such as poison, energy, and experience are wired into the state model, but the parser only reports them when Arena exposes an unambiguous player counter event.

## Why?

Arena games go by quickly, and the client does not give me a simple text transcript after the game. A plain text transcript is useful because I can:

- review a game without watching a replay
- search for key turns or cards
- compare what I thought happened with what actually happened
- feed the transcript into other tools
- debug weird board states or decisions

I looked briefly at alternatives but non did what I wanted and I sometimes had to pay quite a bit of money to find that out. 

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

The program will look in the normal macOS locations for `Player.log` and the newest `Raw_CardDatabase_*.mtga` file. If it finds `Player-prev.log` next to `Player.log`, it reads that first so rotated Arena logs are included too. If it cannot find the needed paths, it will tell you what paths to set.

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

Use this for every game in `Player.log` and `Player-prev.log`:

```bash
python3 mtga_extract_games.py --all --no-resolves
```

Use this to save output to a text file:

```bash
python3 mtga_extract_games.py --last 3 --no-resolves > mtga_transcript.txt
```

The most useful options are:

- `--last 1`: show the most recent game
- `--last 3`: show the last three games
- `--all`: show every game in the current and previous log files
- `--first 3`: show the first three games
- `--nth-from-start 4`: show only game 4 from the log
- `--nth-from-end 2`: show the next-to-last game
- `--range 3 5`: show games 3 through 5
- `--live`: show the current game from its start, then print new transcript lines as Arena writes them
- `--no-resolves`: hide routine "resolves" lines
- `--no-turn-state`: hide board and hand snapshots
- `--no-progress`: hide the progress bar

`--select 4` still works as an older name for `--nth-from-start 4`.

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

To look for trigger-like events, including creatures entering the battlefield attacking:

```bash
python3 mtga_extract_games.py --last 1 --debug-triggers
```

This is meant to help find where Arena records choices like creature type, protection type, modal choices, triggered abilities, or similar decisions.

## Fair Use And Intent

This code was written to parse a plaintext log file that MTG Arena writes to my own machine.

There is no intent to decode, decrypt, or bypass anything. There is no intent to distribute copyrighted material, copy game assets, or change the game. The goal is only to turn my own local game log into a readable text transcript so I can review games I played.

## License

This project is released under the MIT License.

It is provided as-is, with no warranty. If you choose to use it, you are responsible for what you do with it. I am not liable for any damages or problems that come from using this software.
