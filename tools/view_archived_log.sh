#!/bin/bash
# View a compressed audit log without permanently extracting it
# Usage: ./view_archived_log.sh 20260627_120000 [file_to_view]
# If file_to_view is not specified, lists all files in the archive

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
LOG_ROOT="$PROJECT_ROOT/artifacts/daily_alpaca_scheduler/output"

SESSION_NAME="$1"
FILE_TO_VIEW="${2:-}"

ARCHIVE_PATH="$LOG_ROOT/${SESSION_NAME}.tar.gz"

if [[ ! -f "$ARCHIVE_PATH" ]]; then
    echo "[ERROR] Archive not found: $ARCHIVE_PATH"
    echo ""
    echo "Available archives:"
    ls -1 "$LOG_ROOT"/*.tar.gz 2>/dev/null | xargs -n1 basename | sed 's/.tar.gz$//' || echo "  (none)"
    exit 1
fi

if [[ -z "$FILE_TO_VIEW" ]]; then
    # List contents
    echo "=== Contents of $SESSION_NAME ==="
    tar -tzf "$ARCHIVE_PATH" | sed "s|^${SESSION_NAME}/||" | grep -v "^$"
    echo ""
    echo "To view a specific file:"
    echo "  $0 $SESSION_NAME <filename>"
    exit 0
fi

# View specific file
FULL_PATH="${SESSION_NAME}/${FILE_TO_VIEW}"

if ! tar -tzf "$ARCHIVE_PATH" | grep -q "^${FULL_PATH}$"; then
    echo "[ERROR] File not found in archive: $FILE_TO_VIEW"
    echo ""
    echo "Available files:"
    tar -tzf "$ARCHIVE_PATH" | sed "s|^${SESSION_NAME}/||" | grep -v "^$"
    exit 1
fi

echo "=== Viewing: $SESSION_NAME / $FILE_TO_VIEW ==="
echo ""

# Extract to stdout and view with appropriate pager/viewer
case "$FILE_TO_VIEW" in
    *.csv)
        tar -xzOf "$ARCHIVE_PATH" "$FULL_PATH" | head -100
        echo ""
        echo "(showing first 100 lines; use 'tar -xzOf $ARCHIVE_PATH $FULL_PATH' to see full file)"
        ;;
    *.json)
        tar -xzOf "$ARCHIVE_PATH" "$FULL_PATH" | jq '.' 2>/dev/null || tar -xzOf "$ARCHIVE_PATH" "$FULL_PATH"
        ;;
    *)
        tar -xzOf "$ARCHIVE_PATH" "$FULL_PATH"
        ;;
esac

exit 0
