#!/usr/bin/env bash
# publish.sh
# ──────────────────────────────────────────────────────────────────────────────
# Build the Lambda packages, upload them to the public artifacts S3 bucket,
# upload the packaged CloudFormation template at a stable URL, and print the
# quick-create link to paste into the blog post.
#
# Prerequisites:
#   - The public artifacts bucket must exist. Deploy it once from:
#       cloudvisor-sandbox/terraform/live/sandbox/public-artifacts/
#   - AWS credentials with write access to that bucket
#   - SAM CLI installed
#
# Usage:
#   ./publish.sh
#
# Override defaults via environment variables:
#   BUCKET=my-bucket REGION=eu-west-1 ./publish.sh
#
# IMPORTANT: Region lock
#   Lambda requires its code zip to live in the SAME region as the function.
#   The quick-create link this script prints hard-codes the region below so
#   that readers deploy the stack into the same region where we host the zips.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BUCKET="${BUCKET:-cloudvisor-jano-sandbox-public-artifacts}"
REGION="${REGION:-us-east-1}"
PREFIX="${PREFIX:-durable-demo}"
STACK_NAME="${STACK_NAME:-durable-functions-demo}"

cd "$(dirname "$0")"

echo ""
echo "▶ Publishing durable-demo"
echo "    Bucket  : $BUCKET"
echo "    Region  : $REGION"
echo "    Prefix  : $PREFIX"
echo ""

# ── Sanity check: bucket exists and is writable ──────────────────────────────
if ! aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" 2>/dev/null; then
  echo "✗ Bucket '$BUCKET' not found or not accessible in $REGION."
  echo "  Deploy it first from the cloudvisor-sandbox Terraform stack:"
  echo "    cd ../../cloudvisor-sandbox/terraform/live/sandbox/public-artifacts"
  echo "    terragrunt apply"
  exit 1
fi

# ── Build ────────────────────────────────────────────────────────────────────
echo "▶ sam build"
sam build

# ── Package: uploads Lambda zips to S3 and rewrites CodeUri references ───────
echo ""
echo "▶ sam package"
sam package \
  --s3-bucket "$BUCKET" \
  --s3-prefix "$PREFIX/artifacts" \
  --output-template-file .aws-sam/template.packaged.yaml \
  --region "$REGION"

# ── Upload the packaged template to a stable URL ─────────────────────────────
TEMPLATE_KEY="$PREFIX/template.yaml"
echo ""
echo "▶ Uploading packaged template to s3://$BUCKET/$TEMPLATE_KEY"
aws s3 cp .aws-sam/template.packaged.yaml \
  "s3://$BUCKET/$TEMPLATE_KEY" \
  --region "$REGION" \
  --content-type "application/x-yaml" \
  --cache-control "no-cache, max-age=60" \
  --only-show-errors

TEMPLATE_URL="https://$BUCKET.s3.$REGION.amazonaws.com/$TEMPLATE_KEY"

# URL-encode the template URL for safe embedding in a query string
ENCODED_URL=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$TEMPLATE_URL")

QUICK_CREATE_URL="https://console.aws.amazon.com/cloudformation/home?region=$REGION#/stacks/quickcreate?templateURL=$ENCODED_URL&stackName=$STACK_NAME"

echo ""
echo "────────────────────────────────────────────────────────────────────────"
echo "✓ Published"
echo "────────────────────────────────────────────────────────────────────────"
echo ""
echo "Template URL:"
echo "  $TEMPLATE_URL"
echo ""
echo "Quick-create URL (paste this into the blog post as the Launch Stack button):"
echo ""
echo "  $QUICK_CREATE_URL"
echo ""
echo "Markdown badge:"
echo ""
echo "  [![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)]($QUICK_CREATE_URL)"
echo ""
echo "────────────────────────────────────────────────────────────────────────"
