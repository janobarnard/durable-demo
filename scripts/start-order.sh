#!/usr/bin/env bash
# scripts/start-order.sh
# ──────────────────────────────────────────────────────────────────────────────
# Start a new order workflow and print the endpoints to use next.
#
# Usage:
#   ./scripts/start-order.sh <API_URL>
#   ./scripts/start-order.sh              (prompts for the URL)
#
# Example:
#   API_URL=$(aws cloudformation describe-stacks \
#     --stack-name durable-functions-demo \
#     --query 'Stacks[0].Outputs[?OutputKey==`ApiUrl`].OutputValue' \
#     --output text)
#   ./scripts/start-order.sh "$API_URL"
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

API_URL="${1:-}"
if [[ -z "$API_URL" ]]; then
  read -rp "Enter the API URL (from CloudFormation Outputs): " API_URL
fi

API_URL="${API_URL%/}"  # strip trailing slash

ORDER_ID="ORD-$(date +%s | tail -c 6)"

echo ""
echo "Starting order $ORDER_ID ..."
echo ""

RESPONSE=$(curl -s -X POST "$API_URL/orders" \
  -H "Content-Type: application/json" \
  -d "{
    \"orderId\": \"$ORDER_ID\",
    \"customerId\": \"CUST-DEMO\",
    \"items\": [
      {\"sku\": \"WIDGET-001\", \"name\": \"Demo Widget\",  \"qty\": 2},
      {\"sku\": \"GADGET-007\", \"name\": \"Demo Gadget\", \"qty\": 1}
    ]
  }")

echo "$RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$RESPONSE"
echo ""
echo "─────────────────────────────────────────────────────────────"
echo "Next: wait ~2 seconds for the orchestrator to reach the"
echo "payment step, then run:"
echo ""
echo "  ./scripts/check-status.sh  \"$API_URL\" \"$ORDER_ID\""
echo "  ./scripts/approve-order.sh \"$API_URL\" \"$ORDER_ID\""
echo "─────────────────────────────────────────────────────────────"
