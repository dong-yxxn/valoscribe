#!/usr/bin/env python3
"""Extract highlight clips from per-round videos and event logs."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create highlight clips from Valoscribe round logs."
    )
    parser.add_argument("--round-video-dir", required=True, type=Path)
    parser.add_argument("--round-log-dir", required=True, type=Path)
    parser.add_argument("--video-output", required=True, type=Path)
    parser.add_argument("--manifest-output", required=True, type=Path)
    parser.add_argument("--top-n", type=int, default=12)
    parser.add_argument("--pre-padding", type=float, default=1.0)
    parser.add_argument("--post-padding", type=float, default=0.0)
    parser.add_argument(
        "--round-end-padding",
        type=float,
        default=3.0,
        help="Extra seconds added only to highlights that run through round_end.",
    )
    parser.add_argument("--min-duration", type=float, default=8.0)
    parser.add_argument("--max-duration", type=float, default=35.0)
    parser.add_argument("--kill-window", type=float, default=6.0)
    parser.add_argument(
        "--merge-gap",
        type=float,
        default=6.0,
        help="Merge highlight candidates in the same round when their clip gap is this many seconds or less.",
    )
    parser.add_argument(
        "--max-merged-duration",
        type=float,
        default=45.0,
        help="Maximum duration allowed for a merged highlight candidate.",
    )
    parser.add_argument(
        "--reel-output",
        type=Path,
        default=None,
        help="Combined highlight reel output path. Defaults to <video-output>/highlight_mapN.mp4 when map number is detected.",
    )
    parser.add_argument(
        "--no-reel",
        action="store_true",
        help="Do not create a combined highlight reel.",
    )
    parser.add_argument(
        "--clean-output",
        action="store_true",
        help="Remove existing generated highlight mp4 files from video-output before extraction.",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"[WARN] Skipping malformed JSONL line {path}:{line_number}: {exc}", file=sys.stderr)
    return rows


def parse_round_number(path: Path) -> int | None:
    match = re.search(r"round(\d+)", path.stem, re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def event_time(event: dict[str, Any]) -> float | None:
    timestamp = event.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        return None
    return float(timestamp)


def collect_round_bounds(events: list[dict[str, Any]]) -> tuple[float, float]:
    start = 0.0
    end = 0.0

    for event in events:
        timestamp = event_time(event)
        if timestamp is None:
            continue
        if event.get("type") == "round_start":
            start = timestamp
        elif event.get("type") == "round_end":
            end = max(end, timestamp)

    if end <= start:
        timestamps = [event_time(event) for event in events]
        valid = [timestamp for timestamp in timestamps if timestamp is not None]
        if valid:
            end = max(valid)

    return start, end


def round_winner(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("type") == "round_end" and isinstance(event.get("winner"), str):
            return event["winner"]
    return None


def kill_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kills = [event for event in events if event.get("type") == "kill" and event_time(event) is not None]
    return sorted(kills, key=lambda event: float(event["timestamp"]))


def add_candidate(
    candidates: list[dict[str, Any]],
    *,
    round_number: int,
    kind: str,
    reason: str,
    score: float,
    start: float,
    end: float,
    events: list[dict[str, Any]],
    winner: str | None,
) -> None:
    if end <= start:
        return

    candidates.append(
        {
            "round_number": round_number,
            "kind": kind,
            "reason": reason,
            "score": round(score, 3),
            "start": round(start, 3),
            "end": round(end, 3),
            "winner": winner,
            "includes_round_end": False,
            "events": summarize_events(events),
        }
    )


def summarize_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for event in events:
        item = {
            "type": event.get("type"),
            "timestamp": event.get("timestamp"),
        }
        for key in (
            "killer_name",
            "victim_name",
            "killer_team",
            "victim_team",
            "player",
            "team",
            "winner",
            "ability",
            "weapon",
        ):
            if key in event:
                item[key] = event[key]
        summary.append(item)
    return summary


def build_team_rosters(events: list[dict[str, Any]]) -> dict[str, set[str]]:
    rosters: dict[str, set[str]] = defaultdict(set)
    for event in events:
        for player_key, team_key in (
            ("player", "team"),
            ("killer_name", "killer_team"),
            ("victim_name", "victim_team"),
        ):
            player = event.get(player_key)
            team = event.get(team_key)
            if isinstance(player, str) and isinstance(team, str):
                rosters[team].add(player)
    return rosters


def clutch_candidates(
    *,
    round_number: int,
    events: list[dict[str, Any]],
    kills: list[dict[str, Any]],
    winner: str | None,
    round_end: float,
) -> list[dict[str, Any]]:
    if not winner:
        return []

    rosters = build_team_rosters(events)
    if winner not in rosters or len(rosters) < 2:
        return []

    alive = {team: set(players) for team, players in rosters.items()}
    candidates: list[dict[str, Any]] = []
    clutch_start: float | None = None
    clutch_state: tuple[int, int] | None = None

    for kill in kills:
        timestamp = event_time(kill)
        if timestamp is None:
            continue

        victim = kill.get("victim_name")
        victim_team = kill.get("victim_team")
        if isinstance(victim, str) and isinstance(victim_team, str):
            alive.setdefault(victim_team, set()).discard(victim)

        winner_alive = len(alive.get(winner, set()))
        opponent_alive = sum(len(players) for team, players in alive.items() if team != winner)
        if winner_alive <= 2 and opponent_alive >= winner_alive + 1:
            if clutch_start is None:
                clutch_start = timestamp
                clutch_state = (winner_alive, opponent_alive)

    if clutch_start is not None and clutch_state is not None:
        nearby_events = [
            event
            for event in events
            if (timestamp := event_time(event)) is not None and clutch_start <= timestamp <= round_end
        ]
        winner_alive, opponent_alive = clutch_state
        score = 10 + max(0, opponent_alive - winner_alive) * 3
        add_candidate(
            candidates,
            round_number=round_number,
            kind="clutch",
            reason=f"{winner_alive}v{opponent_alive} comeback by {winner}",
            score=score,
            start=clutch_start,
            end=round_end,
            events=nearby_events,
            winner=winner,
        )
        candidates[-1]["includes_round_end"] = True

    return candidates


def build_candidates(round_number: int, events: list[dict[str, Any]], kill_window: float) -> list[dict[str, Any]]:
    kills = kill_events(events)
    if not kills:
        return []

    winner = round_winner(events)
    _, round_end = collect_round_bounds(events)
    candidates: list[dict[str, Any]] = []

    for i, first in enumerate(kills):
        window = [
            kill
            for kill in kills[i:]
            if float(kill["timestamp"]) - float(first["timestamp"]) <= kill_window
        ]
        if len(window) >= 3:
            start = float(window[0]["timestamp"])
            end = float(window[-1]["timestamp"])
            players = Counter(kill.get("killer_name") for kill in window if isinstance(kill.get("killer_name"), str))
            top_player, top_count = players.most_common(1)[0] if players else ("unknown", 0)
            add_candidate(
                candidates,
                round_number=round_number,
                kind="kill_burst",
                reason=f"{len(window)} kills in {kill_window:g}s; top fragger {top_player} ({top_count})",
                score=6 + len(window) * 2 + top_count,
                start=start,
                end=end,
                events=window,
                winner=winner,
            )

    kills_by_player: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for kill in kills:
        killer = kill.get("killer_name")
        if isinstance(killer, str):
            kills_by_player[killer].append(kill)

    for killer, player_kills in kills_by_player.items():
        if len(player_kills) >= 4:
            add_candidate(
                candidates,
                round_number=round_number,
                kind="high_kill_round",
                reason=f"{killer} {len(player_kills)}K round",
                score=12 + len(player_kills) * 3,
                start=float(player_kills[0]["timestamp"]),
                end=round_end,
                events=player_kills,
                winner=winner,
            )

        for i, first in enumerate(player_kills):
            window = [
                kill
                for kill in player_kills[i:]
                if float(kill["timestamp"]) - float(first["timestamp"]) <= max(kill_window, 8.0)
            ]
            if len(window) >= 2:
                add_candidate(
                    candidates,
                    round_number=round_number,
                    kind="multi_kill",
                    reason=f"{killer} {len(window)}K sequence",
                    score=4 + len(window) * 4,
                    start=float(window[0]["timestamp"]),
                    end=float(window[-1]["timestamp"]),
                    events=window,
                    winner=winner,
                )

    last_kill = kills[-1]
    last_kill_ts = float(last_kill["timestamp"])
    if round_end and round_end - last_kill_ts <= 8.0:
        add_candidate(
            candidates,
            round_number=round_number,
            kind="round_decider",
            reason="last kill before round_end",
            score=8,
            start=last_kill_ts,
            end=round_end,
            events=[last_kill] + [event for event in events if event.get("type") == "round_end"],
            winner=winner,
        )
        candidates[-1]["includes_round_end"] = True

    candidates.extend(
        clutch_candidates(
            round_number=round_number,
            events=events,
            kills=kills,
            winner=winner,
            round_end=round_end,
        )
    )

    return candidates


def padded_bounds(
    candidate: dict[str, Any],
    pre_padding: float,
    post_padding: float,
    round_end_padding: float,
    min_duration: float,
    max_duration: float,
) -> tuple[float, float]:
    clip_start = max(0.0, float(candidate["start"]) - pre_padding)
    extra_round_end_padding = round_end_padding if candidate.get("includes_round_end") else 0.0
    clip_end = float(candidate["end"]) + post_padding + extra_round_end_padding
    duration = clip_end - clip_start

    if duration < min_duration:
        extra = min_duration - duration
        clip_start = max(0.0, clip_start - extra / 2)
        clip_end += extra / 2

    duration = clip_end - clip_start
    if duration > max_duration:
        center = (float(candidate["start"]) + float(candidate["end"])) / 2
        clip_start = max(0.0, center - max_duration / 2)
        clip_end = clip_start + max_duration

    return round(clip_start, 3), round(clip_end, 3)


def unique_join(values: list[str], separator: str) -> str:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return separator.join(result)


def merge_two_candidates(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    merged_kinds = sorted(
        set(left.get("merged_kinds", [left["kind"]])) | set(right.get("merged_kinds", [right["kind"]]))
    )
    merged["kind"] = "merged"
    merged["reason"] = f"merged nearby highlights: {', '.join(merged_kinds)}"
    merged["score"] = round(max(float(left["score"]), float(right["score"])) + min(float(left["score"]), float(right["score"])) * 0.35 + 2, 3)
    merged["start"] = min(float(left["start"]), float(right["start"]))
    merged["end"] = max(float(left["end"]), float(right["end"]))
    merged["clip_start"] = min(float(left["clip_start"]), float(right["clip_start"]))
    merged["clip_end"] = max(float(left["clip_end"]), float(right["clip_end"]))
    merged["includes_round_end"] = bool(left.get("includes_round_end") or right.get("includes_round_end"))
    merged["events"] = sorted(
        list(left.get("events", [])) + list(right.get("events", [])),
        key=lambda event: float(event.get("timestamp", 0.0) or 0.0),
    )
    merged["merged_count"] = int(left.get("merged_count", 1)) + int(right.get("merged_count", 1))
    merged["merged_kinds"] = merged_kinds
    return merged


def merge_nearby_candidates(
    candidates: list[dict[str, Any]],
    *,
    merge_gap: float,
    max_merged_duration: float,
) -> list[dict[str, Any]]:
    if merge_gap < 0:
        return candidates

    by_round: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_round[int(candidate["round_number"])].append(candidate)

    merged_candidates: list[dict[str, Any]] = []
    for round_number in sorted(by_round):
        round_candidates = sorted(
            by_round[round_number],
            key=lambda item: (float(item["clip_start"]), float(item["clip_end"]), -float(item["score"])),
        )
        current: dict[str, Any] | None = None
        for candidate in round_candidates:
            if current is None:
                current = dict(candidate)
                current.setdefault("merged_count", 1)
                current.setdefault("merged_kinds", [candidate["kind"]])
                continue

            gap = float(candidate["clip_start"]) - float(current["clip_end"])
            merged_duration = max(float(current["clip_end"]), float(candidate["clip_end"])) - min(
                float(current["clip_start"]),
                float(candidate["clip_start"]),
            )
            if gap <= merge_gap and merged_duration <= max_merged_duration:
                current = merge_two_candidates(current, candidate)
            else:
                merged_candidates.append(current)
                current = dict(candidate)
                current.setdefault("merged_count", 1)
                current.setdefault("merged_kinds", [candidate["kind"]])

        if current is not None:
            merged_candidates.append(current)

    return merged_candidates


def overlaps(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a["round_number"] != b["round_number"]:
        return False
    start = max(float(a["clip_start"]), float(b["clip_start"]))
    end = min(float(a["clip_end"]), float(b["clip_end"]))
    intersection = max(0.0, end - start)
    if intersection <= 0:
        return False
    shortest = min(float(a["clip_end"]) - float(a["clip_start"]), float(b["clip_end"]) - float(b["clip_start"]))
    return intersection / max(shortest, 0.001) >= 0.65


def select_candidates(candidates: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: (item["score"], item["round_number"]), reverse=True):
        if any(overlaps(candidate, existing) for existing in selected):
            continue
        selected.append(candidate)
        if len(selected) >= top_n:
            break
    return selected


def safe_reason(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9가-힣]+", "_", value).strip("_").lower()
    return value[:40] or "highlight"


def default_reel_output(video_output: Path) -> Path:
    for parent in [video_output, *video_output.parents]:
        match = re.match(r"map(\d+)(?:_|$)", parent.name, re.IGNORECASE)
        if match:
            return video_output / f"highlight_map{int(match.group(1))}.mp4"
    return video_output / "highlight_reel.mp4"


def run_ffmpeg(video: Path, clip_start: float, clip_end: float, output_path: Path, overwrite: bool) -> None:
    duration = max(0.0, clip_end - clip_start)
    command = [
        "ffmpeg",
        "-y" if overwrite else "-n",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{clip_start:.3f}",
        "-i",
        str(video),
        "-t",
        f"{duration:.3f}",
        "-c",
        "copy",
        str(output_path),
    ]
    subprocess.run(command, check=True)


def run_ffmpeg_concat(video_paths: list[Path], output_path: Path, overwrite: bool) -> None:
    concat_list = output_path.with_suffix(output_path.suffix + ".concat.txt")
    with concat_list.open("w", encoding="utf-8") as handle:
        for video_path in video_paths:
            escaped = str(video_path.resolve()).replace("'", "'\\''")
            handle.write(f"file '{escaped}'\n")

    command = [
        "ffmpeg",
        "-y" if overwrite else "-n",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-c",
        "copy",
        str(output_path),
    ]
    try:
        subprocess.run(command, check=True)
    finally:
        concat_list.unlink(missing_ok=True)


def clean_output_dir(video_output: Path, reel_output: Path) -> int:
    removed = 0
    for path in video_output.glob("highlight_*.mp4"):
        if path.is_file():
            path.unlink()
            removed += 1
    if reel_output.exists() and reel_output.is_file():
        reel_output.unlink()
        removed += 1
    return removed


def main() -> int:
    args = parse_args()

    if not args.round_video_dir.exists():
        print(f"[ERROR] Round video dir not found: {args.round_video_dir}", file=sys.stderr)
        return 1
    if not args.round_log_dir.exists():
        print(f"[ERROR] Round log dir not found: {args.round_log_dir}", file=sys.stderr)
        return 1
    if args.top_n < 1:
        print("[ERROR] --top-n must be 1 or greater", file=sys.stderr)
        return 1

    candidates: list[dict[str, Any]] = []
    for log_path in sorted(args.round_log_dir.glob("round*.log")):
        round_number = parse_round_number(log_path)
        if round_number is None:
            continue
        video_path = args.round_video_dir / f"round{round_number:02d}.mp4"
        if not video_path.exists():
            print(f"[WARN] Missing round video for {log_path.name}: {video_path}", file=sys.stderr)
            continue

        events = read_jsonl(log_path)
        round_candidates = build_candidates(round_number, events, args.kill_window)
        for candidate in round_candidates:
            clip_start, clip_end = padded_bounds(
                candidate,
                args.pre_padding,
                args.post_padding,
                args.round_end_padding,
                args.min_duration,
                args.max_duration,
            )
            candidate["clip_start"] = clip_start
            candidate["clip_end"] = clip_end
            candidate["round_video"] = str(video_path)
        candidates.extend(round_candidates)

    if not candidates:
        print("[ERROR] No highlight candidates found", file=sys.stderr)
        return 1

    merged_candidates = merge_nearby_candidates(
        candidates,
        merge_gap=args.merge_gap,
        max_merged_duration=args.max_merged_duration,
    )
    selected = select_candidates(merged_candidates, args.top_n)
    for score_rank, candidate in enumerate(selected, 1):
        candidate["score_rank"] = score_rank
    selected = sorted(selected, key=lambda item: (item["round_number"], item["clip_start"], item["clip_end"]))
    args.video_output.mkdir(parents=True, exist_ok=True)
    args.manifest_output.parent.mkdir(parents=True, exist_ok=True)
    reel_output = args.reel_output or default_reel_output(args.video_output)
    if not args.no_reel:
        reel_output.parent.mkdir(parents=True, exist_ok=True)
    if args.clean_output:
        removed = clean_output_dir(args.video_output, reel_output)
        print(f"[INFO] Removed {removed} existing highlight video(s)")

    manifest: list[dict[str, Any]] = []
    extracted_videos: list[Path] = []
    print(
        f"[INFO] Extracting {len(selected)} highlight clip(s) from "
        f"{len(candidates)} candidate(s), merged to {len(merged_candidates)} candidate(s)"
    )

    for index, candidate in enumerate(selected, 1):
        output_name = (
            f"highlight_{index:03d}_r{candidate['round_number']:02d}_"
            f"{candidate['kind']}_{safe_reason(candidate['reason'])}.mp4"
        )
        output_path = args.video_output / output_name
        run_ffmpeg(
            Path(candidate["round_video"]),
            float(candidate["clip_start"]),
            float(candidate["clip_end"]),
            output_path,
            args.overwrite,
        )

        manifest_item = dict(candidate)
        manifest_item["rank"] = index
        manifest_item["output_video"] = str(output_path)
        manifest.append(manifest_item)
        extracted_videos.append(output_path)
        print(
            f"[INFO] #{index:03d} R{candidate['round_number']:02d} "
            f"{candidate['kind']} score={candidate['score']} "
            f"{candidate['clip_start']:.3f}-{candidate['clip_end']:.3f}s -> {output_path.name}"
        )

    if not args.no_reel and extracted_videos:
        reel_videos = [Path(item["output_video"]) for item in manifest]
        run_ffmpeg_concat(reel_videos, reel_output, args.overwrite)
        for item in manifest:
            item["reel_video"] = str(reel_output)
        print(f"[INFO] Highlight reel -> {reel_output}")

    with args.manifest_output.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)

    print(f"[SUCCESS] Highlight extraction complete: {args.manifest_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
