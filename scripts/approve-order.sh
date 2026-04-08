#!/usr/bin/env bash
# scripts/approve-order.sh
# ──────────────────────────────────────────────────────────────────────────────
# Approve or reject payment for a waiting order.
# Internally this calls send_durable_execution_callback_success or _failure,
# which wakes the hibernating orchestrator and resumes it from the checkpoint.
#
# Usage:
#   ./scripts/approve-order.sh <API_URL> <ORDER_ID> [approve|reject]
#
# Default action is "approve".
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

API_URL="${1:-}"
ORDER_ID="${2:-}"
ACTION="${3:-approve}"

if [[ -z "$API_URL" || -z "$ORDER_ID" ]]; then
  echo "Usage: $0 <API_URL> <ORDER_ID> [approve|reject]"
  exit 1
fi

API_URL="${API_URL%/}"

if [[ "$ACTION" != "approve" && "$ACTION" != "reject" ]]; then
  echo "Action must be 'approve' or 'reject' (got: $ACTION)"
  exit 1
fi

echo ""
echo "Sending $ACTION callback for order $ORDER_ID ..."
echo ""

RESPONSE=$(curl -s -X POST "$API_URL/orders/$ORDER_ID/$ACTION")
echo "$RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$RESPONSE"
echo ""
echo "─────────────────────────────────────────────────────────────"
echo "The durable function is replaying from the last checkpoint."
echo "Poll the status to see the final result:"
echo ""
echo "  ./scripts/check-status.sh \"$API_URL\" \"$ORDER_ID\""
echo "─────────────────────────────────────────────────────────────"
