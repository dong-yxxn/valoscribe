#!/usr/bin/env python3
"""Transcribe round clips with faster-whisper and metadata glossary prompts."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


VALORANT_TERMS = [
    "Valorant",
    "spike",
    "plant",
    "defuse",
    "retake",
    "clutch",
    "eco",
    "bonus",
    "force buy",
    "full buy",
    "save",
    "ultimate",
    "orb",
    "site",
    "main",
    "heaven",
    "hell",
    "short",
    "long",
    "flank",
    "post plant",
    "operator",
    "vandal",
    "phantom",
]


_WORKER_MODEL: Any | None = None
_WORKER_METADATA: dict[str, Any] | None = None
_WORKER_CONFIG: dict[str, Any] | None = None

HANGUL_BASE = 0xAC00
HANGUL_END = 0xD7A3
HANGUL_INITIALS = [
    "g",
    "kk",
    "n",
    "d",
    "tt",
    "r",
    "m",
    "b",
    "pp",
    "s",
    "ss",
    "",
    "j",
    "jj",
    "ch",
    "k",
    "t",
    "p",
    "h",
]
HANGUL_VOWELS = [
    "a",
    "ae",
    "ya",
    "yae",
    "eo",
    "e",
    "yeo",
    "ye",
    "o",
    "wa",
    "wae",
    "oe",
    "yo",
    "u",
    "wo",
    "we",
    "wi",
    "yu",
    "eu",
    "ui",
    "i",
]
HANGUL_FINALS = [
    "",
    "k",
    "k",
    "ks",
    "n",
    "nj",
    "nh",
    "t",
    "l",
    "lk",
    "lm",
    "lb",
    "ls",
    "lt",
    "lp",
    "lh",
    "m",
    "p",
    "ps",
    "t",
    "t",
    "ng",
    "t",
    "t",
    "k",
    "t",
    "p",
    "h",
]
KOREAN_PARTICLES = [
    "으로는",
    "으로도",
    "으로",
    "처럼",
    "까지",
    "부터",
    "에게",
    "한테",
    "에서",
    "에는",
    "라도",
    "이나",
    "나",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "과",
    "와",
    "도",
    "만",
    "의",
    "에",
    "로",
]
ENTITY_LINK_THRESHOLD = 0.84
ENTITY_CANDIDATE_THRESHOLD = 0.74


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transcribe Valoscribe round clips with faster-whisper."
    )
    parser.add_argument("--round-video-dir", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--round-log-dir", type=Path, default=None)
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--language", default="ko")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--compute-type", default="default")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument(
        "--round",
        dest="round_filters",
        action="append",
        default=[],
        help="Transcribe only a specific round, e.g. --round round01 or --round 1. Can be repeated.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Transcribe at most N round clips.")
    parser.add_argument(
        "--parallel-workers",
        type=int,
        default=1,
        help="Number of worker processes. Each worker loads its own Whisper model.",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--word-timestamps", dest="word_timestamps", action="store_true", default=True)
    parser.add_argument("--no-word-timestamps", dest="word_timestamps", action="store_false")
    parser.add_argument("--vad-filter", dest="vad_filter", action="store_true", default=True)
    parser.add_argument("--no-vad-filter", dest="vad_filter", action="store_false")
    parser.add_argument(
        "--condition-on-previous-text",
        action="store_true",
        help="Let Whisper condition each segment on previous text. Disabled by default to reduce drift.",
    )
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []

    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"[WARN] Skipping malformed line {path}:{line_number}: {exc}", file=sys.stderr)
    return rows


def unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = value.strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def build_metadata_glossary(metadata: dict[str, Any]) -> dict[str, list[str] | str]:
    teams = [team.get("name", "") for team in metadata.get("teams", []) if isinstance(team, dict)]
    players = metadata.get("players", [])
    player_names = [player.get("name", "") for player in players if isinstance(player, dict)]
    agents = [player.get("agent", "") for player in players if isinstance(player, dict)]

    return {
        "map_name": str(metadata.get("map", "")),
        "teams": unique_preserve_order(teams),
        "players": unique_preserve_order(player_names),
        "agents": unique_preserve_order(agents),
    }


def build_round_glossary(round_events: list[dict[str, Any]]) -> dict[str, list[str]]:
    names: list[str] = []
    abilities: list[str] = []
    event_types: list[str] = []

    for event in round_events:
        event_type = event.get("type")
        if isinstance(event_type, str):
            event_types.append(event_type)

        for key in ("player", "killer_name", "victim_name"):
            value = event.get(key)
            if isinstance(value, str):
                names.append(value)

        ability = event.get("ability") or event.get("ultimate")
        if isinstance(ability, str):
            abilities.append(ability)

    common_types = [name for name, _ in Counter(event_types).most_common(6)]

    return {
        "players": unique_preserve_order(names),
        "abilities": unique_preserve_order(abilities),
        "event_types": common_types,
    }


def build_prompt(metadata: dict[str, Any], round_events: list[dict[str, Any]]) -> str:
    glossary = build_metadata_glossary(metadata)
    round_glossary = build_round_glossary(round_events)

    players = unique_preserve_order(
        list(glossary["players"]) + round_glossary["players"]  # type: ignore[arg-type]
    )
    agents = glossary["agents"]  # type: ignore[assignment]
    teams = glossary["teams"]  # type: ignore[assignment]
    abilities = round_glossary["abilities"]

    prompt_parts = [
        "이 음성은 Valorant e스포츠 한국어 해설입니다.",
        "선수명은 반드시 공식 영문 표기로 출력하세요.",
        f"맵: {glossary['map_name']}",
        f"팀: {', '.join(teams)}",
        f"선수: {', '.join(players)}",
        f"에이전트: {', '.join(agents)}",
        f"스킬과 궁극기: {', '.join(abilities[:24])}",
        f"용어: {', '.join(VALORANT_TERMS)}",
        "팀명, 선수명, 에이전트명은 가능한 위 표기를 유지합니다.",
    ]

    return "\n".join(part for part in prompt_parts if not part.endswith(": "))


def parse_round_number(path: Path) -> int | None:
    match = re.search(r"round(\d+)", path.stem, re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def normalize_round_filter(value: str) -> str:
    match = re.search(r"(\d+)", value)
    if not match:
        return value.lower()
    return f"round{int(match.group(1)):02d}"


def filter_round_videos(round_videos: list[Path], filters: list[str], limit: int | None) -> list[Path]:
    selected = round_videos
    if filters:
        wanted = {normalize_round_filter(value) for value in filters}
        selected = [
            video_path
            for video_path in selected
            if normalize_round_filter(video_path.stem) in wanted
        ]

    if limit is not None:
        selected = selected[:limit]

    return selected


def is_hangul_char(value: str) -> bool:
    return HANGUL_BASE <= ord(value) <= HANGUL_END


def contains_hangul(value: str) -> bool:
    return any(is_hangul_char(char) for char in value)


def romanize_hangul(value: str) -> str:
    pieces: list[str] = []
    for char in value:
        if not is_hangul_char(char):
            pieces.append(char)
            continue

        syllable = ord(char) - HANGUL_BASE
        initial = syllable // 588
        vowel = (syllable % 588) // 28
        final = syllable % 28
        pieces.append(HANGUL_INITIALS[initial] + HANGUL_VOWELS[vowel] + HANGUL_FINALS[final])
    return "".join(pieces)


def split_camel_case(value: str) -> str:
    value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
    return re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", value)


def normalize_phonetic_key(value: str) -> str:
    if contains_hangul(value):
        value = romanize_hangul(value)
    else:
        value = split_camel_case(value)

    value = value.casefold()
    value = re.sub(r"[^a-z0-9]+", "", value)
    replacements = [
        ("ph", "f"),
        ("ck", "k"),
        ("qu", "kw"),
        ("x", "ks"),
        ("z", "s"),
        ("v", "b"),
        ("f", "p"),
        ("y", "i"),
        ("l", "r"),
        ("eo", "o"),
        ("ae", "e"),
        ("eu", ""),
    ]
    for source, target in replacements:
        value = value.replace(source, target)
    return value


def roman_to_int(value: str) -> int | None:
    roman_values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    text = value.upper()
    if not text or any(char not in roman_values for char in text):
        return None

    total = 0
    previous = 0
    for char in reversed(text):
        current = roman_values[char]
        if current < previous:
            total -= current
        else:
            total += current
        previous = current
    return total


def korean_number(value: int) -> str | None:
    numbers = {
        0: "영",
        1: "일",
        2: "이",
        3: "삼",
        4: "사",
        5: "오",
        6: "육",
        7: "칠",
        8: "팔",
        9: "구",
        10: "십",
        11: "십일",
        12: "십이",
        13: "십삼",
        14: "십사",
        15: "십오",
        16: "십육",
        17: "십칠",
        18: "십팔",
        19: "십구",
        20: "이십",
    }
    return numbers.get(value)


def entity_forms(name: str) -> list[str]:
    forms = [name, split_camel_case(name)]
    compact = re.sub(r"[^A-Za-z0-9]+", "", name)
    if compact and compact != name:
        forms.append(compact)

    if re.fullmatch(r"[IVXLCDM]+", name.upper()):
        roman_value = roman_to_int(name)
        if roman_value is not None:
            forms.append(str(roman_value))
            hangul_number = korean_number(roman_value)
            if hangul_number:
                forms.append(hangul_number)

    return unique_preserve_order(forms)


def build_entity_catalog(metadata: dict[str, Any], round_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entities: list[dict[str, str]] = []

    for team in metadata.get("teams", []):
        if isinstance(team, dict) and isinstance(team.get("name"), str):
            entities.append({"name": team["name"], "type": "team"})

    for player in metadata.get("players", []):
        if not isinstance(player, dict):
            continue
        if isinstance(player.get("name"), str):
            entities.append({"name": player["name"], "type": "player"})
        if isinstance(player.get("agent"), str):
            entities.append({"name": player["agent"], "type": "agent"})

    for event in round_events:
        for key, entity_type in (
            ("player", "player"),
            ("killer_name", "player"),
            ("victim_name", "player"),
            ("killer_agent", "agent"),
            ("victim_agent", "agent"),
            ("agent", "agent"),
            ("ability", "ability"),
            ("ultimate", "ability"),
            ("weapon", "weapon"),
        ):
            value = event.get(key)
            if isinstance(value, str) and value:
                entities.append({"name": value, "type": entity_type})

    catalog: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entity in entities:
        key = (entity["type"], entity["name"].casefold())
        if key in seen:
            continue
        seen.add(key)
        forms = entity_forms(entity["name"])
        catalog.append(
            {
                "name": entity["name"],
                "type": entity["type"],
                "forms": forms,
                "keys": [normalize_phonetic_key(form) for form in forms],
            }
        )
    return catalog


def strip_korean_particle(surface: str) -> str:
    if not contains_hangul(surface):
        return surface
    for particle in KOREAN_PARTICLES:
        if surface.endswith(particle) and len(surface) > len(particle) + 1:
            return surface[: -len(particle)]
    return surface


def extract_entity_surfaces(text: str) -> list[dict[str, Any]]:
    surfaces: list[dict[str, Any]] = []
    seen: set[tuple[int, int, str]] = set()
    pattern = re.compile(r"[가-힣A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*")

    for match in pattern.finditer(text):
        raw = match.group(0)
        stripped = strip_korean_particle(raw)
        candidates = [stripped] if stripped != raw else [raw]

        for surface in unique_preserve_order(candidates):
            if len(surface) < 2:
                continue
            start = match.start()
            end = start + len(surface)
            key = (start, end, surface)
            if key in seen:
                continue
            seen.add(key)
            surfaces.append({"surface": surface, "start": start, "end": end})

    return surfaces


def entity_event_boost(
    entity: dict[str, Any],
    round_events: list[dict[str, Any]],
    segment_start: float,
    segment_end: float,
) -> float:
    entity_name = entity["name"]
    entity_type = entity["type"]
    keys_by_type = {
        "player": ("player", "killer_name", "victim_name"),
        "agent": ("agent", "killer_agent", "victim_agent"),
        "ability": ("ability", "ultimate"),
        "weapon": ("weapon",),
        "team": ("team", "killer_team", "victim_team", "winner"),
    }
    keys = keys_by_type.get(entity_type, ())

    for event in round_events:
        timestamp = event.get("timestamp")
        if not isinstance(timestamp, (int, float)):
            continue
        if timestamp < segment_start - 2.0 or timestamp > segment_end + 2.0:
            continue
        for key in keys:
            value = event.get(key)
            if isinstance(value, str) and value.casefold() == entity_name.casefold():
                return 0.12
    return 0.0


def score_entity_surface(
    surface: str,
    entity: dict[str, Any],
    round_events: list[dict[str, Any]],
    segment_start: float,
    segment_end: float,
) -> float:
    surface_key = normalize_phonetic_key(surface)
    if not surface_key:
        return 0.0

    best = 0.0
    for entity_key in entity["keys"]:
        if not entity_key:
            continue
        score = SequenceMatcher(None, surface_key, entity_key).ratio()
        if len(surface_key) >= 3 and (surface_key in entity_key or entity_key in surface_key):
            score = max(score, 0.86)
        best = max(best, score)

    best += entity_event_boost(entity, round_events, segment_start, segment_end)
    return min(best, 1.0)


def link_transcript_entities(
    text: str,
    metadata: dict[str, Any],
    round_events: list[dict[str, Any]],
    segment_start: float,
    segment_end: float,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    catalog = build_entity_catalog(metadata, round_events)
    surfaces = extract_entity_surfaces(text)
    links: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    for surface in surfaces:
        scored: list[dict[str, Any]] = []
        for entity in catalog:
            score = score_entity_surface(
                surface["surface"],
                entity,
                round_events,
                segment_start,
                segment_end,
            )
            if score >= ENTITY_CANDIDATE_THRESHOLD:
                scored.append(
                    {
                        "surface": surface["surface"],
                        "start": surface["start"],
                        "end": surface["end"],
                        "entity": entity["name"],
                        "entity_type": entity["type"],
                        "confidence": round(score, 3),
                    }
                )

        scored.sort(key=lambda item: item["confidence"], reverse=True)
        if not scored:
            continue

        best = scored[0]
        second_score = scored[1]["confidence"] if len(scored) > 1 else 0.0
        if best["confidence"] >= ENTITY_LINK_THRESHOLD and best["confidence"] - second_score >= 0.08:
            best["method"] = "metadata_phonetic_event_link"
            links.append(best)
        else:
            for candidate in scored[:3]:
                candidate["method"] = "metadata_phonetic_candidate"
                candidates.append(candidate)

    linked_text = text
    occupied: list[tuple[int, int]] = []
    for link in sorted(links, key=lambda item: (item["start"], item["end"] - item["start"]), reverse=True):
        if any(not (link["end"] <= start or link["start"] >= end) for start, end in occupied):
            continue
        linked_text = linked_text[: link["start"]] + link["entity"] + linked_text[link["end"] :]
        occupied.append((link["start"], link["end"]))

    for link in links:
        link.pop("start", None)
        link.pop("end", None)
    for candidate in candidates:
        candidate.pop("start", None)
        candidate.pop("end", None)

    return linked_text, links, candidates


def segment_to_record(
    *,
    segment: Any,
    segment_id: int,
    round_number: int | None,
    video_path: Path,
    model_name: str,
    metadata: dict[str, Any],
    round_events: list[dict[str, Any]],
    language: str | None,
    language_probability: float | None,
    include_words: bool,
) -> dict[str, Any]:
    raw_text = segment.text.strip()
    linked_text, entity_links, entity_candidates = link_transcript_entities(
        raw_text,
        metadata,
        round_events,
        float(segment.start),
        float(segment.end),
    )

    record: dict[str, Any] = {
        "segment_id": segment_id,
        "round_number": round_number,
        "start": round(float(segment.start), 3),
        "end": round(float(segment.end), 3),
        "text": linked_text,
        "text_raw": raw_text,
        "source_video": str(video_path),
        "model": model_name,
        "language": language,
        "language_probability": language_probability,
    }
    if entity_links:
        record["entity_links"] = entity_links
    if entity_candidates:
        record["entity_candidates"] = entity_candidates

    if include_words and getattr(segment, "words", None):
        record["words"] = [
            {
                "start": round(float(word.start), 3),
                "end": round(float(word.end), 3),
                "word": word.word,
            }
            for word in segment.words
        ]

    return record


def build_transcribe_config(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "model": args.model,
        "language": args.language,
        "device": args.device,
        "compute_type": args.compute_type,
        "beam_size": args.beam_size,
        "overwrite": args.overwrite,
        "word_timestamps": args.word_timestamps,
        "vad_filter": args.vad_filter,
        "condition_on_previous_text": args.condition_on_previous_text,
        "output_dir": args.output_dir,
        "round_log_dir": args.round_log_dir,
    }


def transcribe_round_video(
    *,
    video_path: Path,
    metadata: dict[str, Any],
    model: Any,
    config: dict[str, Any],
) -> dict[str, Any]:
    round_number = parse_round_number(video_path)
    output_dir = Path(config["output_dir"])
    output_path = output_dir / f"{video_path.stem}.jsonl"
    if output_path.exists() and not config["overwrite"]:
        return {
            "status": "skipped",
            "video": video_path.name,
            "output": str(output_path),
            "count": 0,
        }

    round_events = []
    round_log_dir = config["round_log_dir"]
    if round_log_dir is not None:
        round_events = read_jsonl(Path(round_log_dir) / f"{video_path.stem}.log")

    prompt = build_prompt(metadata, round_events)
    segments, info = model.transcribe(
        str(video_path),
        language=config["language"],
        task="transcribe",
        beam_size=config["beam_size"],
        vad_filter=config["vad_filter"],
        word_timestamps=config["word_timestamps"],
        initial_prompt=prompt,
        condition_on_previous_text=config["condition_on_previous_text"],
    )

    language = getattr(info, "language", config["language"])
    language_probability = getattr(info, "language_probability", None)

    count = 0
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        for segment_id, segment in enumerate(segments):
            record = segment_to_record(
                segment=segment,
                segment_id=segment_id,
                round_number=round_number,
                video_path=video_path,
                model_name=config["model"],
                metadata=metadata,
                round_events=round_events,
                language=language,
                language_probability=language_probability,
                include_words=config["word_timestamps"],
            )
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1

    temp_path.replace(output_path)
    return {
        "status": "written",
        "video": video_path.name,
        "output": str(output_path),
        "count": count,
    }


def init_transcription_worker(config: dict[str, Any], metadata: dict[str, Any]) -> None:
    global _WORKER_CONFIG, _WORKER_METADATA, _WORKER_MODEL

    from faster_whisper import WhisperModel

    _WORKER_CONFIG = config
    _WORKER_METADATA = metadata
    _WORKER_MODEL = WhisperModel(
        config["model"],
        device=config["device"],
        compute_type=config["compute_type"],
    )


def transcribe_round_video_worker(video_path: str) -> dict[str, Any]:
    if _WORKER_CONFIG is None or _WORKER_METADATA is None or _WORKER_MODEL is None:
        raise RuntimeError("Transcription worker was not initialized")

    return transcribe_round_video(
        video_path=Path(video_path),
        metadata=_WORKER_METADATA,
        model=_WORKER_MODEL,
        config=_WORKER_CONFIG,
    )


def main() -> int:
    args = parse_args()

    if not args.round_video_dir.exists():
        print(f"[ERROR] Round video dir not found: {args.round_video_dir}", file=sys.stderr)
        return 1
    if not args.metadata.exists():
        print(f"[ERROR] Metadata not found: {args.metadata}", file=sys.stderr)
        return 1
    if args.parallel_workers < 1:
        print("[ERROR] --parallel-workers must be 1 or greater", file=sys.stderr)
        return 1

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print(
            "[ERROR] faster-whisper is not installed. Run: uv sync",
            file=sys.stderr,
        )
        return 1

    metadata = load_json(args.metadata)
    round_videos = filter_round_videos(
        sorted(args.round_video_dir.glob("round*.mp4")),
        args.round_filters,
        args.limit,
    )
    if not round_videos:
        print(f"[ERROR] No round*.mp4 files found in {args.round_video_dir}", file=sys.stderr)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    config = build_transcribe_config(args)
    worker_count = min(args.parallel_workers, len(round_videos))

    if worker_count == 1:
        print(
            f"[INFO] Loading faster-whisper model={args.model} "
            f"device={args.device} compute_type={args.compute_type}"
        )
        model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)

        print(f"[INFO] Transcribing {len(round_videos)} round clip(s)")
        for video_path in round_videos:
            print(f"[INFO] Transcribing {video_path.name}")
            result = transcribe_round_video(
                video_path=video_path,
                metadata=metadata,
                model=model,
                config=config,
            )
            if result["status"] == "skipped":
                print(f"[INFO] Skipping existing transcript: {result['output']}")
            else:
                print(f"[SUCCESS] {result['video']}: wrote {result['count']} segment(s)")
    else:
        print(
            f"[INFO] Transcribing {len(round_videos)} round clip(s) with "
            f"{worker_count} worker processes"
        )
        print(
            f"[INFO] Each worker loads model={args.model} "
            f"device={args.device} compute_type={args.compute_type}"
        )
        with ProcessPoolExecutor(
            max_workers=worker_count,
            initializer=init_transcription_worker,
            initargs=(config, metadata),
        ) as executor:
            futures = {
                executor.submit(transcribe_round_video_worker, str(video_path)): video_path
                for video_path in round_videos
            }
            for future in as_completed(futures):
                video_path = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    print(f"[ERROR] {video_path.name}: {exc}", file=sys.stderr)
                    return 1

                if result["status"] == "skipped":
                    print(f"[INFO] Skipping existing transcript: {result['output']}")
                else:
                    print(f"[SUCCESS] {result['video']}: wrote {result['count']} segment(s)")

    print("[SUCCESS] Round transcription complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
