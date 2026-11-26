"""SePay checkout and authenticated, idempotent webhook ingestion."""

import hmac
import json
import logging
import re
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.http import HttpResponseNotFound, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import views
from .models import ChargerPort, ChargingSession, PaymentOrder
from .mqtt_config import mqtt_connection

AMOUNTS = (10000, 20000, 30000, 40000)
ORDER_CODE = re.compile(r"SC[A-F0-9]{16}")
logger = logging.getLogger(__name__)


def _checkout_enabled():
    return (not settings.MQTT_PUBLIC_TEST_MODE
            and bool(settings.SEPAY_WEBHOOK_API_KEY and settings.SEPAY_BANK
                     and settings.SEPAY_ACCOUNT_NUMBER))


@require_http_methods(["GET", "POST"])
def payment_order(request):
    if not _checkout_enabled():
        return HttpResponseNotFound()
    try:
        mqtt_connection()
    except ValidationError:
        return JsonResponse({"error": "Charging is unavailable."}, status=503)

    ports = ChargerPort.objects.filter(is_active=True, port_no=1).select_related("device")
    if request.method == "POST":
        try:
            amount = int(request.POST.get("amount", ""))
            port_id = int(request.POST.get("port", ""))
            if amount not in AMOUNTS or not 0 < port_id < 2**63:
                raise ValueError
        except ValueError:
            return JsonResponse({"error": "Invalid port or amount."}, status=400)
        port = get_object_or_404(ports, pk=port_id)
        order = PaymentOrder.objects.create(
            code="SC" + secrets.token_hex(8).upper(), port=port, amount_vnd=amount,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        return redirect("myapp:payment_order_status", code=order.code)
    return render(request, "myapp/payment_order.html", {"ports": ports, "amounts": AMOUNTS})


@require_GET
def payment_order_status(request, code):
    if not _checkout_enabled() or not ORDER_CODE.fullmatch(code):
        return HttpResponseNotFound()
    order = get_object_or_404(PaymentOrder.objects.select_related("session", "port__device"), code=code)
    qr_url = None
    if not order.session_id and order.expires_at > timezone.now():
        qr_url = "https://qr.sepay.vn/img?" + urlencode({
            "acc": settings.SEPAY_ACCOUNT_NUMBER, "bank": settings.SEPAY_BANK,
            "amount": order.amount_vnd, "des": order.code,
        })
    refresh = bool(qr_url or (order.session_id and order.session.status != "done"))
    return render(request, "myapp/payment_order.html", {"order": order, "qr_url": qr_url, "refresh": refresh})


@csrf_exempt
@require_POST
def webhook(request):
    if not _checkout_enabled():
        return HttpResponseNotFound()
    expected = "Apikey " + settings.SEPAY_WEBHOOK_API_KEY
    supplied = request.headers.get("Authorization", "")
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        return JsonResponse({"success": False}, status=401)
    try:
        if len(request.body) > 16384:
            raise ValueError
        event = json.loads(request.body)
        if not isinstance(event, dict):
            raise ValueError
        code = event.get("code")
        tx_id = event.get("id")
        amount = event.get("transferAmount")
        if (not isinstance(code, str) or not ORDER_CODE.fullmatch(code)
                or type(tx_id) is not int or not 0 < tx_id < 2**63
                or event.get("transferType") != "in"
                or event.get("accountNumber") != settings.SEPAY_ACCOUNT_NUMBER
                or type(amount) is not int):
            raise ValueError
    except (ValueError, UnicodeError):
        return JsonResponse({"success": False}, status=400)

    try:
        with transaction.atomic():
            order = PaymentOrder.objects.select_for_update().select_related("session").get(code=code)
            if order.amount_vnd != amount:
                return JsonResponse({"success": False}, status=400)
            if order.session_id:
                if order.session.sepay_transaction_id != str(tx_id):
                    return JsonResponse({"success": False}, status=409)
            else:
                if order.expires_at <= timezone.now():
                    return JsonResponse({"success": False}, status=409)
                order.session = ChargingSession.objects.create(
                    sepay_transaction_id=str(tx_id), amount_vnd=amount, status="paid",
                )
                order.save(update_fields=["session"])
    except PaymentOrder.DoesNotExist:
        return JsonResponse({"success": False}, status=404)
    except IntegrityError:
        return JsonResponse({"success": False}, status=409)
    except DatabaseError:
        return JsonResponse({"success": False}, status=503)
    return JsonResponse({"success": True})


def dispatch_paid_orders():
    """Assign verified orders to their selected port, then send through private MQTT."""
    if not _checkout_enabled():
        return
    mqtt_connection()
    busy_port = ChargingSession.objects.filter(
        device_id=OuterRef("paymentorder__port__device_id"), port_no=OuterRef("paymentorder__port__port_no"),
        status__in=views.ACTIVE_STATUSES,
    ).exclude(pk=OuterRef("pk"))
    sessions = ChargingSession.objects.filter(
        paymentorder__isnull=False, status__in=("paid", "waiting", "assigned"),
        mqtt_start_sent_at__isnull=True, paymentorder__port__is_active=True,
    ).filter(~Exists(busy_port)).order_by("created_at", "pk")
    for session in sessions[:20]:
        try:
            assigned = views.assign_session_to_free_slot(session)
            if assigned:
                views.publish_start_once(assigned)
        except (DatabaseError, OSError, ValueError, ValidationError):
            logger.warning("Could not dispatch SePay session %s.", session.pk)
