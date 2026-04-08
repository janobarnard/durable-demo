"""
Order API + UI Handler — Lambda Durable Functions Demo
======================================================

Standard (non-durable) Lambda that serves five routes:

  GET  /                         Interactive HTML UI (embedded below)
  POST /orders                   Start a new order workflow
  GET  /orders/{orderId}         Poll the current workflow state
  POST /orders/{orderId}/approve Resume the paused workflow (payment approved)
  POST /orders/{orderId}/reject  Resume the paused workflow (payment declined)

The approve/reject routes retrieve the callbackId stored by the
orchestrator, then call send_durable_execution_callback_success /
send_durable_execution_callback_failure to wake the hibernating function.
"""
import json
import os
import time
import uuid
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

dynamodb = boto3.resource('dynamodb')
lambda_client = boto3.client('lambda')
table = dynamodb.Table(os.environ['ORDERS_TABLE'])
ORCHESTRATOR_ARN = os.environ['ORCHESTRATOR_ARN']

# Keep order records for 7 days
ORDER_TTL_SECONDS = 7 * 24 * 60 * 60


def lambda_handler(event, context):
    method = event.get('httpMethod', '')
    path = event.get('path', '')
    path_params = event.get('pathParameters') or {}
    order_id = path_params.get('orderId')

    try:
        # ── UI ───────────────────────────────────────────────────────────
        if method == 'GET' and path in ('/', ''):
            return _ui_response()

        # ── API ──────────────────────────────────────────────────────────
        if method == 'POST' and path == '/orders':
            return _start_order(event)

        if method == 'GET' and order_id:
            return _get_order(order_id)

        if method == 'POST' and order_id and path.endswith('/approve'):
            return _send_callback(order_id, approved=True)

        if method == 'POST' and order_id and path.endswith('/reject'):
            return _send_callback(order_id, approved=False)

        return _resp(404, {'error': 'Route not found', 'path': path, 'method': method})

    except ClientError as exc:
        print(f"AWS error: {exc}")
        return _resp(500, {'error': exc.response['Error']['Message']})
    except Exception as exc:
        print(f"Unexpected error: {exc}")
        return _resp(500, {'error': str(exc)})


# ── Route handlers ────────────────────────────────────────────────────────────

def _start_order(event: dict) -> dict:
    body = json.loads(event.get('body') or '{}')

    order_id = body.get('orderId') or uuid.uuid4().hex[:8].upper()
    customer_id = body.get('customerId', 'CUST-DEMO')
    items = body.get('items', [{'sku': 'WIDGET-001', 'name': 'Demo Widget', 'qty': 1}])

    now_ms = int(time.time() * 1000)

    # Write the initial record so the GET endpoint works immediately.
    table.put_item(Item={
        'orderId': order_id,
        'customerId': customer_id,
        'items': items,
        'status': 'starting',
        'createdAt': Decimal(str(now_ms)),
        'ttl': int(time.time()) + ORDER_TTL_SECONDS,
    })

    # Invoke the durable orchestrator asynchronously.
    #
    # Key parameters:
    #   FunctionName          Must be a qualified ARN (:$LATEST or a version alias).
    #   InvocationType        Event = async (202). Required for workflows longer than
    #                         15 minutes.
    #   DurableExecutionName  Idempotency key: a second invocation with the same name
    #                         returns the existing execution rather than starting a
    #                         duplicate.
    lambda_client.invoke(
        FunctionName=f"{ORCHESTRATOR_ARN}:$LATEST",
        InvocationType='Event',
        DurableExecutionName=order_id,
        Payload=json.dumps({
            'orderId': order_id,
            'customerId': customer_id,
            'items': items,
        }).encode(),
    )

    return _resp(202, {
        'orderId': order_id,
        'status': 'starting',
        'createdAt': now_ms,
        'message': (
            'Workflow started. The orchestrator will run validate-order and '
            'reserve-inventory, then pause waiting for payment approval.'
        ),
    })


def _get_order(order_id: str) -> dict:
    result = table.get_item(Key={'orderId': order_id})
    item = result.get('Item')

    if not item:
        return _resp(404, {'error': f'Order {order_id!r} not found'})

    public = {k: v for k, v in item.items() if k not in ('callbackId', 'ttl')}

    if item.get('status') == 'awaiting_payment' and item.get('callbackId'):
        public['callbackAvailable'] = True

    return _resp(200, _decimal_to_native(public))


def _send_callback(order_id: str, approved: bool) -> dict:
    result = table.get_item(Key={'orderId': order_id})
    item = result.get('Item')

    if not item:
        return _resp(404, {'error': f'Order {order_id!r} not found'})

    callback_id = item.get('callbackId')
    status = item.get('status', 'unknown')

    if not callback_id:
        return _resp(409, {
            'error': f'Order is not awaiting payment (current status: {status!r})',
            'hint': (
                'The orchestrator may still be running the validate/reserve steps. '
                'Wait a moment and try again.'
            ),
        })

    action = 'approved' if approved else 'rejected'

    if approved:
        lambda_client.send_durable_execution_callback_success(
            CallbackId=callback_id,
            Result=json.dumps({'approved': True, 'method': 'api'}),
        )
    else:
        lambda_client.send_durable_execution_callback_failure(
            CallbackId=callback_id,
            Error={
                'ErrorType': 'PaymentDeclined',
                'ErrorMessage': 'Payment rejected via API',
            },
        )

    return _resp(200, {
        'orderId': order_id,
        'action': action,
        'message': (
            f'Payment {action}. The durable function will now replay from the last '
            f'checkpoint and continue to the fulfillment step.'
        ),
    })


def _ui_response() -> dict:
    return {
        'statusCode': 200,
        'headers': {'Content-Type': 'text/html; charset=utf-8'},
        'body': UI_HTML,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resp(status_code: int, body: dict) -> dict:
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
        },
        'body': json.dumps(body, default=str),
    }


def _decimal_to_native(obj):
    """boto3 returns DynamoDB numbers as Decimal, which isn't JSON-native."""
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    if isinstance(obj, dict):
        return {k: _decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_to_native(v) for v in obj]
    return obj


# ─────────────────────────────────────────────────────────────────────────────
# Embedded interactive UI
# ─────────────────────────────────────────────────────────────────────────────
#
# A single self-contained HTML page with a visual workflow timeline and a
# live metrics panel. The key teaching moment: the page renders a ticking
# "Wall time" counter next to a flat "Active compute" counter — the gap
# between them is the hibernation time, billed at $0.00.
#
# No frameworks, no external fetches. Everything lives in this one string.

UI_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Lambda Durable Functions — Live Demo</title>
<style>
  :root {
    --bg: #0b0e14;
    --panel: #141821;
    --panel-2: #1b2030;
    --border: #252b3a;
    --text: #e6e9ef;
    --muted: #8a92a6;
    --accent: #5b8dee;
    --accent-hover: #7aa3f0;
    --success: #4ade80;
    --warning: #fbbf24;
    --danger: #ef4444;
    --hibernate: #a78bfa;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    line-height: 1.55;
    font-size: 15px;
  }
  .container { max-width: 860px; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }
  h1 { font-size: 1.6rem; margin: 0 0 0.25rem; font-weight: 600; letter-spacing: -0.01em; }
  .subtitle { color: var(--muted); margin: 0 0 2rem; font-size: 0.95rem; }

  .panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1.5rem;
    margin-bottom: 1.25rem;
  }

  button {
    background: var(--accent);
    color: #fff;
    border: none;
    padding: 0.65rem 1.3rem;
    border-radius: 6px;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: background 0.15s;
    font-family: inherit;
  }
  button:hover:not(:disabled) { background: var(--accent-hover); }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  button.secondary {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--text);
  }
  button.secondary:hover:not(:disabled) { background: var(--panel-2); }
  button.danger { background: var(--danger); }
  button.danger:hover:not(:disabled) { background: #dc3030; }
  button.success { background: var(--success); color: #000; }
  button.success:hover:not(:disabled) { background: #65e690; }

  .header-row { display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
  .order-info { color: var(--muted); font-size: 0.875rem; }
  .order-info code { color: var(--text); }

  /* Workflow timeline */
  .steps { position: relative; padding: 0.5rem 0 0 0; }
  .step {
    display: flex;
    align-items: flex-start;
    gap: 1rem;
    padding: 1rem 0 1rem 2.5rem;
    position: relative;
    transition: all 0.2s;
  }
  .step::before {
    content: '';
    position: absolute;
    left: 0.95rem;
    top: 2.5rem;
    bottom: -0.5rem;
    width: 2px;
    background: var(--border);
  }
  .step:last-child::before { display: none; }

  .step-icon {
    position: absolute;
    left: 0;
    top: 0.9rem;
    width: 2rem;
    height: 2rem;
    border-radius: 50%;
    background: var(--panel-2);
    border: 2px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--muted);
    z-index: 1;
    transition: all 0.2s;
  }

  .step.done .step-icon {
    background: var(--success);
    border-color: var(--success);
    color: #000;
  }
  .step.done::before { background: var(--success); }
  .step.active .step-icon {
    background: var(--warning);
    border-color: var(--warning);
    color: #000;
    animation: pulse 1.5s ease-in-out infinite;
  }
  .step.hibernating .step-icon {
    background: var(--hibernate);
    border-color: var(--hibernate);
    color: #000;
  }
  .step.hibernating { background: rgba(167, 139, 250, 0.05); border-radius: 8px; margin-left: -0.5rem; padding-left: 3rem; }
  .step.failed .step-icon {
    background: var(--danger);
    border-color: var(--danger);
    color: #fff;
  }
  .step.failed::before { background: var(--danger); }

  @keyframes pulse {
    0%, 100% { transform: scale(1); box-shadow: 0 0 0 0 rgba(251, 191, 36, 0.5); }
    50%     { transform: scale(1.05); box-shadow: 0 0 0 6px rgba(251, 191, 36, 0); }
  }

  .step-body { flex: 1; min-width: 0; }
  .step-title {
    margin: 0.25rem 0 0.25rem;
    font-weight: 500;
    font-size: 0.95rem;
  }
  .step-title code { font-size: 0.9em; color: var(--accent); background: none; padding: 0; }
  .step-detail { color: var(--muted); font-size: 0.85rem; margin: 0; }
  .step.done .step-detail { color: var(--success); }
  .step.hibernating .step-detail { color: var(--hibernate); font-weight: 500; }
  .step.failed .step-detail { color: var(--danger); }

  .actions { display: flex; gap: 0.5rem; margin-top: 0.75rem; }

  /* Metrics panel */
  .metrics {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 0.75rem;
    margin-top: 1.25rem;
    padding-top: 1.25rem;
    border-top: 1px solid var(--border);
  }
  .metric {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.9rem 1rem;
    text-align: center;
  }
  .metric-label {
    color: var(--muted);
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 0.35rem;
  }
  .metric-value {
    font-size: 1.4rem;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    color: var(--text);
  }
  .metric-value.free { color: var(--success); }
  .metric-sub { color: var(--muted); font-size: 0.7rem; margin-top: 0.2rem; }

  /* Explainer */
  .explainer {
    background: rgba(91, 141, 238, 0.08);
    border-left: 3px solid var(--accent);
    padding: 1rem 1.25rem;
    margin-bottom: 1.25rem;
    font-size: 0.9rem;
    color: var(--muted);
    border-radius: 0 8px 8px 0;
  }
  .explainer strong { color: var(--text); }
  .explainer code { color: var(--accent); }

  .hidden { display: none !important; }
  code {
    background: rgba(255,255,255,0.06);
    padding: 0.1rem 0.35rem;
    border-radius: 3px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.85em;
  }
  footer {
    text-align: center;
    margin-top: 2.5rem;
    font-size: 0.8rem;
    color: var(--muted);
  }
  footer a { color: var(--accent); text-decoration: none; }
  footer a:hover { text-decoration: underline; }

  .status-badge {
    display: inline-block;
    padding: 0.2rem 0.6rem;
    border-radius: 999px;
    font-size: 0.75rem;
    font-weight: 500;
    background: var(--panel-2);
    color: var(--muted);
    text-transform: lowercase;
    letter-spacing: 0.02em;
  }
  .status-badge.ok        { background: rgba(74, 222, 128, 0.15); color: var(--success); }
  .status-badge.warn      { background: rgba(251, 191, 36, 0.15); color: var(--warning); }
  .status-badge.hibernate { background: rgba(167, 139, 250, 0.15); color: var(--hibernate); }
  .status-badge.err       { background: rgba(239, 68, 68, 0.15); color: var(--danger); }
</style>
</head>
<body>
<div class="container">
  <h1>Lambda Durable Functions — Live Demo</h1>
  <p class="subtitle">Watch a workflow hibernate for free, then resume from a checkpoint.</p>

  <div class="explainer">
    <strong>What you'll see:</strong> Click <em>Start new order</em> to kick off a
    workflow. It runs two quick steps, then hits <code>wait_for_callback</code> and
    pauses while waiting for payment approval. Notice how <strong>active compute time</strong>
    stays flat while <strong>wall time</strong> keeps climbing — that gap is zero-cost
    hibernation. Click <em>Approve</em> to wake it up and watch it resume from
    exactly where it left off.
  </div>

  <div class="panel">
    <div class="header-row">
      <button id="startBtn">Start new order</button>
      <div id="orderInfo" class="order-info hidden">
        Order <code id="orderId"></code> &middot;
        <span class="status-badge" id="statusBadge">—</span>
      </div>
    </div>
  </div>

  <div id="workflow" class="panel hidden">
    <div class="steps">
      <div class="step" id="step-validate">
        <div class="step-icon">1</div>
        <div class="step-body">
          <p class="step-title"><code>context.step("validate-order")</code></p>
          <p class="step-detail" id="detail-validate">Pending…</p>
        </div>
      </div>

      <div class="step" id="step-reserve">
        <div class="step-icon">2</div>
        <div class="step-body">
          <p class="step-title"><code>context.step("reserve-inventory")</code></p>
          <p class="step-detail" id="detail-reserve">Pending…</p>
        </div>
      </div>

      <div class="step" id="step-wait">
        <div class="step-icon">3</div>
        <div class="step-body">
          <p class="step-title"><code>context.wait_for_callback("payment-authorization")</code></p>
          <p class="step-detail" id="detail-wait">Pending…</p>
          <div class="actions hidden" id="waitActions">
            <button id="approveBtn" class="success">✓ Approve payment</button>
            <button id="rejectBtn" class="danger">✗ Reject</button>
          </div>
        </div>
      </div>

      <div class="step" id="step-fulfill">
        <div class="step-icon">4</div>
        <div class="step-body">
          <p class="step-title"><code>context.step("fulfill-order")</code></p>
          <p class="step-detail" id="detail-fulfill">Pending…</p>
        </div>
      </div>
    </div>

    <div class="metrics">
      <div class="metric">
        <div class="metric-label">⏱ Wall time</div>
        <div class="metric-value" id="metric-wall">0.0s</div>
        <div class="metric-sub">since order started</div>
      </div>
      <div class="metric">
        <div class="metric-label">💻 Active compute</div>
        <div class="metric-value" id="metric-compute">0.0s</div>
        <div class="metric-sub">GB-seconds billed</div>
      </div>
      <div class="metric">
        <div class="metric-label">💤 Hibernation</div>
        <div class="metric-value free" id="metric-hibernate">0.0s</div>
        <div class="metric-sub">billed at $0.00</div>
      </div>
    </div>
  </div>

  <footer>
    Source code on <a href="https://github.com/janobarnard/durable-demo" target="_blank" rel="noopener">GitHub</a>
    &middot;
    Built with <a href="https://docs.aws.amazon.com/lambda/latest/dg/durable-functions.html" target="_blank" rel="noopener">AWS Lambda Durable Functions</a>
  </footer>
</div>

<script>
// Derive API base from the current page URL.
// When served from API Gateway at /{stage}/ the pathname is '/<stage>/' or '/<stage>'.
const API_BASE = (() => {
  const parts = window.location.pathname.split('/').filter(Boolean);
  return parts.length ? '/' + parts[0] : '';
})();

let currentOrderId = null;
let pollTimer = null;
let tickTimer = null;
let serverStartedAt = null;  // createdAt from DDB
let latestData = {};

const $ = id => document.getElementById(id);

function setStep(id, state, detail) {
  const el = $(id);
  el.classList.remove('done', 'active', 'hibernating', 'failed');
  if (state) el.classList.add(state);
  if (detail != null) {
    const detailId = 'detail-' + id.replace('step-', '');
    $(detailId).textContent = detail;
  }
}

function setStatusBadge(status) {
  const badge = $('statusBadge');
  badge.textContent = status;
  badge.className = 'status-badge';
  if (['fulfilled'].includes(status)) badge.classList.add('ok');
  else if (['awaiting_payment'].includes(status)) badge.classList.add('hibernate');
  else if (['rejected', 'payment_declined', 'payment_failed'].includes(status)) badge.classList.add('err');
  else badge.classList.add('warn');
}

function fmtDur(ms) {
  if (ms == null || ms < 0) return '—';
  if (ms < 1000) return ms + ' ms';
  return (ms / 1000).toFixed(ms < 10000 ? 2 : 1) + ' s';
}

function fmtSec(s) {
  if (s == null || s < 0) return '0.0s';
  return s.toFixed(1) + 's';
}

async function startOrder() {
  $('startBtn').disabled = true;
  $('startBtn').textContent = 'Starting…';

  try {
    const resp = await fetch(API_BASE + '/orders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        customerId: 'CUST-WEB',
        items: [
          { sku: 'WIDGET-001', name: 'Demo Widget', qty: 2 },
          { sku: 'GADGET-007', name: 'Demo Gadget', qty: 1 },
        ],
      }),
    });
    const data = await resp.json();
    currentOrderId = data.orderId;
    serverStartedAt = data.createdAt || Date.now();

    $('orderId').textContent = currentOrderId;
    $('orderInfo').classList.remove('hidden');
    $('workflow').classList.remove('hidden');
    setStatusBadge(data.status || 'starting');

    // Reset all steps
    ['step-validate', 'step-reserve', 'step-wait', 'step-fulfill'].forEach(id => {
      setStep(id, null, 'Pending…');
    });
    setStep('step-validate', 'active', 'Running…');
    $('waitActions').classList.add('hidden');

    // Reset metrics
    $('metric-wall').textContent = '0.0s';
    $('metric-compute').textContent = '0.0s';
    $('metric-hibernate').textContent = '0.0s';

    latestData = {};
    startPolling();
    startTicker();
  } finally {
    $('startBtn').textContent = 'Start new order';
    $('startBtn').disabled = false;
  }
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollStatus, 800);
  pollStatus();
}

function startTicker() {
  if (tickTimer) clearInterval(tickTimer);
  tickTimer = setInterval(updateLiveMetrics, 100);
}

async function pollStatus() {
  if (!currentOrderId) return;
  try {
    const resp = await fetch(API_BASE + '/orders/' + currentOrderId);
    if (!resp.ok) return;
    const data = await resp.json();
    latestData = data;
    render(data);
  } catch (e) {
    console.warn('poll failed', e);
  }
}

function render(data) {
  setStatusBadge(data.status || 'unknown');

  const started = data.createdAt || serverStartedAt;
  const v = data.validateCompletedAt;
  const r = data.reserveCompletedAt;
  const h = data.hibernatingAt;
  const s = data.resumedAt;
  const f = data.fulfillCompletedAt;

  // Step 1: validate
  if (v) {
    setStep('step-validate', 'done',
      '✓ Checkpointed · ran in ' + fmtDur(v - (started || v)));
  } else if (!data.status || data.status === 'starting') {
    setStep('step-validate', 'active', 'Running…');
  }

  // Step 2: reserve
  if (r) {
    setStep('step-reserve', 'done',
      '✓ Checkpointed · ran in ' + fmtDur(r - (v || r)) +
      (data.reservationId ? ' · ' + data.reservationId : ''));
  } else if (v) {
    setStep('step-reserve', 'active', 'Running…');
  }

  // Step 3: wait
  if (h && !s && !['payment_failed', 'payment_declined'].includes(data.status)) {
    setStep('step-wait', 'hibernating',
      '💤 HIBERNATING — function is not running. Click approve/reject to resume.');
    $('waitActions').classList.remove('hidden');
  } else if (s) {
    setStep('step-wait', 'done',
      '✓ Resumed from checkpoint · hibernated for ' + fmtDur(s - h) + ' at $0.00');
    $('waitActions').classList.add('hidden');
  } else if (r && !h) {
    setStep('step-wait', 'active', 'Setting up callback…');
  }

  // Step 4: fulfill
  if (f) {
    setStep('step-fulfill', 'done',
      '✓ Checkpointed · ran in ' + fmtDur(f - (s || f)) +
      (data.trackingNumber ? ' · tracking ' + data.trackingNumber : ''));
  } else if (s) {
    setStep('step-fulfill', 'active', 'Fulfilling…');
  }

  // Failure states
  if (data.status === 'rejected') {
    setStep('step-validate', 'failed', '✗ ' + (data.reason || 'Rejected'));
  }
  if (data.status === 'payment_declined') {
    setStep('step-wait', 'failed', '✗ Payment declined');
    $('waitActions').classList.add('hidden');
  }
  if (data.status === 'payment_failed') {
    setStep('step-wait', 'failed', '✗ ' + (data.reason || 'Payment timeout'));
    $('waitActions').classList.add('hidden');
  }

  // Stop polling on terminal states
  const terminal = ['fulfilled', 'rejected', 'payment_declined', 'payment_failed'];
  if (terminal.includes(data.status)) {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    // Stop the ticker too so the final numbers stay pinned
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
    updateLiveMetrics(true);
  }
}

function updateLiveMetrics(isFinal) {
  const data = latestData;
  const started = data.createdAt || serverStartedAt;
  if (!started) return;

  const now = Date.now();
  const v = data.validateCompletedAt;
  const r = data.reserveCompletedAt;
  const h = data.hibernatingAt;
  const s = data.resumedAt;
  const f = data.fulfillCompletedAt;
  const isHibernating = h && !s && !['payment_failed', 'payment_declined'].includes(data.status);

  // Wall time: total elapsed since start
  let wallEnd;
  if (f) wallEnd = f;
  else if (['rejected', 'payment_declined', 'payment_failed'].includes(data.status)) {
    wallEnd = data.failedAt || data.declinedAt || now;
  } else {
    wallEnd = now;
  }
  const wallMs = wallEnd - started;

  // Active compute: sum of step durations (approximate when steps are in-flight)
  let activeMs = 0;
  if (v) activeMs += Math.max(0, v - started);
  else if (!r && !h) activeMs += Math.max(0, now - started); // step 1 running
  if (v && r) activeMs += Math.max(0, r - v);
  if (s && f) activeMs += Math.max(0, f - s);
  else if (s && !f) activeMs += Math.max(0, now - s); // fulfill running

  // Hibernation: time between hibernatingAt and resumedAt (or now if still waiting)
  let hibernateMs = 0;
  if (h) {
    hibernateMs = (s || now) - h;
  }

  $('metric-wall').textContent     = fmtSec(wallMs / 1000);
  $('metric-compute').textContent  = fmtSec(activeMs / 1000);
  $('metric-hibernate').textContent = fmtSec(hibernateMs / 1000);
}

async function sendAction(action) {
  const btn = action === 'approve' ? $('approveBtn') : $('rejectBtn');
  btn.disabled = true;
  try {
    await fetch(API_BASE + '/orders/' + currentOrderId + '/' + action, { method: 'POST' });
    // Force an immediate poll to reflect the wake-up
    setTimeout(pollStatus, 300);
  } finally {
    setTimeout(() => { btn.disabled = false; }, 500);
  }
}

$('startBtn').addEventListener('click', startOrder);
$('approveBtn').addEventListener('click', () => sendAction('approve'));
$('rejectBtn').addEventListener('click', () => sendAction('reject'));
</script>
</body>
</html>
"""
