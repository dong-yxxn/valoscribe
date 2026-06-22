#!/usr/bin/env python3
"""Extract per-round video clips and adjusted event logs from event_log.jsonl."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create round clips and per-round logs from a Valoscribe event log."
    )
    parser.add_argument("--video", required=True, type=Path, help="Full map video path")
    parser.add_argument("--events", required=True, type=Path, help="event_log.jsonl path")
    parser.add_argument("--video-output", required=True, type=Path, help="Round video output dir")
    parser.add_argument("--log-output", required=True, type=Path, help="Round log output dir")
    parser.add_argument(
        "--pre-padding",
        type=float,
        default=0.0,
        help="Seconds to include before round_start",
    )
    parser.add_argument(
        "--post-padding",
        type=float,
        default=0.0,
        help="Seconds to include after round_end",
    )
    return parser.parse_args()


def read_events(path: Path) -> list[dict]:
    events: list[dict] = []
    skipped = 0

    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                skipped += 1
                print(
                    f"[WARN] Skipping malformed JSONL line {line_number}: {exc}",
                    file=sys.stderr,
                )

    if skipped:
        print(f"[WARN] Skipped {skipped} malformed event line(s)", file=sys.stderr)

    return events


def build_rounds(events: list[dict]) -> list[dict]:
    starts: dict[int, dict] = {}
    rounds: list[dict] = []

    for event in events:
        event_type = event.get("type")
        round_number = event.get("round_number")

        if not isinstance(round_number, int):
            continue

        if event_type == "round_start":
            starts[round_number] = event
        elif event_type == "round_end" and round_number in starts:
            start_event = starts[round_number]
            start_ts = float(start_event["timestamp"])
            end_ts = float(event["timestamp"])
            if end_ts <= start_ts:
                print(
                    f"[WARN] Skipping round {round_number}: end <= start "
                    f"({end_ts:.3f} <= {start_ts:.3f})",
                    file=sys.stderr,
                )
                continue

            rounds.append(
                {
                    "round_number": round_number,
                    "start": start_ts,
                    "end": end_ts,
                    "start_event": start_event,
                    "end_event": event,
                }
            )

    return sorted(rounds, key=lambda item: item["round_number"])


def write_round_log(
    *,
    events: list[dict],
    round_number: int,
    round_start: float,
    round_end: float,
    clip_start: float,
    clip_end: float,
    output_path: Path,
) -> int:
    count = 0
    with output_path.open("w", encoding="utf-8") as handle:
        for event in events:
            timestamp = event.get("timestamp")
            if not isinstance(timestamp, (int, float)):
                continue

            source_timestamp = float(timestamp)
            if source_timestamp < round_start or source_timestamp > round_end:
                continue

            adjusted = dict(event)
            adjusted["source_timestamp"] = source_timestamp
            adjusted["timestamp"] = round(source_timestamp - clip_start, 3)
            adjusted["clip_start"] = clip_start
            adjusted["clip_end"] = clip_end
            adjusted["round_number"] = round_number
            handle.write(json.dumps(adjusted, ensure_ascii=False) + "\n")
            count += 1

    return count


def run_ffmpeg(video: Path, clip_start: float, clip_end: float, output_path: Path) -> None:
    duration = max(0.0, clip_end - clip_start)
    command = [
        "ffmpeg",
        "-y",
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


def main() -> int:
    args = parse_args()

    if not args.video.exists():
        print(f"[ERROR] Video not found: {args.video}", file=sys.stderr)
        return 1
    if not args.events.exists():
        print(f"[ERROR] Event log not found: {args.events}", file=sys.stderr)
        return 1

    events = read_events(args.events)
    rounds = build_rounds(events)

    if not rounds:
        print("[ERROR] No complete round_start/round_end pairs found", file=sys.stderr)
        return 1

    args.video_output.mkdir(parents=True, exist_ok=True)
    args.log_output.mkdir(parents=True, exist_ok=True)

    print(f"[INFO] Extracting {len(rounds)} round clips")

    for round_data in rounds:
        round_number = round_data["round_number"]
        round_start = round_data["start"]
        round_end = round_data["end"]
        clip_start = max(0.0, round_start - args.pre_padding)
        clip_end = round_end + args.post_padding

        round_name = f"round{round_number:02d}"
        video_output = args.video_output / f"{round_name}.mp4"
        log_output = args.log_output / f"{round_name}.log"

        run_ffmpeg(args.video, clip_start, clip_end, video_output)
        event_count = write_round_log(
            events=events,
            round_number=round_number,
            round_start=round_start,
            round_end=round_end,
            clip_start=clip_start,
            clip_end=clip_end,
            output_path=log_output,
        )

        print(
            f"[INFO] {round_name}: {clip_start:.3f}-{clip_end:.3f}s "
            f"-> {video_output} ({event_count} events)"
        )

    print("[SUCCESS] Round clip extraction complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
