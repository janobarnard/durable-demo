#!/usr/bin/env bash
# scripts/check-status.sh
# ──────────────────────────────────────────────────────────────────────────────
# Poll the order status. Run this after start-order.sh to watch the
# workflow progress through: starting → awaiting_payment → fulfilled.
#
# Usage:
#   ./scripts/check-status.sh <API_URL> <ORDER_ID>
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

API_URL="${1:-}"
ORDER_ID="${2:-}"

if [[ -z "$API_URL" || -z "$ORDER_ID" ]]; then
  echo "Usage: $0 <API_URL> <ORDER_ID>"
  exit 1
fi

API_URL="${API_URL%/}"

echo ""
echo "Checking status for order $ORDER_ID ..."
echo ""

RESPONSE=$(curl -s "$API_URL/orders/$ORDER_ID")
echo "$RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$RESPONSE"
echo ""

STATUS=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null || echo "unknown")

case "$STATUS" in
  awaiting_payment)
    echo "─────────────────────────────────────────────────────────────"
    echo "The workflow is HIBERNATING — no compute charges right now."
    echo "Approve or reject payment to resume:"
    echo ""
    echo "  ./scripts/approve-order.sh \"$API_URL\" \"$ORDER_ID\""
    echo "─────────────────────────────────────────────────────────────"
    ;;
  fulfilled)
    echo "Order fulfilled!"
    ;;
  rejected|payment_declined|payment_failed)
    echo "Order ended with status: $STATUS"
    ;;
  starting)
    echo "Orchestrator is still running the initial steps. Try again in a moment."
    ;;
  *)
    echo "Status: $STATUS"
    ;;
esac
