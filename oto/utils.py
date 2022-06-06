import base64
import hashlib
import hmac
import json
import logging
from threading import Lock

from django.contrib.sites.models import Site
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden

from saleor.account.models import User
from saleor.order.actions import cancel_fulfillment
from saleor.order.models import Fulfillment
from saleor.plugins.manager import get_plugins_manager

logger = logging.Logger(__name__)


def get_order_customer_data(order):
    return {
        "email": order.get_customer_email(),
        "country": order.shipping_address.country.code,
        "postcode": order.shipping_address.postal_code,
        "address": order.shipping_address.street_address_1
        or order.shipping_address.street_address_2,
        "name": order.user.get_full_name() if order.user else "",
        "city": order.shipping_address.city if order.shipping_address else "",
        "mobile": str(order.shipping_address.phone) if order.shipping_address else "",
        "district": order.shipping_address.city_area if order.shipping_address else "",
    }


def get_order_items_data(fulfillment):
    site = Site.objects.get_current()
    return [
        {
            "quantity": line.quantity,
            "sku": line.order_line.product_sku,
            "name": line.order_line.product_name,
            "productId": line.order_line.variant_id,
            "price": float(line.order_line.total_price_net_amount),
            "image": "%s%s"
            % (site.domain, line.order_line.variant.product.get_first_image().image.url)
            if line.order_line.variant.product.get_first_image()
            else "",
        }
        for line in fulfillment.lines.all()
    ]


def get_oto_order_id(fulfillment: Fulfillment):
    return str(f"#{fulfillment.composed_id}")


def generate_create_order_data(fulfillment):
    fulfillment_line = fulfillment.lines.last()
    fulfillments_count = fulfillment.order.fulfillments.count()
    is_cod_order = (
        True
        if fulfillment.order.get_last_payment().gateway == "payments.cash"
        else False
    )
    shipping_amount = float(
        fulfillment.order.shipping_price_net_amount if fulfillments_count == 1 else 0
    )
    data = {
        "storeName": "WeCre8",
        "shippingAmount": shipping_amount,
        "currency": fulfillment.order.currency,
        "ref1": fulfillment.order.checkout_token,
        "shippingNotes": fulfillment.order.customer_note,
        "payment_method": "cod" if is_cod_order else "paid",
        "orderId": get_oto_order_id(fulfillment=fulfillment),
        "items": get_order_items_data(fulfillment=fulfillment),
        "customer": get_order_customer_data(order=fulfillment.order),
        "subtotal": float(fulfillment.order.get_subtotal().net.amount),
        "amount_due": float(
            (float(fulfillment.order.total_gross_amount)) if is_cod_order else 0
        ),
        "amount": float(
            fulfillment_line.quantity
            * fulfillment_line.order_line.unit_price_net_amount
        ),
        "orderDate": "%s %s:%s"
        % (
            str(fulfillment.created_at.date().strftime("%d/%m/%Y")),
            str(fulfillment.created_at.hour),
            str(fulfillment.created_at.minute),
        ),
    }
    return data


def generate_cancel_order_and_return_link_data(fulfillment: "Fulfillment"):
    return dict(
        orderId=get_oto_order_id(fulfillment=fulfillment),
    )


def verify_webhook(request: HttpRequest, config: "dict"):
    """Verify webhook request from OTO."""
    data = json.loads(request.body)
    signature = data.get("signature")
    msg = f"{data['orderId']}:{data['status']}:{data['timestamp']}"
    h = hmac.new(
        msg=msg.encode("utf-8"),
        digestmod=hashlib.sha256,
        key=config.get("PUBLIC_KEY_FOR_SIGNATURE", "").encode("utf-8"),
    ).digest()
    h = base64.b64encode(h).decode("utf-8")

    if h != signature:
        return HttpResponseForbidden("Invalid signature")
    return True


def handle_webhook(request: HttpRequest, config: "dict"):
    """Handle webhook from OTO API."""
    if verify_webhook(request=request, config=config) is True:
        data = json.loads(request.body)
        orderId = data.get("orderId").split("-")

        fulfillment = Fulfillment.objects.filter(
            order__pk=int(orderId[0].split("#")[1]), fulfillment_order=int(orderId[1])
        ).first()
        if fulfillment:
            status = data.get("status", "")
            fulfillment.tracking_number = data.get("trackingNumber", "")
            fulfillment.store_value_in_metadata(
                items={
                    "otoStatus": status,
                    "printAWBURL": data.get("printAWBURL", ""),
                    "trackingURL": data.get("trackingURL", ""),
                    "feedbackLink": data.get("feedbackLink", ""),
                    "shippingCompanyStatus": data.get("dcStatus", ""),
                    "deliveryCompany": data.get("deliveryCompany", ""),
                    "deliverySlotDate": data.get("deliverySlotDate", ""),
                }
            )
            fulfillment.save(update_fields=["tracking_number", "metadata"])
            logger.info("Fulfillment #%s updated", fulfillment.composed_id)

            # Cancel order if OTO user cancel the OTO order
            user, _ = User.objects.get_or_create(email="admin@example.com")
            if status == "canceled":
                cancel_fulfillment(
                    app=None,
                    user=user,
                    warehouse=None,
                    fulfillment=fulfillment,
                    manager=get_plugins_manager(),
                )
                logger.info("Fulfillment #%s cancelled", fulfillment.composed_id)
            return HttpResponse("OK")
        else:
            logger.info(f"Fulfillment {data.get('orderId')} not found")
            return HttpResponse(f"Fulfillment {data.get('orderId')} not found))")
    else:
        logger.info("Webhook is not verified!")
        return HttpResponseForbidden("Webhook is not verified!")


class SingletonMeta(type):
    _instances = {}
    _lock: Lock = Lock()

    def __call__(cls, *args, **kwargs):
        with cls._lock:
            if cls not in cls._instances:
                instance = super().__call__(*args, **kwargs)
                cls._instances[cls] = instance
        return cls._instances[cls]
