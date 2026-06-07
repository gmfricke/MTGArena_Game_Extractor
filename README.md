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
- [AI-Assisted Analysis And Responsible Use](#ai-assisted-analysis-and-responsible-use)
- [Development Transparency](#development-transparency)
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

This is a representative completed game extracted with normal transcript options such as:

```bash
python3 mtga_extract_games.py --last 1 --no-resolves
```

```text
===== GAME 122: MATCH 469be241-82e0-4347-b105-4fceb6341f56 =====
Game type: Constructed Brawl (25 starting life)

=== Turn 1: Me ===
My hand: Archangel of Thune; Authority of the Consuls; Banishing Light; Esper Sentinel; 2x Plains; Stroke of Midnight
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
  Command: Errant and Giada
  Graveyard: (empty)
  Exile: (empty)
I play Plains
I cast Authority of the Consuls

=== Turn 2: Opponent ===
My hand: Archangel of Thune; Banishing Light; Esper Sentinel; Plains; Stroke of Midnight
My board:
  Lands: Tapped: Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
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
  Command: Errant and Giada
  Graveyard: (empty)
  Exile: (empty)

-- Beginning - draw --
Opponent draws a card
Opponent plays Command Tower

=== Turn 3: Me ===
My hand: Archangel of Thune; Banishing Light; Esper Sentinel; Plains; Stroke of Midnight
My board:
  Lands: Untapped: Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: (empty)
  Library: 92 cards
  Command: Giada, Font of Hope
  Graveyard: (empty)
  Exile: (empty)
Opponent's hand: 7 unknown cards
Opponent's board:
  Lands: Untapped: Command Tower
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Library: 91 cards
  Command: Errant and Giada
  Graveyard: (empty)
  Exile: (empty)

-- Beginning - draw --
I draw Plains
I play Plains
I cast Esper Sentinel

=== Turn 4: Opponent ===
My hand: Archangel of Thune; Banishing Light; Plains; Stroke of Midnight
My board:
  Lands: Untapped: Plains; Tapped: Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: Untapped: Esper Sentinel (summoning sick)
  Library: 91 cards
  Command: Giada, Font of Hope
  Graveyard: (empty)
  Exile: (empty)
Opponent's hand: 7 unknown cards
Opponent's board:
  Lands: Untapped: Command Tower
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Library: 91 cards
  Command: Errant and Giada
  Graveyard: (empty)
  Exile: (empty)

-- Beginning - draw --
Opponent draws a card
Opponent plays Plains

=== Turn 5: Me ===
My hand: Archangel of Thune; Banishing Light; Plains; Stroke of Midnight
My board:
  Lands: Untapped: 2x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: Untapped: Esper Sentinel
  Library: 91 cards
  Command: Giada, Font of Hope
  Graveyard: (empty)
  Exile: (empty)
Opponent's hand: 7 unknown cards
Opponent's board:
  Lands: Untapped: Command Tower; Plains
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Library: 90 cards
  Command: Errant and Giada
  Graveyard: (empty)
  Exile: (empty)

-- Beginning - draw --
I draw Tyrite Sanctum
I play Plains

-- Combat - attackers --
I attack Opponent with Esper Sentinel

-- Combat - damage --
Esper Sentinel deals 1 damage to Opponent
Opponent loses 1 life (24)

-- Ending - end step --
Authority of the Consuls trigger: I gain 1 life (26)
I cast Giada, Font of Hope from command zone; commander cast #1; next commander tax +2
Opponent casts Cathar Commando

=== Turn 6: Opponent ===
My hand: Archangel of Thune; Banishing Light; Stroke of Midnight; Tyrite Sanctum
My board:
  Lands: Untapped: Plains; Tapped: 2x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: Untapped: Giada, Font of Hope (summoning sick); Tapped: Esper Sentinel
  Library: 90 cards
  Command: (empty)
  Graveyard: (empty)
  Exile: (empty)
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Command Tower; Plains
  Artifacts/Enchantments: (empty)
  Creatures: Untapped: Cathar Commando
  Library: 90 cards
  Command: Errant and Giada
  Graveyard: (empty)
  Exile: (empty)

-- Beginning - draw --
Opponent draws a card

-- Combat - attackers --
Opponent attacks me with Cathar Commando

-- Combat - blockers --
Giada, Font of Hope blocks Cathar Commando

-- Combat - damage --
Cathar Commando deals 3 damage to me
I lose 3 life (23)

-- Postcombat main --
Authority of the Consuls trigger: I gain 1 life (24)
Opponent casts Skycat Sovereign

=== Turn 7: Me ===
My hand: Archangel of Thune; Banishing Light; Stroke of Midnight; Tyrite Sanctum
My board:
  Lands: Untapped: 3x Plains
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: Untapped: Esper Sentinel; Giada, Font of Hope
  Library: 90 cards
  Command: (empty)
  Graveyard: (empty)
  Exile: (empty)
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Tapped: Command Tower; Plains
  Artifacts/Enchantments: (empty)
  Creatures: Tapped: Cathar Commando; Skycat Sovereign (summoning sick)
  Library: 89 cards
  Command: Errant and Giada
  Graveyard: (empty)
  Exile: (empty)

-- Beginning - draw --
I draw Bonders' Enclave
I play Tyrite Sanctum
I cast Archangel of Thune

=== Turn 8: Opponent ===
My hand: Banishing Light; Bonders' Enclave; Stroke of Midnight
My board:
  Lands: Tapped: 3x Plains; Tyrite Sanctum
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: Untapped: Archangel of Thune (+1/+1 from counters) (summoning sick); Esper Sentinel; Tapped: Giada, Font of Hope
  Library: 89 cards
  Command: (empty)
  Graveyard: (empty)
  Exile: (empty)
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Command Tower; Plains
  Artifacts/Enchantments: (empty)
  Creatures: Untapped: Cathar Commando; Skycat Sovereign
  Library: 89 cards
  Command: Errant and Giada
  Graveyard: (empty)
  Exile: (empty)

-- Beginning - draw --
Opponent draws a card
Opponent casts Wayfarer's Bauble

=== Turn 9: Me ===
My hand: Banishing Light; Bonders' Enclave; Stroke of Midnight
My board:
  Lands: Untapped: 3x Plains; Tyrite Sanctum
  Artifacts/Enchantments: Untapped: Authority of the Consuls
  Creatures: Untapped: Archangel of Thune (+1/+1 from counters); Esper Sentinel; Giada, Font of Hope
  Library: 89 cards
  Command: (empty)
  Graveyard: (empty)
  Exile: (empty)
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Tapped: Command Tower; Plains
  Artifacts/Enchantments: Untapped: Wayfarer's Bauble
  Creatures: Untapped: Cathar Commando; Skycat Sovereign
  Library: 88 cards
  Command: Errant and Giada
  Graveyard: (empty)
  Exile: (empty)

-- Beginning - draw --
I draw Plains
I play Plains
I cast Banishing Light
Banishing Light trigger exiles Cathar Commando

-- Combat - attackers --
I attack Opponent with Giada, Font of Hope and Archangel of Thune

-- Combat - damage --
Archangel of Thune deals 4 damage to Opponent
Commander damage: Giada, Font of Hope deals 2 damage to Opponent (2 total)
Opponent loses 6 life (18)
I gain 4 life (28)

=== Turn 10: Opponent ===
My hand: Bonders' Enclave; Stroke of Midnight
My board:
  Lands: Untapped: 2x Plains; Tapped: 2x Plains; Tyrite Sanctum
  Artifacts/Enchantments: Untapped: Authority of the Consuls; Banishing Light
  Creatures: Untapped: Esper Sentinel (+1/+1 from counters); Giada, Font of Hope (+1/+1 from counters); Tapped: Archangel of Thune (+2/+2 from counters)
  Library: 88 cards
  Command: (empty)
  Graveyard: (empty)
  Exile: (empty)
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Command Tower; Plains
  Artifacts/Enchantments: Untapped: Wayfarer's Bauble
  Creatures: Untapped: Skycat Sovereign
  Library: 88 cards
  Command: Errant and Giada
  Graveyard: (empty)
  Exile: Cathar Commando
Current State:
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
Opponent draws a card
Opponent plays Glacial Floodplain
Wayfarer's Bauble trigger: Opponent sacrifices Wayfarer's Bauble
Wayfarer's Bauble trigger: Opponent puts Island into the battlefield
Wayfarer's Bauble trigger: Opponent shuffles their library

=== Turn 11: Me ===
My hand: Bonders' Enclave; Stroke of Midnight
My board:
  Lands: Untapped: 4x Plains; Tyrite Sanctum
  Artifacts/Enchantments: Untapped: Authority of the Consuls; Banishing Light
  Creatures: Untapped: Archangel of Thune (+2/+2 from counters); Esper Sentinel (+1/+1 from counters); Giada, Font of Hope (+1/+1 from counters)
  Library: 88 cards
  Command: (empty)
  Graveyard: (empty)
  Exile: (empty)
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Tapped: Command Tower; Glacial Floodplain; Island; Plains
  Artifacts/Enchantments: (empty)
  Creatures: Untapped: Skycat Sovereign
  Library: 86 cards
  Command: Errant and Giada
  Graveyard: Wayfarer's Bauble
  Exile: Cathar Commando
Current State:
  Giada, Font of Hope has dealt 2 commander damage to Opponent

-- Beginning - draw --
I draw Celestial Vault
I play Bonders' Enclave

-- Combat - attackers --
I attack Opponent with Esper Sentinel; Giada, Font of Hope; and Archangel of Thune

-- Combat - blockers --
Skycat Sovereign blocks Archangel of Thune

-- Combat - damage --
Archangel of Thune deals 5 damage to Skycat Sovereign (5/1 damage)
Skycat Sovereign deals 1 damage to Archangel of Thune (1/6 damage)
Esper Sentinel deals 2 damage to Opponent
Commander damage: Giada, Font of Hope deals 4 damage to Opponent (6 total)
Opponent loses 6 life (12)
I gain 5 life (33)

-- Ending - end step --
Opponent's Skycat Sovereign dies

=== Turn 12: Opponent ===
My hand: Celestial Vault; Stroke of Midnight
My board:
  Lands: Untapped: 3x Plains; Tapped: Bonders' Enclave; Plains; Tyrite Sanctum
  Artifacts/Enchantments: Untapped: Authority of the Consuls; Banishing Light
  Creatures: Untapped: Giada, Font of Hope (+3/+3 from counters); Tapped: Archangel of Thune (+3/+3 from counters); Esper Sentinel (+2/+2 from counters)
  Library: 87 cards
  Command: (empty)
  Graveyard: (empty)
  Exile: (empty)
Opponent's hand: 6 unknown cards
Opponent's board:
  Lands: Untapped: Command Tower; Glacial Floodplain; Island; Plains
  Artifacts/Enchantments: (empty)
  Creatures: (empty)
  Library: 86 cards
  Command: Errant and Giada
  Graveyard: Skycat Sovereign; Wayfarer's Bauble
  Exile: Cathar Commando
Current State:
  Giada, Font of Hope has dealt 6 commander damage to Opponent

-- Beginning - draw --
Opponent draws a card
I draw Radiant Fountain
Ossification becomes attached to Plains
Opponent casts Ossification targeting Plains
Ossification trigger exiles Archangel of Thune
Opponent casts Kor Skyfisher
Authority of the Consuls trigger: I gain 1 life (34)
Opponent reveals Command Tower
Kor Skyfisher trigger: Opponent returns Command Tower to hand
Opponent plays Command Tower
I cast Stroke of Midnight targeting Ossification
Stroke of Midnight destroys Ossification
Ossification trigger: I return Archangel of Thune to the battlefield
Authority of the Consuls trigger: I gain 1 life (35)

-- Combat - beginning --
Opponent concedes
Winner: Me

Match result: Opponent conceded
Match winner: Me
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

## AI-Assisted Analysis And Responsible Use

One of the motivations for producing a plain-text game transcript is that it can be reviewed by other software, including large language models and other AI systems.

For example, a transcript can be used to:

- Review games after they finish.
- Identify key decision points.
- Analyze deck performance over many games.
- Study sequencing, combat, mulligans, and resource management.
- Generate summaries and coaching suggestions.

I primarily use the transcripts for post-game review and experimentation with AI-assisted analysis. You can ask an AI system for feedback and advice after a game, or use the `--live` feature to feed a current transcript into another program for in-game advice. I've certainly benefitted from an AI catching interaction options that I just didn't see and from summarising opponent engines that I wasn't familiar with and suggesting ways to stall them.

### Real-Time Assistance

This tool itself does not make gameplay decisions, interact with MTG Arena, automate actions, or provide recommendations. It only reconstructs information already present in the Arena log and presents it in a human-readable form.

However, because the transcript is plain text, it is technically possible to feed a live transcript into another program and request strategic advice while a game is still in progress.

Players already appear to be pasting Arena screenshots into LLMs to get feedback. That can be useful for review, but it is usually too slow and cumbersome for interactive play. A side effect of this logger is that the game state can be provided as text while the game is happening, which means an LLM can potentially provide real-time assistance. That changes the nature of AI assistance from post-game analysis into something closer to having the LLM play the game for you.

Whether this is appropriate depends on the rules, policies, and expectations of the platform, event, or community in which the game is being played.

A useful analogy is computer chess. Modern chess engines are invaluable for post-game analysis, training, and study. At the same time, receiving engine recommendations during a competitive game is generally considered outside assistance and is prohibited in most organized play.

Many players view real-time AI assistance in card games similarly. Post-game analysis is widely accepted. Real-time strategic advice during competitive play is often considered unfair.

Users are responsible for understanding and complying with the rules of any platform, tournament, league, or event in which they participate. Sadly, using LLMs to assist in MTG games runs the risk of an AI arms race where play comes down to who has the best AI assistant. Chess recognises the role of AI in modern play but is still popular for play by unassisted humans. I hope it works out that way for MTG.

This project is intended primarily as a logging, archival, debugging, and post-game analysis tool.

## Development Transparency

This project was written with the assistance of Codex.

I have been a programmer since I was 12, so for more than 40 years at the time this README was written. LLM-assisted coding is currently still sometimes controversial, and I think it is worth being explicit about how I used it here. Just as it took years for assembly programmers to come to trust and use compilers after their introduction but now all programmers use compilers, similarly, soon (and already in many large companies) the question will not be "why are you using LLMs to code" it will be "why *aren't* you using LLMs to code". I expect to look back at this section in 10 years and wonder why I bothered to mention codex at all.  

I used this project in part to see how far I could go with Codex in a fairly deterministic, low-stakes, and easily evaluated domain. MTG Arena logs are obscure and noisy, but the output is testable: either the transcript matches what happened in the game, or it does not.

With Codex, I was able to produce something I find very helpful in a few days. Without that assistance, this would probably have taken me months, or more likely I would have given up while reading through obscure JSON output and trying to find the patterns by hand.

## Fair Use And Intent

This code was written to parse a plaintext log file that MTG Arena writes to my own machine.

There is no intent to decode, decrypt, or bypass anything. There is no intent to distribute copyrighted material, copy game assets, or change the game. The goal is only to turn my own local game log into a readable text transcript so I can review games I played.

## License

This project is released under the MIT License.

The spirit of the MIT License is simple: keep the copyright notice, don’t sue previous authors if there are problems, and otherwise do whatever you like with the code: use it, modify it, redistribute it, or build on it. 

If you contribute to the project, feel free to add your own copyright notice for your contributions. You may license your own work however you choose, including under the MIT License, a commercial license, or another open-source license. Just keep the original copyright notice and MIT license text intact. The ability to use whatever license you like for your contributions is what makes the MIT license more permissive than Copyleft licenses. 

### MIT License

Copyright (c) Copyright (c) 2026 Matthew Fricke

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

*The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.*

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
