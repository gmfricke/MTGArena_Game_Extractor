# MTGArena_Game_Extractor

I wanted a tool that gave me my MTG Arena games in plain text so I could review them later and pass them into other software.

I could not find anything straightforward that did this, so I wrote this Python program.

MTG Arena writes a lot of useful information into `Player.log`, but it is buried in large JSON messages and most of the gameplay events use internal IDs instead of card names. This script reads the log, follows the game state messages, and uses the local Arena card database to translate card `grpId` values into readable card names.

The result is a transcript that looks more like:

```text
=== Turn 3: Me ===
Me plays Snow-Covered Plains
Me casts Giada, Font of Hope from command zone

=== Turn 4: Opponent ===
Opponent plays Island
Opponent casts Arcane Signet
```

It can also print board state, hands, graveyards, exile, commanders, attacks, blocks, life changes, mulligans, and match results when those details are available in the log.

## Why This Is Interesting

Arena games go by quickly, and the client does not give me a simple text transcript after the game. A plain text transcript is useful because I can:

- review a game without watching a replay
- search for key turns or cards
- compare what I thought happened with what actually happened
- feed the transcript into other tools
- debug weird board states or decisions

The script is also useful if you are curious about how MTG Arena represents games internally. There are debug modes for inspecting annotations, raw game events, card objects, and player choice records.

## Basic Usage

Set paths for your Arena log and card database:

```bash
LOG="$HOME/Library/Logs/Wizards Of The Coast/MTGA/Player.log"
CARDDB="$HOME/Library/Application Support/com.wizards.mtga/Downloads/Raw/Raw_CardDatabase_18c90f36843327a3b136b3ec128ed020.mtga"
```

Then run:

```bash
python3 mtga_extract_plays.py "$LOG" "$CARDDB" --last 1 --no-resolves
```

Save the last two games to a file:

```bash
python3 mtga_extract_plays.py "$LOG" "$CARDDB" --last 2 --no-resolves > mtga_transcript.txt
```

Show only one game by number:

```bash
python3 mtga_extract_plays.py "$LOG" "$CARDDB" --select 3 --no-resolves
```

## Debugging Choices

Arena records most gameplay as IDs and structured game state changes. Card names should come from the SQLite card database, not from the raw log.

To inspect a card by name:

```bash
python3 mtga_extract_plays.py "$LOG" "$CARDDB" --last 1 --debug-card "Serra's Emissary"
```

To inspect a card by `grpId`:

```bash
python3 mtga_extract_plays.py "$LOG" "$CARDDB" --last 1 --debug-grpid 75982
```

To look for events that may contain choices or selections:

```bash
python3 mtga_extract_plays.py "$LOG" "$CARDDB" --last 1 --debug-choices
```

This is meant to help find where Arena records choices like creature type, protection type, modal choices, or similar decisions.

## Notes

The Arena card database filename changes when Arena updates. If the command fails because the database path does not exist, look in:

```text
~/Library/Application Support/com.wizards.mtga/Downloads/Raw/
```

and use the current `Raw_CardDatabase_*.mtga` file.

## License

This project is released under the MIT License.

It is provided as-is, with no warranty. If you choose to use it, you are responsible for what you do with it. I am not liable for any damages or problems that come from using this software.
