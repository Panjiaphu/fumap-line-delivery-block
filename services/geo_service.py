import math
from urllib.parse import quote_plus


EARTH_RADIUS_KM = 6371.0088


class GeoError(ValueError):
    pass


def normalize_float(value, default=0.0):
    try:
        if value is None:
            return float(default)

        value = str(value).strip()

        if value == "":
            return float(default)

        return float(value)

    except Exception:
        return float(default)


def normalize_lat_lng(value):
    return normalize_float(value, 0.0)


def has_valid_coordinates(lat, lng) -> bool:
    lat = normalize_lat_lng(lat)
    lng = normalize_lat_lng(lng)

    if lat == 0 or lng == 0:
        return False

    if lat < -90 or lat > 90:
        return False

    if lng < -180 or lng > 180:
        return False

    return True


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    lat1 = normalize_lat_lng(lat1)
    lng1 = normalize_lat_lng(lng1)
    lat2 = normalize_lat_lng(lat2)
    lng2 = normalize_lat_lng(lng2)

    if not has_valid_coordinates(lat1, lng1):
        raise GeoError("店家座標不正確。")

    if not has_valid_coordinates(lat2, lng2):
        raise GeoError("收貨座標不正確。")

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)

    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )

    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    distance = EARTH_RADIUS_KM * c

    return round(distance, 3)


def calculate_order_distance_km(store_lat, store_lng, delivery_lat, delivery_lng):
    if not has_valid_coordinates(store_lat, store_lng):
        return 0.0

    if not has_valid_coordinates(delivery_lat, delivery_lng):
        return 0.0

    return haversine_km(store_lat, store_lng, delivery_lat, delivery_lng)


def format_coordinates(lat, lng) -> str:
    lat = normalize_lat_lng(lat)
    lng = normalize_lat_lng(lng)

    if not has_valid_coordinates(lat, lng):
        return ""

    return f"{lat:.6f},{lng:.6f}"


def google_maps_search_url(address_or_query: str) -> str:
    query = str(address_or_query or "").strip()

    if not query:
        return ""

    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(query)


def google_maps_coordinates_url(lat, lng) -> str:
    coords = format_coordinates(lat, lng)

    if not coords:
        return ""

    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(coords)


def google_maps_direction_url(origin, destination) -> str:
    origin = str(origin or "").strip()
    destination = str(destination or "").strip()

    if not destination:
        return ""

    if origin:
        return (
            "https://www.google.com/maps/dir/?api=1"
            + "&origin="
            + quote_plus(origin)
            + "&destination="
            + quote_plus(destination)
        )

    return (
        "https://www.google.com/maps/dir/?api=1"
        + "&destination="
        + quote_plus(destination)
    )


def google_maps_best_url(*, address="", lat=0, lng=0) -> str:
    coords_url = google_maps_coordinates_url(lat, lng)

    if coords_url:
        return coords_url

    return google_maps_search_url(address)
