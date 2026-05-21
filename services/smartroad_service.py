import json
import re
import unicodedata


DIFFICULTY_KEYWORDS = [
    "難找",
    "上樓",
    "電梯",
    "沒電梯",
    "重",
    "DIFFICULT",
    "HARD",
    "KHO",
    "KHÓ",
    "LEN LAU",
    "LÊN LẦU",
]

DIFFICULTY_FLAGS = {
    "HEAVY",
    "STAIRS",
    "HARD_ADDRESS",
    "MALL",
}


def normalize_text(value):
    text = str(value or "").strip()

    if not text:
        return ""

    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_latin(value):
    text = normalize_text(value)

    if not text:
        return ""

    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = unicodedata.normalize("NFKC", text)
    text = text.upper()
    text = re.sub(r"[,\.;:#]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _compact_cjk(text):
    return re.sub(r"\s+", "", normalize_text(text))


def parse_house_number(address):
    """
    Simple MVP parser for house number.

    Supported examples:
    - "40 Lý Hồng Phong"
    - "Lý Hồng Phong 40"
    - "桃園市中壢區中正路320號"
    - "No. 320, Zhongzheng Road"
    - "320 Zhongzheng Rd"

    Returns string number, or "".
    """
    raw = normalize_text(address)

    if not raw:
        return ""

    text = unicodedata.normalize("NFKC", raw)

    patterns = [
        r"(?:NO\.?|NUMBER|#)\s*([0-9]{1,6})\b",
        r"([0-9]{1,6})\s*號",
        r"\b([0-9]{1,6})\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)

        if match:
            return str(int(match.group(1)))

    return ""


def house_parity(number):
    try:
        n = int(str(number or "").strip())

        if n <= 0:
            return "UNKNOWN"

        return "EVEN" if n % 2 == 0 else "ODD"

    except Exception:
        return "UNKNOWN"


def _remove_house_number_noise(text):
    text = normalize_text(text)
    text = re.sub(r"(?:NO\.?|NUMBER|#)\s*[0-9]{1,6}", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[0-9]{1,6}\s*號", " ", text)
    text = re.sub(r"\b[0-9]{1,6}\b", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_chinese_road_name(address):
    text = _compact_cjk(address)

    if not text:
        return ""

    # Match the last plausible road segment before a house number.
    # Examples:
    # 桃園市中壢區中正路320號 -> 中正路
    # 台北市大安區忠孝東路四段20號 -> 忠孝東路
    patterns = [
        r"([\u4e00-\u9fff0-9一二三四五六七八九十]+(?:大道|路|街|巷))\d*號?",
        r"([\u4e00-\u9fff0-9一二三四五六七八九十]+(?:大道|路|街|巷))",
    ]

    candidates = []

    for pattern in patterns:
        for match in re.finditer(pattern, text):
            value = match.group(1).strip()

            if value:
                candidates.append(value)

    if candidates:
        return candidates[-1]

    return ""


def _parse_latin_road_name(address):
    text = normalize_latin(address)

    if not text:
        return ""

    text = _remove_house_number_noise(text)
    text = normalize_latin(text)

    # Remove common administrative/address words but keep the actual road phrase.
    text = re.sub(r"\b(NO|NUMBER|TAOYUAN|ZHONGLI|DISTRICT|CITY|COUNTY|TAIWAN)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    road_suffixes = [
        "ROAD",
        "RD",
        "STREET",
        "ST",
        "AVENUE",
        "AVE",
        "BOULEVARD",
        "BLVD",
        "LANE",
        "LN",
        "ALLEY",
        "DUONG",
        "DƯỜNG",
    ]

    words = text.split()

    if not words:
        return ""

    # English-style: Zhongzheng Road, Ly Hong Phong Street.
    for i, word in enumerate(words):
        if word in road_suffixes and i > 0:
            start = max(0, i - 4)
            candidate_words = words[start : i + 1]
            candidate = " ".join(candidate_words)
            candidate = candidate.replace(" RD", " ROAD")
            candidate = candidate.replace(" ST", " STREET")
            candidate = candidate.replace(" AVE", " AVENUE")
            return candidate.strip()

    # Vietnamese-style without explicit "duong": "Ly Hong Phong".
    # After removing numbers, keep a reasonable road-like phrase.
    if len(words) >= 2:
        return " ".join(words[-4:]).strip()

    return words[0].strip()


def parse_road_name(address):
    """
    Simple MVP road-name parser.

    Returns normalized road name, or "" if not parseable.
    """
    raw = normalize_text(address)

    if not raw:
        return ""

    cjk = _parse_chinese_road_name(raw)

    if cjk:
        return cjk

    return _parse_latin_road_name(raw)


def same_road(store_address, delivery_address):
    store_road = parse_road_name(store_address)
    customer_road = parse_road_name(delivery_address)

    return bool(store_road and customer_road and store_road == customer_road)


def same_side(store_house_number, customer_house_number):
    store_parity = house_parity(store_house_number)
    customer_parity = house_parity(customer_house_number)

    if store_parity == "UNKNOWN" or customer_parity == "UNKNOWN":
        return None

    return store_parity == customer_parity


def detect_uturn_risk(store_number, customer_number, is_same_road, is_same_side):
    """
    MVP hint only. This is not a real route-direction engine.
    """
    if not is_same_road:
        return False

    if is_same_side is False:
        return True

    try:
        store_n = int(str(store_number or "").strip())
        customer_n = int(str(customer_number or "").strip())
    except Exception:
        return False

    if store_n <= 0 or customer_n <= 0:
        return False

    # Same side but house number decreases: possibly reverse direction.
    if is_same_side is True and customer_n < store_n:
        return True

    return False


def score_to_lane(score):
    score = _clamp_score(score)

    if score >= 75:
        return "GREEN"

    if score >= 50:
        return "YELLOW"

    return "RED"


def score_to_label(score):
    score = _clamp_score(score)

    if score >= 75:
        return "EASY"

    if score >= 50:
        return "NORMAL"

    return "HARD"


def _clamp_score(score):
    try:
        score = int(round(float(score)))
    except Exception:
        score = 50

    return max(0, min(100, score))


def _float(value, default=0.0):
    try:
        return float(value or 0)
    except Exception:
        return float(default)


def _same_city_block(store_city_block, order_city_block):
    store_city = normalize_latin(store_city_block)
    order_city = normalize_latin(order_city_block)

    if not store_city or not order_city:
        return None

    return store_city == order_city


def _has_low_floor_or_no_floor(floor_number):
    text = normalize_latin(floor_number)

    if not text:
        return True

    # Common no-stairs/low-floor values.
    if text in {"1", "1F", "F1", "GROUND", "GROUND FLOOR", "一樓"}:
        return True

    return False


def _address_note_has_difficulty(address_note):
    note_raw = normalize_text(address_note)
    note_latin = normalize_latin(address_note)

    if not note_raw and not note_latin:
        return False

    for keyword in DIFFICULTY_KEYWORDS:
        if keyword in note_raw or keyword in note_latin:
            return True

    return False


def _difficulty_flags_set(difficulty_flags):
    if difficulty_flags is None:
        return set()

    if isinstance(difficulty_flags, str):
        flags = re.split(r"[,|\s]+", difficulty_flags)
    else:
        try:
            flags = list(difficulty_flags)
        except Exception:
            flags = []

    return {normalize_latin(flag) for flag in flags if normalize_latin(flag)}


def calculate_smartroad_score(
    *,
    store_address,
    delivery_address,
    store_city_block="",
    order_city_block="",
    distance_km=0,
    has_valid_gps=False,
    floor_number="",
    address_note="",
    difficulty_flags=None,
    rain_fee_twd=0,
    extra_fee_twd=0,
):
    """
    SmartRoad V1: hint/risk scoring only.

    It does not:
    - call Google Maps
    - optimize actual route
    - block Driver from accepting
    - modify settlement/accounting
    - connect to FGO/TimeBlock
    """
    score = 50
    reasons = []

    distance = _float(distance_km, 0)
    city_same = _same_city_block(store_city_block, order_city_block)

    store_road_name = parse_road_name(store_address)
    customer_road_name = parse_road_name(delivery_address)
    store_house_number = parse_house_number(store_address)
    customer_house_number = parse_house_number(delivery_address)
    store_house_parity = house_parity(store_house_number)
    customer_house_parity = house_parity(customer_house_number)

    is_same_road = bool(
        store_road_name
        and customer_road_name
        and store_road_name == customer_road_name
    )
    is_same_side = same_side(store_house_number, customer_house_number)
    uturn_risk = detect_uturn_risk(
        store_house_number,
        customer_house_number,
        is_same_road,
        is_same_side,
    )

    flags = _difficulty_flags_set(difficulty_flags)
    has_difficulty_flags = bool(flags.intersection(DIFFICULTY_FLAGS))
    low_floor_or_no_floor = _has_low_floor_or_no_floor(floor_number)
    note_is_difficult = _address_note_has_difficulty(address_note)

    if distance > 0 and distance <= 2:
        score += 20
        reasons.append("distance <= 2km: +20")
    elif distance > 2 and distance <= 5:
        score -= 10
        reasons.append("distance 2-5km: -10")
    elif distance > 5:
        score -= 20
        reasons.append("distance > 5km: -20")
    else:
        reasons.append("distance unknown: +0")

    if city_same is True:
        score += 10
        reasons.append("same city_block: +10")
    elif city_same is False:
        score -= 15
        reasons.append("different city_block: -15")
    else:
        reasons.append("city_block unknown: +0")

    if has_valid_gps:
        score += 10
        reasons.append("valid GPS: +10")
    else:
        score -= 10
        reasons.append("missing GPS: -10")

    if is_same_road:
        score += 15
        reasons.append("same road: +15")

        if is_same_side is True:
            score += 10
            reasons.append("same side/parity: +10")
        elif is_same_side is False:
            score -= 10
            reasons.append("different side/parity: -10")
        else:
            reasons.append("same side unknown: +0")
    else:
        reasons.append("same road unknown/false: +0")

    if uturn_risk:
        score -= 15
        reasons.append("possible U-turn risk: -15")
    else:
        reasons.append("no U-turn risk detected: +0")

    if low_floor_or_no_floor:
        score += 5
        reasons.append("no/low floor: +5")
    else:
        score -= 10
        reasons.append("floor/stairs risk: -10")

    if note_is_difficult:
        score -= 10
        reasons.append("difficult address note: -10")
    else:
        score += 5
        reasons.append("no difficult address note: +5")

    if has_difficulty_flags:
        score -= 10
        reasons.append("difficulty flags: -10")
    else:
        score += 5
        reasons.append("no difficulty flags: +5")

    if _float(rain_fee_twd, 0) > 0:
        score -= 10
        reasons.append("rain fee/rain flag: -10")

    if _float(extra_fee_twd, 0) > 0:
        reasons.append("extra fee exists: info only")

    score = _clamp_score(score)
    lane = score_to_lane(score)
    label = score_to_label(score)

    if score < 50 and _float(extra_fee_twd, 0) <= 0:
        reasons.append("score < 50: extra fee may be considered, no auto-set in V1")

    return {
        "score": score,
        "label": label,
        "lane": lane,
        "reasons": reasons,
        "reasons_json": json.dumps(reasons, ensure_ascii=False),
        "same_road": bool(is_same_road),
        "same_side": is_same_side,
        "uturn_risk": bool(uturn_risk),
        "store_road_name": store_road_name,
        "customer_road_name": customer_road_name,
        "store_house_number": store_house_number,
        "customer_house_number": customer_house_number,
        "store_house_parity": store_house_parity,
        "customer_house_parity": customer_house_parity,
    }


def smartroad_db_payload(result):
    """
    Convert calculate_smartroad_score() result into DB-friendly fields.
    """
    same_side_value = result.get("same_side")

    return {
        "smartroad_score": int(result.get("score") or 50),
        "smartroad_score_label": result.get("label") or "UNKNOWN",
        "smartroad_reasons_json": result.get("reasons_json") or "[]",
        "smartroad_same_road": 1 if result.get("same_road") else 0,
        "smartroad_same_side": 1 if same_side_value is True else 0,
        "smartroad_uturn_risk": 1 if result.get("uturn_risk") else 0,
        "store_road_name": result.get("store_road_name") or "",
        "customer_road_name": result.get("customer_road_name") or "",
        "store_house_number": result.get("store_house_number") or "",
        "customer_house_number": result.get("customer_house_number") or "",
        "store_house_parity": result.get("store_house_parity") or "UNKNOWN",
        "customer_house_parity": result.get("customer_house_parity") or "UNKNOWN",
        "smartroad_lane": result.get("lane") or "YELLOW",
    }
