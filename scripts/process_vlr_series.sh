#!/bin/bash

################################################################################
# VLR Series Processing Pipeline
#
# Automates the full pipeline for processing Valorant matches from VLR.gg:
# 1. Scrapes VLR.gg for match metadata
# 2. Splits metadata into individual map files
# 3. For each map:
#    - Downloads YouTube VOD
#    - Runs orchestration to extract events
#    - Saves output in organized folder structure
#    - Removes VOD to save space
#
# Usage:
#   ./process_vlr_series.sh <vlr_url> [output_base_dir] [--keep-map-video] [--parallel-processes [N]] [--extract-round-clips]
#
# Example:
#   ./process_vlr_series.sh "https://www.vlr.gg/542272/..." ./output --keep-map-video
#   ./process_vlr_series.sh "https://www.vlr.gg/542272/..." ./output --parallel-processes
#   ./process_vlr_series.sh "https://www.vlr.gg/542272/..." ./output --extract-round-clips
################################################################################

set -e  # Exit on error
set -u  # Exit on undefined variable

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Script directory (for accessing config files)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Print colored message
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_step() {
    echo -e "${CYAN}[STEP]${NC} $1"
}

extract_round_clips_for_map() {
    local map_num="$1"
    local process_video="$2"
    local output_dir="$3"
    local map_dir="$4"

    local event_log_file="$output_dir/event_log.jsonl"
    local round_video_dir="$map_dir/video/rounds"
    local round_log_dir="$output_dir/rounds"

    if [ ! -f "$process_video" ]; then
        log_error "[Map $map_num] Cannot extract round clips; video not found: $process_video"
        return 1
    fi

    if [ ! -f "$event_log_file" ]; then
        log_error "[Map $map_num] Cannot extract round clips; event log not found: $event_log_file"
        return 1
    fi

    log_info "[Map $map_num] Extracting round clips..."
    python3 "$SCRIPT_DIR/extract_round_clips.py" \
        --video "$process_video" \
        --events "$event_log_file" \
        --video-output "$round_video_dir" \
        --log-output "$round_log_dir" \
        --pre-padding "$ROUND_PRE_PADDING" \
        --post-padding "$ROUND_POST_PADDING"
}

# Check arguments
if [ $# -lt 1 ]; then
    log_error "Usage: $0 <vlr_url> [output_base_dir] [--keep-map-video] [--parallel-processes [N]] [--extract-round-clips]"
    log_info "Example: $0 'https://www.vlr.gg/542272/...' ./output"
    log_info "Example: $0 'https://www.vlr.gg/542272/...' ./output --keep-map-video"
    log_info "Example: $0 'https://www.vlr.gg/542272/...' ./output --parallel-processes"
    log_info "Example: $0 'https://www.vlr.gg/542272/...' ./output --extract-round-clips"
    exit 1
fi

VLR_URL="$1"
shift

OUTPUT_BASE_DIR="./series_output"
KEEP_MAP_VIDEO=false
PARALLEL_PROCESSES=""
EXTRACT_ROUND_CLIPS=false
ROUND_PRE_PADDING=0
ROUND_POST_PADDING=0

while [ $# -gt 0 ]; do
    case "$1" in
        --keep-map-video)
            KEEP_MAP_VIDEO=true
            ;;
        --parallel-processes)
            PARALLEL_PROCESSES="auto"
            if [ $# -gt 1 ] && [[ "$2" =~ ^[0-9]+$ ]]; then
                PARALLEL_PROCESSES="$2"
                shift
            fi
            ;;
        --extract-round-clips)
            EXTRACT_ROUND_CLIPS=true
            ;;
        --round-pre-padding)
            if [ $# -lt 2 ]; then
                log_error "--round-pre-padding requires a value"
                exit 1
            fi
            ROUND_PRE_PADDING="$2"
            shift
            ;;
        --round-post-padding)
            if [ $# -lt 2 ]; then
                log_error "--round-post-padding requires a value"
                exit 1
            fi
            ROUND_POST_PADDING="$2"
            shift
            ;;
        -*)
            log_error "Unknown option: $1"
            log_error "Usage: $0 <vlr_url> [output_base_dir] [--keep-map-video] [--parallel-processes [N]] [--extract-round-clips]"
            exit 1
            ;;
        *)
            OUTPUT_BASE_DIR="$1"
            ;;
    esac
    shift
done

if [ -n "$PARALLEL_PROCESSES" ]; then
    if [ "$PARALLEL_PROCESSES" != "auto" ] && [ "$PARALLEL_PROCESSES" -lt 1 ]; then
        log_error "--parallel-processes must be greater than 0"
        exit 1
    fi
    KEEP_MAP_VIDEO=true
fi

if [ "$EXTRACT_ROUND_CLIPS" = true ]; then
    KEEP_MAP_VIDEO=true
fi

# Extract match ID from URL for unique directory naming
# Format: https://www.vlr.gg/542265/team1-vs-team2-event
MATCH_ID=$(echo "$VLR_URL" | grep -oE '/[0-9]+/' | head -1 | tr -d '/')

if [ -z "$MATCH_ID" ]; then
    log_error "Could not extract match ID from URL: $VLR_URL"
    log_error "Expected format: https://www.vlr.gg/MATCH_ID/..."
    exit 1
fi

log_info "Starting VLR series processing pipeline"
log_info "VLR URL: $VLR_URL"
log_info "Match ID: $MATCH_ID"
log_info "Output directory: $OUTPUT_BASE_DIR"
if [ "$KEEP_MAP_VIDEO" = true ]; then
    log_info "Keep map videos: Yes"
else
    log_info "Keep map videos: No"
fi
if [ -n "$PARALLEL_PROCESSES" ]; then
    log_info "Parallel processing: Yes (${PARALLEL_PROCESSES})"
else
    log_info "Parallel processing: No"
fi
if [ "$EXTRACT_ROUND_CLIPS" = true ]; then
    log_info "Extract round clips: Yes (pre=${ROUND_PRE_PADDING}s, post=${ROUND_POST_PADDING}s)"
else
    log_info "Extract round clips: No"
fi
echo ""

################################################################################
# Step 1: Scrape VLR metadata
################################################################################

log_step "Step 1: Scraping VLR.gg metadata"

# Create temp directory for intermediate files (unique per match for parallel processing)
TEMP_DIR="$OUTPUT_BASE_DIR/.temp_${MATCH_ID}"
mkdir -p "$TEMP_DIR"

SERIES_METADATA="$TEMP_DIR/series_metadata.json"

log_info "Scraping match metadata..."
valoscribe scrape-vlr "$VLR_URL" -o "$SERIES_METADATA"

if [ ! -f "$SERIES_METADATA" ]; then
    log_error "Failed to scrape VLR metadata"
    exit 1
fi

log_success "Metadata scraped successfully"
echo ""

################################################################################
# Step 2: Extract series information and create organized folder structure
################################################################################

log_step "Step 2: Extracting series information"

# Extract team names and create series folder
TEAM1=$(jq -r '.teams[0]' "$SERIES_METADATA")
TEAM2=$(jq -r '.teams[1]' "$SERIES_METADATA")
NUM_MAPS=$(jq '.maps | length' "$SERIES_METADATA")

# Create series folder name (include match ID for uniqueness)
# Format: <match_id>_<team1>_vs_<team2>
# Example: 542265_paper_rex_vs_g2_esports
SERIES_NAME="${MATCH_ID}_${TEAM1}_vs_${TEAM2}"
SERIES_NAME=$(echo "$SERIES_NAME" | tr '[:upper:]' '[:lower:]' | tr ' ' '_')

SERIES_DIR="$OUTPUT_BASE_DIR/$SERIES_NAME"
mkdir -p "$SERIES_DIR"

log_info "Series: $TEAM1 vs $TEAM2 (Match ID: $MATCH_ID)"
log_info "Maps: $NUM_MAPS"
log_info "Series directory: $SERIES_DIR"
echo ""

# Copy series metadata to series folder
cp "$SERIES_METADATA" "$SERIES_DIR/series_metadata.json"

################################################################################
# Step 3: Split metadata into individual map files
################################################################################

log_step "Step 3: Splitting metadata into individual map files"

MAPS_METADATA_DIR="$SERIES_DIR/metadata"
mkdir -p "$MAPS_METADATA_DIR"

log_info "Splitting metadata..."
valoscribe split-metadata "$SERIES_METADATA" -o "$MAPS_METADATA_DIR" -p "map"

log_success "Metadata split into $NUM_MAPS map files"
echo ""

################################################################################
# Step 4: Process each map
################################################################################

log_step "Step 4: Processing individual maps"

PROCESS_MAP_NUMS=()
PROCESS_MAP_NAMES=()
PROCESS_MAP_METADATA=()
PROCESS_MAP_DIRS=()
PROCESS_OUTPUT_DIRS=()
PROCESS_VIDEO_FILES=()
PROCESS_LOG_FILES=()

# Process each map
for ((i=1; i<=NUM_MAPS; i++)); do
    echo ""
    echo "========================================================================"
    log_step "Processing Map $i/$NUM_MAPS"
    echo "========================================================================"

    MAP_METADATA="$MAPS_METADATA_DIR/map${i}.json"

    if [ ! -f "$MAP_METADATA" ]; then
        log_warning "Map $i metadata not found, skipping..."
        continue
    fi

    # Extract map information
    MAP_NAME=$(jq -r '.map' "$MAP_METADATA")
    VOD_URL=$(jq -r '.vod_url' "$MAP_METADATA")

    log_info "Map: $MAP_NAME"
    log_info "VOD URL: $VOD_URL"

    # Check if VOD URL exists
    if [ "$VOD_URL" = "null" ] || [ -z "$VOD_URL" ]; then
        log_warning "No VOD URL found for map $i, skipping..."
        continue
    fi

    # Create map folder (e.g., "map1_haven")
    MAP_FOLDER_NAME="map${i}_$(echo "$MAP_NAME" | tr '[:upper:]' '[:lower:]')"
    MAP_DIR="$SERIES_DIR/$MAP_FOLDER_NAME"
    mkdir -p "$MAP_DIR"

    log_info "Map directory: $MAP_DIR"

    # Define paths
    VOD_DOWNLOAD_DIR="$TEMP_DIR/map${i}/videos"
    mkdir -p "$VOD_DOWNLOAD_DIR"

    OUTPUT_DIR="$MAP_DIR/output"
    mkdir -p "$OUTPUT_DIR"

    MAP_VIDEO_DIR="$MAP_DIR/video"
    MAP_VIDEO_FILE="$MAP_VIDEO_DIR/map${i}_full.mp4"

    # Create log file for this map
    LOG_FILE="$OUTPUT_DIR/processing.log"

    #---------------------------------------------------------------------------
    # Check if output already exists
    #---------------------------------------------------------------------------

    FRAME_STATES_FILE="$OUTPUT_DIR/frame_states.csv"
    EVENT_LOG_FILE="$OUTPUT_DIR/event_log.jsonl"
    RUN_ORCHESTRATION=true

    if [ -f "$FRAME_STATES_FILE" ] && [ -f "$EVENT_LOG_FILE" ]; then
        if [ "$KEEP_MAP_VIDEO" = true ] && [ ! -f "$MAP_VIDEO_FILE" ]; then
            log_warning "[Map $i] Output already exists, but map video is missing"
            log_info "  - $FRAME_STATES_FILE"
            log_info "  - $EVENT_LOG_FILE"
            log_info "  - Will download and save video only: $MAP_VIDEO_FILE"
            RUN_ORCHESTRATION=false
        elif [ "$EXTRACT_ROUND_CLIPS" = true ]; then
            log_success "[Map $i] Output already exists, will extract round clips"
            log_info "  - $FRAME_STATES_FILE"
            log_info "  - $EVENT_LOG_FILE"
            log_info "  - $MAP_VIDEO_FILE"
            RUN_ORCHESTRATION=false
        else
            log_success "[Map $i] Output already exists, skipping processing"
            log_info "  - $FRAME_STATES_FILE"
            log_info "  - $EVENT_LOG_FILE"
            log_info "  - $LOG_FILE (existing log)"
            if [ "$KEEP_MAP_VIDEO" = true ]; then
                log_info "  - $MAP_VIDEO_FILE"
            fi
            continue
        fi
    fi

    # Start logging for this map
    # All output below will be captured to log file AND displayed
    exec 3>&1 4>&2  # Save original stdout/stderr
    exec > >(tee -a "$LOG_FILE") 2>&1  # Redirect to both file and console

    echo "========================================================================="
    echo "Map $i/$NUM_MAPS Processing Log"
    echo "========================================================================="
    echo "Map: $MAP_NAME"
    echo "VOD URL: $VOD_URL"
    echo "Started: $(date)"
    echo "========================================================================="
    echo ""

    #---------------------------------------------------------------------------
    # Step 4a: Download YouTube VOD
    #---------------------------------------------------------------------------

    PROCESS_VIDEO=""
    VOD_FILE=""

    if [ "$KEEP_MAP_VIDEO" = true ] && [ -f "$MAP_VIDEO_FILE" ]; then
        PROCESS_VIDEO="$MAP_VIDEO_FILE"
        log_success "[Map $i] Existing map video found: $MAP_VIDEO_FILE"
    else
        log_info "[Map $i] Downloading YouTube VOD..."

        # Check for optional start_time and duration in metadata (for livestream clips)
        START_TIME=$(jq -r '.start_time // empty' "$MAP_METADATA" 2>/dev/null || echo "")
        DURATION=$(jq -r '.duration // empty' "$MAP_METADATA" 2>/dev/null || echo "")

        # Build download command with optional timestamp parameters
        DOWNLOAD_CMD="valoscribe download \"$VOD_URL\" -o \"$VOD_DOWNLOAD_DIR\" --height 1080 --fps 60"
        if [ -n "$START_TIME" ]; then
            DOWNLOAD_CMD="$DOWNLOAD_CMD --start $START_TIME"
            log_info "  Start time: ${START_TIME}s (from metadata)"
        fi
        if [ -n "$DURATION" ]; then
            DOWNLOAD_CMD="$DOWNLOAD_CMD --duration $DURATION"
            log_info "  Duration: ${DURATION}s (from metadata)"
        fi

        # Execute download
        set +e
        eval $DOWNLOAD_CMD
        DOWNLOAD_STATUS=$?
        set -e

        if [ "$DOWNLOAD_STATUS" -ne 0 ]; then
            log_error "[Map $i] Failed to download VOD"
            echo ""
            echo "========================================================================="
            echo "Map $i Processing Failed"
            echo "Finished: $(date)"
            echo "========================================================================="
            exec 1>&3 2>&4 3>&- 4>&-
            continue
        fi

        # Find the downloaded video file (most recent .mp4 in download dir)
        # Use ls -t which works on both macOS and Linux
        VOD_FILE=$(ls -t "$VOD_DOWNLOAD_DIR"/*.mp4 2>/dev/null | head -1)

        if [ -z "$VOD_FILE" ] || [ ! -f "$VOD_FILE" ]; then
            log_error "[Map $i] Failed to download VOD"
            echo ""
            echo "========================================================================="
            echo "Map $i Processing Failed"
            echo "Finished: $(date)"
            echo "========================================================================="
            exec 1>&3 2>&4 3>&- 4>&-
            continue
        fi

        log_success "[Map $i] VOD downloaded: $(basename "$VOD_FILE")"

        # Preserve the full map video in the map folder when requested.
        # Use mv so we do not duplicate several GB of video data.
        PROCESS_VIDEO="$VOD_FILE"
        if [ "$KEEP_MAP_VIDEO" = true ]; then
            mkdir -p "$MAP_VIDEO_DIR"
            if [ -f "$MAP_VIDEO_FILE" ]; then
                log_warning "[Map $i] Existing map video will be overwritten: $MAP_VIDEO_FILE"
                rm -f "$MAP_VIDEO_FILE"
            fi
            mv "$VOD_FILE" "$MAP_VIDEO_FILE"
            PROCESS_VIDEO="$MAP_VIDEO_FILE"
            log_success "[Map $i] Map video saved: $MAP_VIDEO_FILE"
        fi
    fi

    #---------------------------------------------------------------------------
    # Step 4b: Run orchestration
    #---------------------------------------------------------------------------

    PROCESS_SUCCEEDED=false

    if [ -n "$PARALLEL_PROCESSES" ]; then
        if [ "$RUN_ORCHESTRATION" = true ]; then
            PROCESS_MAP_NUMS+=("$i")
            PROCESS_MAP_NAMES+=("$MAP_NAME")
            PROCESS_MAP_METADATA+=("$MAP_METADATA")
            PROCESS_MAP_DIRS+=("$MAP_DIR")
            PROCESS_OUTPUT_DIRS+=("$OUTPUT_DIR")
            PROCESS_VIDEO_FILES+=("$PROCESS_VIDEO")
            PROCESS_LOG_FILES+=("$LOG_FILE")
            log_success "[Map $i] Queued for parallel processing"
        else
            log_info "[Map $i] Skipping orchestration because output already exists"
            PROCESS_SUCCEEDED=true
        fi
    elif [ "$RUN_ORCHESTRATION" = true ]; then
        log_info "[Map $i] Running orchestration (this may take a while)..."

        # Run orchestration with quiet mode (only show events)
        set +e
        valoscribe orchestrate process-vod "$PROCESS_VIDEO" "$MAP_METADATA" \
            --output "$OUTPUT_DIR" \
            --fps 4 \
            --quiet \
            --mute-agent-detector
            # --show
        ORCHESTRATION_STATUS=$?
        set -e

        if [ "$ORCHESTRATION_STATUS" -eq 0 ]; then
            log_success "[Map $i] Orchestration completed successfully"
            log_info "[Map $i] Output files:"
            log_info "  - $OUTPUT_DIR/frame_states.csv"
            log_info "  - $OUTPUT_DIR/event_log.jsonl"
            PROCESS_SUCCEEDED=true
        else
            log_error "[Map $i] Orchestration failed"
        fi
    else
        log_info "[Map $i] Skipping orchestration because output already exists"
        PROCESS_SUCCEEDED=true
    fi

    #---------------------------------------------------------------------------
    # Step 4c: Copy metadata to map folder
    #---------------------------------------------------------------------------

    cp "$MAP_METADATA" "$MAP_DIR/metadata.json"

    if [ "$EXTRACT_ROUND_CLIPS" = true ] && [ "$PROCESS_SUCCEEDED" = true ]; then
        if extract_round_clips_for_map "$i" "$PROCESS_VIDEO" "$OUTPUT_DIR" "$MAP_DIR"; then
            log_success "[Map $i] Round clips extracted"
        else
            log_error "[Map $i] Round clip extraction failed"
        fi
    fi

    #---------------------------------------------------------------------------
    # Step 4d: Remove VOD to save space
    #---------------------------------------------------------------------------

    if [ "$KEEP_MAP_VIDEO" = true ]; then
        log_info "[Map $i] Keeping map video: $MAP_VIDEO_FILE"
    else
        log_info "[Map $i] Removing VOD to save space..."
        rm -f "$VOD_FILE"
        log_success "[Map $i] VOD removed"
    fi

    # Close log file and restore stdout/stderr
    echo ""
    echo "========================================================================="
    echo "Map $i Processing Complete"
    echo "Finished: $(date)"
    echo "========================================================================="

    exec 1>&3 2>&4 3>&- 4>&-  # Restore original stdout/stderr

    # Final summary for console (not in log file)
    log_success "[Map $i] Processing complete, log saved to: $LOG_FILE"

    echo ""
done

if [ -n "$PARALLEL_PROCESSES" ]; then
    echo ""
    echo "========================================================================"
    log_step "Step 4b: Processing downloaded maps in parallel"
    echo "========================================================================"

    QUEUED_MAPS=${#PROCESS_MAP_NUMS[@]}

    if [ "$QUEUED_MAPS" -eq 0 ]; then
        log_info "No maps queued for processing"
    else
        if [ "$PARALLEL_PROCESSES" = "auto" ]; then
            MAX_PARALLEL="$QUEUED_MAPS"
        else
            MAX_PARALLEL="$PARALLEL_PROCESSES"
        fi

        if [ "$MAX_PARALLEL" -lt 1 ]; then
            log_error "--parallel-processes must be greater than 0"
            exit 1
        fi

        log_info "Queued maps: $QUEUED_MAPS"
        log_info "Max parallel processes: $MAX_PARALLEL"

        PIDS=()

        for ((idx=0; idx<QUEUED_MAPS; idx++)); do
            while [ "$(jobs -pr | wc -l | tr -d ' ')" -ge "$MAX_PARALLEL" ]; do
                sleep 2
            done

            MAP_NUM="${PROCESS_MAP_NUMS[$idx]}"
            MAP_NAME="${PROCESS_MAP_NAMES[$idx]}"
            MAP_METADATA="${PROCESS_MAP_METADATA[$idx]}"
            MAP_DIR="${PROCESS_MAP_DIRS[$idx]}"
            OUTPUT_DIR="${PROCESS_OUTPUT_DIRS[$idx]}"
            PROCESS_VIDEO="${PROCESS_VIDEO_FILES[$idx]}"
            LOG_FILE="${PROCESS_LOG_FILES[$idx]}"

            (
                {
                    echo ""
                    echo "========================================================================="
                    echo "Map $MAP_NUM Parallel Orchestration Log"
                    echo "========================================================================="
                    echo "Map: $MAP_NAME"
                    echo "Video: $PROCESS_VIDEO"
                    echo "Started: $(date)"
                    echo "========================================================================="
                    echo ""

                    log_info "[Map $MAP_NUM] Running orchestration (parallel worker)..."

                    set +e
                    valoscribe orchestrate process-vod "$PROCESS_VIDEO" "$MAP_METADATA" \
                        --output "$OUTPUT_DIR" \
                        --fps 4 \
                        --quiet \
                        --mute-agent-detector
                    STATUS=$?
                    set -e

                    if [ "$STATUS" -eq 0 ]; then
                        log_success "[Map $MAP_NUM] Orchestration completed successfully"
                        log_info "[Map $MAP_NUM] Output files:"
                        log_info "  - $OUTPUT_DIR/frame_states.csv"
                        log_info "  - $OUTPUT_DIR/event_log.jsonl"

                        cp "$MAP_METADATA" "$MAP_DIR/metadata.json"
                        log_success "[Map $MAP_NUM] Metadata copied"

                        if [ "$EXTRACT_ROUND_CLIPS" = true ]; then
                            if extract_round_clips_for_map "$MAP_NUM" "$PROCESS_VIDEO" "$OUTPUT_DIR" "$MAP_DIR"; then
                                log_success "[Map $MAP_NUM] Round clips extracted"
                            else
                                log_error "[Map $MAP_NUM] Round clip extraction failed"
                                STATUS=1
                            fi
                        fi
                    else
                        log_error "[Map $MAP_NUM] Orchestration failed"
                    fi

                    echo ""
                    echo "========================================================================="
                    echo "Map $MAP_NUM Parallel Processing Complete"
                    echo "Finished: $(date)"
                    echo "========================================================================="

                    exit "$STATUS"
                } >> "$LOG_FILE" 2>&1
            ) &

            WORKER_PID="$!"
            PIDS+=("$WORKER_PID")
            log_info "[Map $MAP_NUM] Started parallel worker (pid $WORKER_PID), log: $LOG_FILE"
        done

        FAILURES=0
        for PID in "${PIDS[@]}"; do
            if ! wait "$PID"; then
                FAILURES=$((FAILURES + 1))
            fi
        done

        if [ "$FAILURES" -gt 0 ]; then
            log_error "$FAILURES parallel map processing job(s) failed"
            exit 1
        fi

        log_success "Parallel map processing completed successfully"
    fi
fi

################################################################################
# Step 5: Cleanup and summary
################################################################################

log_step "Step 5: Cleanup and summary"

# Remove temp directory
log_info "Cleaning up temporary files..."
rm -rf "$TEMP_DIR"

echo ""
echo "========================================================================"
log_success "Pipeline completed successfully!"
echo "========================================================================"
echo ""
log_info "Series: $TEAM1 vs $TEAM2"
log_info "Maps processed: $NUM_MAPS"
log_info "Output directory: $SERIES_DIR"
echo ""
log_info "Folder structure:"
tree -L 2 "$SERIES_DIR" 2>/dev/null || find "$SERIES_DIR" -maxdepth 2 -type d | sed 's|[^/]*/| |g'
echo ""
log_info "Next steps:"
log_info "  - Review event logs: $SERIES_DIR/map*/output/event_log.jsonl"
log_info "  - Analyze frame states: $SERIES_DIR/map*/output/frame_states.csv"
log_info "  - Series metadata: $SERIES_DIR/series_metadata.json"
echo ""
