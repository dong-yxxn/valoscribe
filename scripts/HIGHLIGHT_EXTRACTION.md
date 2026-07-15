# Highlight Extraction

This document describes how `scripts/extract_highlights.py` selects, scores, merges, and exports highlight clips from Valoscribe per-round event logs.

## Inputs

The script works from already extracted round-level artifacts:

- `--round-video-dir`: directory containing `roundXX.mp4`
- `--round-log-dir`: directory containing `roundXX.log`
- `--video-output`: directory for generated highlight clips
- `--manifest-output`: JSON manifest path for selected highlights

Each round log is expected to be JSONL with Valoscribe event records such as:

- `round_start`
- `kill`
- `death`
- `ability_used`
- `ultimate_used`
- `round_end`

Highlight timelines use the per-round timestamps inside each `roundXX.log`, not the original full-map video timestamps.

## Default Parameters

Current defaults:

| Option | Default | Meaning |
| --- | ---: | --- |
| `--top-n` | `12` | Number of final highlights to export |
| `--pre-padding` | `1.0` | Seconds before candidate start |
| `--post-padding` | `0.0` | Seconds after candidate end |
| `--round-end-padding` | `3.0` | Extra seconds only for candidates ending at `round_end` |
| `--min-duration` | `8.0` | Minimum exported clip duration |
| `--max-duration` | `35.0` | Maximum duration for a single candidate before merge |
| `--kill-window` | `6.0` | Window for kill-burst detection |
| `--merge-gap` | `6.0` | Max gap between nearby candidates in the same round to merge |
| `--max-merged-duration` | `45.0` | Maximum duration after merging nearby candidates |

## Candidate Types

The script first builds highlight candidates from each round independently.

### `kill_burst`

Detects dense combat.

Condition:

```text
at least 3 kill events within --kill-window seconds
```

Candidate interval:

```text
start = first kill timestamp in the burst
end   = last kill timestamp in the burst
```

Score:

```text
6 + (number of kills * 2) + top fragger kill count in the burst
```

Example: 5 kills in 6 seconds, with one player getting 2 of them:

```text
score = 6 + (5 * 2) + 2 = 18
```

### `multi_kill`

Detects one player getting multiple kills close together.

Condition:

```text
same killer_name gets at least 2 kills within max(--kill-window, 8.0) seconds
```

Candidate interval:

```text
start = first kill timestamp by that player in the sequence
end   = last kill timestamp by that player in the sequence
```

Score:

```text
4 + (number of kills in the sequence * 4)
```

Example: 2K sequence:

```text
score = 4 + (2 * 4) = 12
```

### `high_kill_round`

Detects a player carrying the round by total kills.

Condition:

```text
same killer_name gets at least 4 kills in one round
```

Candidate interval:

```text
start = player's first kill timestamp
end   = round_end timestamp
```

Score:

```text
12 + (player round kills * 3)
```

Example: 4K round:

```text
score = 12 + (4 * 3) = 24
```

This candidate includes `round_end`, so `--round-end-padding` is applied.

### `round_decider`

Detects the final kill when it happens near the round end.

Condition:

```text
last kill timestamp is within 8 seconds of round_end
```

Candidate interval:

```text
start = last kill timestamp
end   = round_end timestamp
```

Score:

```text
8
```

This candidate includes `round_end`, so `--round-end-padding` is applied.

### `clutch`

Detects a possible comeback from an unfavorable alive-state.

The script estimates alive players from team rosters and `kill`/`death` events. A clutch candidate starts when the eventual winning team becomes disadvantaged:

```text
winner_alive <= 2
opponent_alive >= winner_alive + 1
```

Candidate interval:

```text
start = first timestamp where the winning team is in the clutch state
end   = round_end timestamp
```

Score:

```text
10 + ((opponent_alive - winner_alive) * 3)
```

Example: 2v4 comeback:

```text
score = 10 + ((4 - 2) * 3) = 16
```

This candidate includes `round_end`, so `--round-end-padding` is applied.

## Clip Bounds

For every candidate, the raw candidate interval is converted into an export clip interval:

```text
clip_start = max(0, start - pre_padding)
clip_end   = end + post_padding
```

If the candidate includes `round_end`, extra end padding is added:

```text
clip_end = end + post_padding + round_end_padding
```

Then duration constraints are applied:

1. If the clip is shorter than `--min-duration`, it is expanded around the candidate center.
2. If the clip is longer than `--max-duration`, it is trimmed around the candidate center.

For merged candidates, `--max-merged-duration` controls the maximum merged clip duration.

## Candidate Merging

After candidate generation and padding, the script merges nearby candidates within the same round.

Two candidates are merged when:

```text
gap_between_candidates <= --merge-gap
merged_duration <= --max-merged-duration
```

Merged interval:

```text
merged_start = earliest clip_start
merged_end   = latest clip_end
```

Merged score:

```text
max(score_a, score_b) + min(score_a, score_b) * 0.35 + 2
```

Merged metadata:

- `kind` becomes `merged`
- `merged_kinds` lists the merged candidate types
- `merged_count` records how many candidate groups were merged
- `events` are combined and sorted by timestamp
- `includes_round_end` is true if any merged candidate includes `round_end`

## Final Selection

Final selection happens after merging:

1. Sort merged candidates by score descending.
2. Skip candidates that overlap heavily with an already selected candidate in the same round.
3. Keep the top `--top-n` candidates.

Overlap suppression uses this rule:

```text
intersection_duration / shorter_candidate_duration >= 0.65
```

The selected candidates are then re-sorted for output:

```text
round_number ascending, then clip_start ascending
```

This means:

- `score_rank` is the quality rank from scoring.
- `rank` and filename numbering are timeline order.

## Outputs

For each selected highlight, the script writes one mp4:

```text
highlight_001_r01_...
highlight_002_r03_...
...
```

The numbering is timeline order, not score order.

The script also writes a combined map-level highlight reel by default:

```text
highlight_map3.mp4
```

The map number is detected from the map folder name, such as `map3_lotus`. If no map number is detected, the fallback name is:

```text
highlight_reel.mp4
```

The manifest records:

- `rank`: timeline order
- `score_rank`: score order
- `round_number`
- `kind`
- `reason`
- `score`
- `start` / `end`: raw candidate interval
- `clip_start` / `clip_end`: exported clip interval
- `events`: summarized events used as evidence
- `output_video`
- `reel_video`

## Example Command

```bash
uv run python scripts/extract_highlights.py \
  --round-video-dir series_output/686185_dplus_esports_vs_arete/map3_lotus/video/rounds \
  --round-log-dir series_output/686185_dplus_esports_vs_arete/map3_lotus/output/rounds \
  --video-output series_output/686185_dplus_esports_vs_arete/map3_lotus/video/highlights \
  --manifest-output series_output/686185_dplus_esports_vs_arete/map3_lotus/output/highlights/manifest.json \
  --top-n 12 \
  --clean-output \
  --overwrite
```

## Visualization Plan For Demo

For a demo, the goal is to show not only the final highlight reel, but also why each scene was selected. The most useful visualization is a round timeline built from the same `roundXX.log` and `manifest.json` data used by the extractor.

### 1. Match-Level Highlight Overview

Show a horizontal timeline across the whole map, grouped by round:

```text
R01  [==== highlight ====]
R02
R03                      [=== clutch ===]
R04
R05       [== burst ==]
...
```

Each selected highlight should be drawn as a block with:

- round number
- `kind`
- `score`
- `score_rank`
- clip duration
- winner

Color by highlight type:

| Type | Suggested Color | Meaning |
| --- | --- | --- |
| `kill_burst` | Red | Many kills in a short time |
| `multi_kill` | Orange | Same player multi-kill |
| `high_kill_round` | Purple | 4K+ round |
| `round_decider` | Blue | Final kill near round end |
| `clutch` | Green | Disadvantaged winner comeback |
| `merged` | Gold | Multiple nearby highlight reasons merged |

This overview answers:

```text
Where in the map did the selected highlight scenes happen?
```

### 2. Per-Highlight Evidence Timeline

For each selected highlight, show a compact timeline from `clip_start` to `clip_end`.

Example:

```text
R01 highlight_001
7.25s                 13.25s      16.50s  19.25s          22.00s  25.00s
|-----------------------|-----------|-------|---------------|-------|
clip_start              kill        kill    kill            round_end + padding
                         Kally > JaebiN
                                     ANNYEON > shu
                                             Kally > Rico
```

Recommended marks:

- vertical line for each `kill`
- skull/marker for `death`
- flag/diamond for `round_end`
- subtle shaded background for final exported clip interval
- stronger shaded region for raw candidate interval
- labels for `killer_name > victim_name`

This explains:

```text
The clip was selected because these events happened close together.
```

### 3. Score Breakdown Card

Next to the video or timeline, show a small score card:

```text
Kind: kill_burst
Reason: 5 kills in 6s; top fragger Kally (2)
Score: 18
Formula: 6 + (5 kills * 2) + 2 top-fragger kills
Score rank: #2
Timeline rank: #1
```

For merged highlights:

```text
Kind: merged
Merged kinds: kill_burst, multi_kill, round_decider
Merged count: 9
Score: 69.2
```

This answers:

```text
How was this highlight quantified?
```

### 4. Candidate Filtering Funnel

For engineering demos, show the selection funnel:

```text
Raw event logs
  -> 49 highlight candidates
  -> 18 candidates after nearby merge
  -> top 12 selected by score
  -> reordered by round timeline
  -> individual clips + highlight_map3.mp4
```

This can be a simple stacked number card or vertical flow diagram. It is especially useful because the script logs these values:

```text
Extracting 12 highlight clip(s) from 49 candidate(s), merged to 18 candidate(s)
```

### 5. Manifest-Driven Demo UI

The demo can be generated entirely from `manifest.json`. No extra inference is needed.

Useful fields:

| Manifest Field | Visualization Use |
| --- | --- |
| `rank` | Timeline-order numbering |
| `score_rank` | Quality ranking |
| `round_number` | Round grouping |
| `kind` | Color/type label |
| `reason` | Human-readable explanation |
| `score` | Candidate strength |
| `start`, `end` | Raw evidence interval |
| `clip_start`, `clip_end` | Exported video interval |
| `events` | Event markers on timeline |
| `merged_kinds` | Merged reason chips |
| `merged_count` | How many candidates were merged |
| `output_video` | Clip playback |
| `reel_video` | Full highlight reel playback |

### 6. Recommended Demo Layout

Recommended screen structure:

```text
+------------------------------------------------------------+
| Highlight Reel Player                                      |
| highlight_map3.mp4                                         |
+----------------------------+-------------------------------+
| Selected Highlights List   | Evidence Timeline             |
| #001 R01 merged score 69.2 | kills, deaths, round_end      |
| #002 R03 merged score 41.2 | score formula / reason        |
| #003 R05 merged score 28.1 | selected clip playback        |
+----------------------------+-------------------------------+
| Match-Level Round Timeline                                 |
+------------------------------------------------------------+
```

The key interaction:

1. Click a highlight in the list.
2. Load its `output_video`.
3. Show its event timeline.
4. Show score breakdown and reason.
5. Optionally jump to the same scene in `highlight_map3.mp4`.

### 7. Minimal Static Visualization

If a full UI is too much, generate a static HTML report:

- one `<video>` for `highlight_map3.mp4`
- one table from `manifest.json`
- one SVG or HTML timeline per highlight
- colored event markers from each highlight's `events`

This is enough to demonstrate:

- selected scenes
- event evidence
- score and rank
- merge behavior
- final reel order

## Notes And Limits

- Highlight quality depends on `kill`, `death`, and `round_end` accuracy in event logs.
- The current scoring favors combat-heavy moments, especially multi-kill bursts and comeback endings.
- The script does not yet use transcript excitement signals.
- `round_end` timing may be early in some logs; `--round-end-padding` compensates only for candidates that run through `round_end`.
