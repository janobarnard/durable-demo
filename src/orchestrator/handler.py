"""
Order Processing Orchestrator — Lambda Durable Functions Demo
=============================================================

This function owns the complete order workflow. The @durable_execution
decorator enables checkpoint-and-replay: every time a context.step()
call completes, the SDK persists its result. If the function is
interrupted and re-invoked, completed steps are skipped and their
stored results are reused instead of re-executing the logic.

Workflow
--------
  1. validate-order        → reject early if inputs are invalid
  2. reserve-inventory     → mark stock as held
  3. payment-authorization → HIBERNATE here (zero compute cost while waiting)
  4. mark-resumed          → record the wake-up timestamp
  5. fulfill-order         → generate tracking number and ship

Why all side effects live inside steps
--------------------------------------
Code between steps is *re-executed* during replay. That is fine for
deterministic logic, but any DynamoDB write or wall-clock timestamp outside
a step would run again and either double-write or produce inconsistent
values. So every ``_persist`` call is placed inside a ``context.step()``
lambda — once the step is checkpointed, its side effects are frozen.

The single exception is ``_on_hibernate`` (the callable passed to
``wait_for_callback``). The SDK guarantees it is invoked exactly once,
when the wait is first set up, and never during replay. That makes it
the right place to record ``hibernatingAt`` and stash the callbackId.
"""
import json
import os
import random
import string
import time
from decimal import Decimal

import boto3
from aws_durable_execution_sdk_python import DurableContext, durable_execution
from aws_durable_execution_sdk_python.config import Duration, WaitForCallbackConfig

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['ORDERS_TABLE'])

HIBERNATION_TIMEOUT_SECONDS = int(os.environ.get('HIBERNATION_TIMEOUT_SECONDS', '600'))


@durable_execution
def lambda_handler(event: dict, context: DurableContext):
    order_id = event['orderId']
    customer_id = event['customerId']
    items = event['items']

    # ── Step 1: Validate ─────────────────────────────────────────────────────
    validation = context.step(
        lambda _: _validate_step(order_id, customer_id, items),
        name='validate-order',
    )

    if not validation['valid']:
        return {'orderId': order_id, 'status': 'rejected', 'reason': validation['reason']}

    # ── Step 2: Reserve inventory ────────────────────────────────────────────
    reservation = context.step(
        lambda _: _reserve_step(order_id, items),
        name='reserve-inventory',
    )

    # ── Step 3: Wait for payment (human-in-the-loop) ─────────────────────────
    # wait_for_callback suspends execution and terminates the Lambda invocation.
    # Compute charges stop the instant the function exits. When the callback
    # arrives (via send_durable_execution_callback_success/failure), the SDK
    # replays the function, skips the two completed steps above, and resumes
    # here with the callback result.
    def _on_hibernate(callback_id: str, _ctx) -> None:
        """Invoked exactly once by the SDK as the function enters hibernation."""
        _persist(
            order_id,
            status='awaiting_payment',
            callbackId=callback_id,
            hibernatingAt=_now_ms(),
        )
        print(f"[order={order_id}] Hibernating. callbackId={callback_id}")

    try:
        payment = context.wait_for_callback(
            _on_hibernate,
            name='payment-authorization',
            config=WaitForCallbackConfig(timeout=Duration(seconds=HIBERNATION_TIMEOUT_SECONDS)),
        )
    except Exception as exc:
        # Timeout, or explicit rejection via send_durable_execution_callback_failure.
        # Persist inside a step so the failure state is checkpointed.
        return context.step(
            lambda _: _mark_payment_failed(order_id, str(exc)),
            name='mark-payment-failed',
        )

    # ── Step 4: Record that we woke up ───────────────────────────────────────
    # Inside a step so the timestamp is checkpointed and stable across replays.
    context.step(
        lambda _: _mark_resumed(order_id),
        name='mark-resumed',
    )

    if isinstance(payment, str):
        payment = json.loads(payment)

    if not payment.get('approved', False):
        return context.step(
            lambda _: _mark_declined(order_id),
            name='mark-declined',
        )

    # ── Step 5: Fulfill the order ────────────────────────────────────────────
    fulfillment = context.step(
        lambda _: _fulfill_step(order_id, items, reservation['reservationId']),
        name='fulfill-order',
    )

    return {
        'orderId': order_id,
        'status': 'fulfilled',
        'trackingNumber': fulfillment['trackingNumber'],
        'estimatedDelivery': fulfillment['estimatedDelivery'],
    }


# ── Step implementations ──────────────────────────────────────────────────────
# Each one combines the simulated business logic with a single _persist call,
# then returns a plain dict that the SDK can serialize into a checkpoint.

def _validate_step(order_id: str, customer_id: str, items: list) -> dict:
    result = _validate_order(customer_id, items)
    now = _now_ms()
    if result['valid']:
        _persist(order_id, status='reserving', validateCompletedAt=now)
    else:
        _persist(
            order_id,
            status='rejected',
            validateCompletedAt=now,
            reason=result['reason'],
        )
    return result


def _reserve_step(order_id: str, items: list) -> dict:
    result = _reserve_inventory(order_id, items)
    _persist(
        order_id,
        status='reserved',
        reserveCompletedAt=_now_ms(),
        reservationId=result['reservationId'],
    )
    return result


def _mark_resumed(order_id: str) -> dict:
    now = _now_ms()
    _persist(order_id, status='processing_payment', resumedAt=now)
    return {'resumedAt': now}


def _mark_declined(order_id: str) -> dict:
    _persist(order_id, status='payment_declined', declinedAt=_now_ms())
    return {'orderId': order_id, 'status': 'payment_declined'}


def _mark_payment_failed(order_id: str, reason: str) -> dict:
    _persist(order_id, status='payment_failed', failedAt=_now_ms(), reason=reason)
    return {'orderId': order_id, 'status': 'payment_failed', 'reason': reason}


def _fulfill_step(order_id: str, items: list, reservation_id: str) -> dict:
    result = _fulfill_order(order_id, items, reservation_id)
    _persist(
        order_id,
        status='fulfilled',
        fulfillCompletedAt=_now_ms(),
        trackingNumber=result['trackingNumber'],
        estimatedDelivery=result['estimatedDelivery'],
    )
    return result


# ── Simulated business logic ──────────────────────────────────────────────────
# In a real application these would call downstream services (inventory API,
# payment gateway, shipping provider, etc.). The simulated versions add a
# small sleep to represent network latency so the UI can show a realistic
# "active compute" measurement.

def _validate_order(customer_id: str, items: list) -> dict:
    time.sleep(0.15)  # simulate downstream latency
    if not customer_id or not customer_id.strip():
        return {'valid': False, 'reason': 'customerId is required'}
    if not items:
        return {'valid': False, 'reason': 'Order must contain at least one item'}
    return {'valid': True}


def _reserve_inventory(order_id: str, items: list) -> dict:
    time.sleep(0.2)  # simulate downstream latency
    return {
        'reservationId': f"RSV-{order_id[:8].upper()}",
        'items': items,
        'reservedAt': _now_ms(),
    }


def _fulfill_order(order_id: str, items: list, reservation_id: str) -> dict:
    time.sleep(0.25)  # simulate downstream latency
    tracking = 'TRK-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    return {
        'trackingNumber': tracking,
        'estimatedDelivery': '3–5 business days',
        'carrier': 'DemoShip Express',
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_ms() -> int:
    return int(time.time() * 1000)


def _persist(order_id: str, **fields) -> None:
    """
    Update the order record with the supplied keyword arguments. DynamoDB
    reserved words are aliased automatically. Numbers are converted to Decimal
    so boto3 accepts them.
    """
    expr_names = {}
    expr_values = {}
    set_clauses = []

    for key, value in fields.items():
        alias = f'#f_{key}'
        expr_names[alias] = key

        # DynamoDB requires Decimal for numeric values
        if isinstance(value, (int, float)):
            value = Decimal(str(value))

        expr_values[f':v_{key}'] = value
        set_clauses.append(f'{alias} = :v_{key}')

    table.update_item(
        Key={'orderId': order_id},
        UpdateExpression='SET ' + ', '.join(set_clauses),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )
