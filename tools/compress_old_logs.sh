#!/bin/bash
# Compress audit logs older than 30 days to save disk space
# Usage: ./compress_old_logs.sh [--dry-run]
# Run weekly via Task Scheduler or cron

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
LOG_ROOT="$PROJECT_ROOT/artifacts/daily_alpaca_scheduler/output"
CUTOFF_DAYS=30
DRY_RUN=false

# Parse arguments
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
    echo "[DRY RUN MODE] No files will be modified"
    echo ""
fi

echo "=== Audit Log Compression Tool ==="
echo "Log root: $LOG_ROOT"
echo "Cutoff: $CUTOFF_DAYS days"
echo "Date: $(date)"
echo ""

if [[ ! -d "$LOG_ROOT" ]]; then
    echo "[ERROR] Log root does not exist: $LOG_ROOT"
    exit 1
fi

COMPRESSED_COUNT=0
SAVED_BYTES=0

# Find directories older than CUTOFF_DAYS
while IFS= read -r dir; do
    # Skip if already compressed
    if [[ -f "${dir}.tar.gz" ]]; then
        echo "[SKIP] Already compressed: $(basename "$dir")"
        continue
    fi

    # Get directory size
    DIR_SIZE=$(du -sb "$dir" | cut -f1)

    if $DRY_RUN; then
        echo "[DRY RUN] Would compress: $(basename "$dir") ($(numfmt --to=iec-i --suffix=B $DIR_SIZE))"
    else
        echo "[COMPRESS] $(basename "$dir") ($(numfmt --to=iec-i --suffix=B $DIR_SIZE))..."

        # Compress with maximum compression
        tar -czf "${dir}.tar.gz" -C "$(dirname "$dir")" "$(basename "$dir")" 2>/dev/null

        if [[ $? -eq 0 && -f "${dir}.tar.gz" ]]; then
            ARCHIVE_SIZE=$(stat -c%s "${dir}.tar.gz")
            SAVED=$((DIR_SIZE - ARCHIVE_SIZE))
            RATIO=$((100 - ARCHIVE_SIZE * 100 / DIR_SIZE))

            echo "  → ${dir}.tar.gz"
            echo "  → Compressed: $(numfmt --to=iec-i --suffix=B $ARCHIVE_SIZE) (${RATIO}% reduction)"

            # Remove original directory only after successful compression
            rm -rf "$dir"

            COMPRESSED_COUNT=$((COMPRESSED_COUNT + 1))
            SAVED_BYTES=$((SAVED_BYTES + SAVED))
        else
            echo "  → [ERROR] Compression failed, keeping original"
        fi
    fi
done < <(find "$LOG_ROOT" -maxdepth 1 -type d -mtime +$CUTOFF_DAYS ! -name "$(basename "$LOG_ROOT")")

echo ""
echo "=== Summary ==="
echo "Directories compressed: $COMPRESSED_COUNT"

if [[ $SAVED_BYTES -gt 0 ]]; then
    echo "Space saved: $(numfmt --to=iec-i --suffix=B $SAVED_BYTES)"
fi

if $DRY_RUN; then
    echo ""
    echo "This was a dry run. Run without --dry-run to actually compress files."
fi

echo ""
echo "Current disk usage:"
du -sh "$LOG_ROOT"

exit 0
