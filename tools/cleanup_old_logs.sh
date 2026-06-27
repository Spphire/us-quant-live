#!/bin/bash
# Delete audit logs older than 1 year (both raw and compressed)
# Usage: ./cleanup_old_logs.sh [--dry-run] [--days N]
# Run monthly via Task Scheduler or cron

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
LOG_ROOT="$PROJECT_ROOT/artifacts/daily_alpaca_scheduler/output"
CUTOFF_DAYS=365
DRY_RUN=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --days)
            CUTOFF_DAYS="$2"
            shift 2
            ;;
        *)
            echo "Usage: $0 [--dry-run] [--days N]"
            echo "  --dry-run: Show what would be deleted without actually deleting"
            echo "  --days N: Delete logs older than N days (default: 365)"
            exit 1
            ;;
    esac
done

if $DRY_RUN; then
    echo "[DRY RUN MODE] No files will be deleted"
    echo ""
fi

echo "=== Audit Log Cleanup Tool ==="
echo "Log root: $LOG_ROOT"
echo "Cutoff: $CUTOFF_DAYS days"
echo "Date: $(date)"
echo ""

if [[ ! -d "$LOG_ROOT" ]]; then
    echo "[ERROR] Log root does not exist: $LOG_ROOT"
    exit 1
fi

DELETED_COUNT=0
FREED_BYTES=0

# Find files and directories older than CUTOFF_DAYS
while IFS= read -r path; do
    # Get size
    if [[ -f "$path" ]]; then
        SIZE=$(stat -c%s "$path")
    else
        SIZE=$(du -sb "$path" | cut -f1)
    fi

    if $DRY_RUN; then
        echo "[DRY RUN] Would delete: $(basename "$path") ($(numfmt --to=iec-i --suffix=B $SIZE))"
    else
        echo "[DELETE] $(basename "$path") ($(numfmt --to=iec-i --suffix=B $SIZE))..."
        rm -rf "$path"

        if [[ $? -eq 0 ]]; then
            DELETED_COUNT=$((DELETED_COUNT + 1))
            FREED_BYTES=$((FREED_BYTES + SIZE))
        else
            echo "  → [ERROR] Failed to delete"
        fi
    fi
done < <(find "$LOG_ROOT" -maxdepth 1 \( -type d -o -name "*.tar.gz" \) -mtime +$CUTOFF_DAYS ! -name "$(basename "$LOG_ROOT")")

echo ""
echo "=== Summary ==="
echo "Items deleted: $DELETED_COUNT"

if [[ $FREED_BYTES -gt 0 ]]; then
    echo "Space freed: $(numfmt --to=iec-i --suffix=B $FREED_BYTES)"
fi

if $DRY_RUN; then
    echo ""
    echo "This was a dry run. Run without --dry-run to actually delete files."
fi

echo ""
echo "Current disk usage:"
du -sh "$LOG_ROOT"

exit 0
