import json

from db import get_db
from services.geo_service import (
    calculate_order_distance_km,
    has_valid_coordinates,
    normalize_float,
)


class DeliveryFeeError(ValueError):
    pass


DISTANCE_BAND_0_2 = "0-2KM"
DISTANCE_BAND_3_4 = "3-4KM"
DISTANCE_BAND_5_6 = "5-6KM"
DISTANCE_BAND_OVER_6 = "OVER_6KM"
DISTANCE_BAND_ADMIN = "ADMIN_REVIEW"

ALLOWED_DISTANCE_BANDS = {
    DISTANCE_BAND_0_2,
    DISTANCE_BAND_3_4,
    DISTANCE_BAND_5_6,
    DISTANCE_BAND_OVER_6,
    DISTANCE_BAND_ADMIN,
}


def _money(value, default=0):
    try:
        return max(0, int(value or default))
    except Exception:
        return int(default)


def normalize_distance_band(distance_band):
    value = str(distance_band or "").strip().upper()

    aliases = {
        "0-2": DISTANCE_BAND_0_2,
        "0-2KM": DISTANCE_BAND_0_2,
        "0_2KM": DISTANCE_BAND_0_2,
        "2": DISTANCE_BAND_0_2,
        "3-4": DISTANCE_BAND_3_4,
        "3-4KM": DISTANCE_BAND_3_4,
        "3_4KM": DISTANCE_BAND_3_4,
        "4": DISTANCE_BAND_3_4,
        "5-6": DISTANCE_BAND_5_6,
        "5-6KM": DISTANCE_BAND_5_6,
        "5_6KM": DISTANCE_BAND_5_6,
        "6": DISTANCE_BAND_5_6,
        "OVER_6": DISTANCE_BAND_OVER_6,
        "OVER_6KM": DISTANCE_BAND_OVER_6,
        ">6": DISTANCE_BAND_OVER_6,
        "ADMIN": DISTANCE_BAND_ADMIN,
        "ADMIN_REVIEW": DISTANCE_BAND_ADMIN,
    }

    return aliases.get(value, DISTANCE_BAND_0_2)


def distance_band_from_km(distance_km):
    distance = normalize_float(distance_km, 0.0)

    if distance <= 0:
        return DISTANCE_BAND_0_2

    if distance <= 2:
        return DISTANCE_BAND_0_2

    if distance <= 4:
        return DISTANCE_BAND_3_4

    if distance <= 6:
        return DISTANCE_BAND_5_6

    return DISTANCE_BAND_OVER_6


def calculate_delivery_fee_split(distance_km=None, distance_band=None, *, allow_admin_review=False):
    """
    Commercial V2 delivery fee rule.

    0-2km: total 40, customer 30, store 10.
    3-4km: total 60, customer 45, store 15.
    5-6km: total 80, customer 60, store 20.
    >6km: requires admin review.
    """
    numeric_distance = normalize_float(distance_km, 0.0)

    if numeric_distance > 0:
        band = distance_band_from_km(numeric_distance)
    else:
        band = normalize_distance_band(distance_band)

    if band in {DISTANCE_BAND_OVER_6, DISTANCE_BAND_ADMIN}:
        if allow_admin_review:
            return {
                "distance_band": DISTANCE_BAND_ADMIN,
                "distance_km": numeric_distance,
                "base_delivery_fee_twd": 0,
                "customer_delivery_share_twd": 0,
                "store_delivery_support_twd": 0,
                "requires_admin_review": True,
                "error": "配送距離超過 6km，請由 Admin 手動處理。",
            }

        raise DeliveryFeeError("配送距離超過 6km，請由 Admin 手動處理。")

    if band == DISTANCE_BAND_0_2:
        return {
            "distance_band": DISTANCE_BAND_0_2,
            "distance_km": numeric_distance,
            "base_delivery_fee_twd": 40,
            "customer_delivery_share_twd": 30,
            "store_delivery_support_twd": 10,
            "requires_admin_review": False,
            "error": "",
        }

    if band == DISTANCE_BAND_3_4:
        return {
            "distance_band": DISTANCE_BAND_3_4,
            "distance_km": numeric_distance,
            "base_delivery_fee_twd": 60,
            "customer_delivery_share_twd": 45,
            "store_delivery_support_twd": 15,
            "requires_admin_review": False,
            "error": "",
        }

    if band == DISTANCE_BAND_5_6:
        return {
            "distance_band": DISTANCE_BAND_5_6,
            "distance_km": numeric_distance,
            "base_delivery_fee_twd": 80,
            "customer_delivery_share_twd": 60,
            "store_delivery_support_twd": 20,
            "requires_admin_review": False,
            "error": "",
        }

    raise DeliveryFeeError("配送距離設定不正確。")


def is_floor_2_plus(floor_number) -> bool:
    text = str(floor_number or "").strip().lower()

    if not text:
        return False

    if "無電梯" in text or "no elevator" in text:
        return True

    digits = ""

    for ch in text:
        if ch.isdigit():
            digits += ch
        elif digits:
            break

    if not digits:
        return False

    try:
        return int(digits) >= 2
    except Exception:
        return False


def normalize_difficulty_flags(flags):
    if not flags:
        return []

    if isinstance(flags, str):
        flags = [flags]

    normalized = []

    allowed = {
        "HEAVY_ITEM": "重物 / 大量",
        "DIFFICULT_LOCATION": "地址難找",
        "SHOPPING_MALL_CENTER": "商場中心",
        "REMOTE_AREA": "偏遠 / 非中心區",
        "RAIN": "雨天",
        "FLOOR_2_PLUS": "2樓以上 / 無電梯",
    }

    for flag in flags:
        key = str(flag or "").strip().upper()

        if key in allowed and key not in normalized:
            normalized.append(key)

    return normalized


def is_rain_enabled(db=None) -> bool:
    if db is None:
        db = get_db()

    try:
        from services.system_flag_service import is_rain_surcharge_enabled

        return bool(is_rain_surcharge_enabled(db))
    except Exception:
        return False


def calculate_single_extra_fee(
    db=None,
    *,
    floor_number="",
    difficulty_flags=None,
    manual_extra_reason="",
    include_rain=True,
):
    """
    Extra delivery fee rule:
    - max +20 TWD per order.
    - no stacking.
    """
    reasons = []
    normalized_flags = normalize_difficulty_flags(difficulty_flags)

    if is_floor_2_plus(floor_number):
        if "FLOOR_2_PLUS" not in normalized_flags:
            normalized_flags.append("FLOOR_2_PLUS")
        reasons.append("2樓以上 / 無電梯")

    flag_labels = {
        "HEAVY_ITEM": "重物 / 大量",
        "DIFFICULT_LOCATION": "地址難找",
        "SHOPPING_MALL_CENTER": "商場中心",
        "REMOTE_AREA": "偏遠 / 非中心區",
        "RAIN": "雨天",
        "FLOOR_2_PLUS": "2樓以上 / 無電梯",
    }

    for flag in normalized_flags:
        label = flag_labels.get(flag)

        if label and label not in reasons:
            reasons.append(label)

    rain_active = False

    if include_rain:
        rain_active = is_rain_enabled(db)

        if rain_active:
            if "RAIN" not in normalized_flags:
                normalized_flags.append("RAIN")
            if "雨天" not in reasons:
                reasons.append("雨天")

    manual_extra_reason = str(manual_extra_reason or "").strip()

    if manual_extra_reason:
        reasons.append(manual_extra_reason)

    has_extra_fee = bool(reasons)
    extra_fee_twd = 20 if has_extra_fee else 0

    return {
        "extra_fee_twd": extra_fee_twd,
        "rain_fee_twd": 20 if rain_active and has_extra_fee else 0,
        "has_extra_fee": has_extra_fee,
        "difficulty_flags": normalized_flags,
        "difficulty_flags_json": json.dumps(normalized_flags, ensure_ascii=False),
        "extra_fee_reason": "、".join(reasons),
    }


def calculate_distance_and_fee_for_order(
    *,
    store_lat=0,
    store_lng=0,
    delivery_lat=0,
    delivery_lng=0,
    distance_band="0-2KM",
    allow_admin_review=False,
):
    """
    Prefer real GPS distance. If GPS is incomplete, use distance_band fallback.
    """
    distance_km = 0.0

    if has_valid_coordinates(store_lat, store_lng) and has_valid_coordinates(delivery_lat, delivery_lng):
        distance_km = calculate_order_distance_km(
            store_lat,
            store_lng,
            delivery_lat,
            delivery_lng,
        )

    fee = calculate_delivery_fee_split(
        distance_km=distance_km,
        distance_band=distance_band,
        allow_admin_review=allow_admin_review,
    )

    fee["distance_km"] = distance_km
    return fee


def build_delivery_fee_rule_json(fee, extra=None):
    payload = {
        "rule_version": "COMMERCIAL_V2_GPS_BAND",
        "distance_band": fee.get("distance_band", DISTANCE_BAND_0_2),
        "distance_km": fee.get("distance_km", 0),
        "base_delivery_fee_twd": _money(fee.get("base_delivery_fee_twd")),
        "customer_delivery_share_twd": _money(fee.get("customer_delivery_share_twd")),
        "store_delivery_support_twd": _money(fee.get("store_delivery_support_twd")),
        "requires_admin_review": bool(fee.get("requires_admin_review")),
        "extra_fee_twd": _money((extra or {}).get("extra_fee_twd")),
        "extra_fee_reason": (extra or {}).get("extra_fee_reason", ""),
        "extra_fee_rule": "MAX_20_ONCE_PER_ORDER",
    }

    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def build_delivery_fee_preview(
    *,
    subtotal_twd=0,
    store_lat=0,
    store_lng=0,
    delivery_lat=0,
    delivery_lng=0,
    distance_band="0-2KM",
    floor_number="",
    difficulty_flags=None,
    manual_extra_reason="",
):
    fee = calculate_distance_and_fee_for_order(
        store_lat=store_lat,
        store_lng=store_lng,
        delivery_lat=delivery_lat,
        delivery_lng=delivery_lng,
        distance_band=distance_band,
    )

    extra = calculate_single_extra_fee(
        floor_number=floor_number,
        difficulty_flags=difficulty_flags or [],
        manual_extra_reason=manual_extra_reason,
    )

    subtotal_twd = _money(subtotal_twd)

    total_twd = (
        subtotal_twd
        + _money(fee["customer_delivery_share_twd"])
        + _money(extra["extra_fee_twd"])
    )

    return {
        **fee,
        **extra,
        "subtotal_twd": subtotal_twd,
        "total_twd": total_twd,
        "delivery_fee_rule_json": build_delivery_fee_rule_json(fee, extra),
    }
