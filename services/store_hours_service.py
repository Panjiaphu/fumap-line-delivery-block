import json
from datetime import datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


DEFAULT_OPEN_TIME = "10:00"
DEFAULT_CLOSE_TIME = "21:00"
DEFAULT_LAST_ORDER_MINUTES = 30
DEFAULT_OPEN_DAYS = [0, 1, 2, 3, 4, 5, 6]

WEEKDAY_LABELS = {
    0: "週一",
    1: "週二",
    2: "週三",
    3: "週四",
    4: "週五",
    5: "週六",
    6: "週日",
}

STATUS_LABELS = {
    "OPEN": "目前可接單",
    "MANUAL_CLOSED": "店家休息中",
    "TEMP_CLOSED": "今日暫停接單",
    "DAY_OFF": "今日店休",
    "BEFORE_OPEN": "尚未開店",
    "AFTER_CUTOFF": "已過最後接單時間",
    "AFTER_CLOSE": "已打烊",
    "INACTIVE": "店家尚未啟用",
    "SETUP_INCOMPLETE": "店家資料未完成",
}


def taipei_now():
    """
    Return current datetime in Asia/Taipei.

    Fallback to UTC+8 if zoneinfo is not available.
    """
    if ZoneInfo:
        try:
            return datetime.now(ZoneInfo("Asia/Taipei"))
        except Exception:
            pass

    return datetime.now(timezone.utc) + timedelta(hours=8)


def _row_get(row, key, default=None):
    if row is None:
        return default

    try:
        if key in row.keys():
            value = row[key]
            return default if value is None else value
    except Exception:
        pass

    try:
        value = row.get(key, default)
        return default if value is None else value
    except Exception:
        return default


def _int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def parse_hhmm(value, default=DEFAULT_OPEN_TIME):
    """
    Parse HH:MM into datetime.time.
    Invalid value returns default.
    """
    value = str(value or "").strip()
    default = str(default or DEFAULT_OPEN_TIME).strip()

    def _parse(raw):
        parts = raw.split(":")
        if len(parts) != 2:
            raise ValueError("invalid HH:MM")

        hour = int(parts[0])
        minute = int(parts[1])

        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError("invalid time range")

        return time(hour=hour, minute=minute)

    try:
        return _parse(value)
    except Exception:
        try:
            return _parse(default)
        except Exception:
            return time(hour=10, minute=0)


def normalize_hhmm(value, default=DEFAULT_OPEN_TIME):
    parsed = parse_hhmm(value, default)
    return f"{parsed.hour:02d}:{parsed.minute:02d}"


def parse_open_days(open_days_json):
    """
    Parse JSON list of weekdays.

    Python weekday:
    Monday = 0
    Sunday = 6

    Null/empty/invalid defaults to every day to avoid accidentally locking stores.
    """
    if not open_days_json:
        return DEFAULT_OPEN_DAYS[:]

    try:
        if isinstance(open_days_json, (list, tuple)):
            raw_days = open_days_json
        else:
            raw_days = json.loads(str(open_days_json))

        days = []

        for item in raw_days:
            day = int(item)

            if 0 <= day <= 6 and day not in days:
                days.append(day)

        if not days:
            return DEFAULT_OPEN_DAYS[:]

        return sorted(days)

    except Exception:
        return DEFAULT_OPEN_DAYS[:]


def dump_open_days(days):
    clean_days = []

    for item in days or []:
        try:
            day = int(item)
        except Exception:
            continue

        if 0 <= day <= 6 and day not in clean_days:
            clean_days.append(day)

    if not clean_days:
        clean_days = DEFAULT_OPEN_DAYS[:]

    return json.dumps(sorted(clean_days), ensure_ascii=False)


def normalize_open_days_from_form(form):
    """
    Read request.form.getlist("open_days").
    If nothing is selected, default to every day.
    """
    try:
        raw_days = form.getlist("open_days")
    except Exception:
        raw_days = []

    return parse_open_days(raw_days)


def open_days_labels(days):
    days = parse_open_days(days)

    return [
        WEEKDAY_LABELS.get(day, str(day))
        for day in days
    ]


def _time_to_minutes(value):
    if isinstance(value, time):
        return value.hour * 60 + value.minute

    parsed = parse_hhmm(value, DEFAULT_OPEN_TIME)
    return parsed.hour * 60 + parsed.minute


def _minutes_to_hhmm(value):
    value = int(value or 0)
    value = max(0, min(value, 23 * 60 + 59))

    hour = value // 60
    minute = value % 60

    return f"{hour:02d}:{minute:02d}"


def _status_payload(
    *,
    accepting,
    code,
    reason,
    open_time,
    close_time,
    cutoff_time,
    today_weekday,
    open_days,
):
    label = STATUS_LABELS.get(code, code)

    return {
        "accepting": bool(accepting),
        "code": code,
        "label": label,
        "reason": reason or label,
        "open_time": open_time,
        "close_time": close_time,
        "cutoff_time": cutoff_time,
        "today_weekday": today_weekday,
        "open_days": open_days,
        "open_days_labels": open_days_labels(open_days),
    }


def is_store_accepting_orders(store, now=None):
    """
    Realtime business-hours decision.

    This does not mutate stores.is_open.
    It only calculates whether the store can accept new customer orders now.
    """
    now = now or taipei_now()

    open_time = normalize_hhmm(
        _row_get(store, "open_time", DEFAULT_OPEN_TIME),
        DEFAULT_OPEN_TIME,
    )
    close_time = normalize_hhmm(
        _row_get(store, "close_time", DEFAULT_CLOSE_TIME),
        DEFAULT_CLOSE_TIME,
    )

    open_days = parse_open_days(_row_get(store, "open_days_json", ""))
    today_weekday = now.weekday()

    last_order_minutes = _int(
        _row_get(store, "last_order_minutes_before_close", DEFAULT_LAST_ORDER_MINUTES),
        DEFAULT_LAST_ORDER_MINUTES,
    )
    last_order_minutes = max(0, min(180, last_order_minutes))

    open_minutes = _time_to_minutes(open_time)
    close_minutes = _time_to_minutes(close_time)
    now_minutes = now.hour * 60 + now.minute
    cutoff_minutes = max(open_minutes, close_minutes - last_order_minutes)
    cutoff_time = _minutes_to_hhmm(cutoff_minutes)

    status = str(_row_get(store, "status", "") or "").strip().upper()
    setup_completed = _int(_row_get(store, "setup_completed", 0), 0)
    is_open = _int(_row_get(store, "is_open", 1), 1)
    is_temporarily_closed = _int(_row_get(store, "is_temporarily_closed", 0), 0)
    temporary_close_reason = str(
        _row_get(store, "temporary_close_reason", "") or ""
    ).strip()

    common = {
        "open_time": open_time,
        "close_time": close_time,
        "cutoff_time": cutoff_time,
        "today_weekday": today_weekday,
        "open_days": open_days,
    }

    if status != "ACTIVE":
        return _status_payload(
            accepting=False,
            code="INACTIVE",
            reason="店家尚未啟用或尚未通過審核。",
            **common,
        )

    if setup_completed != 1:
        return _status_payload(
            accepting=False,
            code="SETUP_INCOMPLETE",
            reason="店家資料尚未完成。",
            **common,
        )

    if is_open != 1:
        return _status_payload(
            accepting=False,
            code="MANUAL_CLOSED",
            reason="店家已手動設定為休息中。",
            **common,
        )

    if is_temporarily_closed == 1:
        reason = temporary_close_reason or "店家今日暫停接單。"

        return _status_payload(
            accepting=False,
            code="TEMP_CLOSED",
            reason=f"店家已設定今日暫停：{reason}",
            **common,
        )

    if today_weekday not in open_days:
        return _status_payload(
            accepting=False,
            code="DAY_OFF",
            reason="今日不是店家的固定營業日。",
            **common,
        )

    if close_minutes <= open_minutes:
        return _status_payload(
            accepting=False,
            code="AFTER_CLOSE",
            reason="V1 暫不支援跨日營業時間，請店家重新設定營業時間。",
            **common,
        )

    if now_minutes < open_minutes:
        return _status_payload(
            accepting=False,
            code="BEFORE_OPEN",
            reason=f"尚未到開店時間。今日營業時間 {open_time} - {close_time}。",
            **common,
        )

    if now_minutes >= close_minutes:
        return _status_payload(
            accepting=False,
            code="AFTER_CLOSE",
            reason=f"店家已打烊。今日營業時間 {open_time} - {close_time}。",
            **common,
        )

    if now_minutes >= cutoff_minutes:
        return _status_payload(
            accepting=False,
            code="AFTER_CUTOFF",
            reason=f"已過最後接單時間 {cutoff_time}，避免店家來不及製作。",
            **common,
        )

    return _status_payload(
        accepting=True,
        code="OPEN",
        reason=f"今日營業時間 {open_time} - {close_time}，最後接單 {cutoff_time}。",
        **common,
    )


def should_show_store_publicly(store, now=None):
    """
    V1: still show active/setup-completed stores publicly.
    They may be shown with a closed badge.
    """
    status = str(_row_get(store, "status", "") or "").strip().upper()
    setup_completed = _int(_row_get(store, "setup_completed", 0), 0)

    return status == "ACTIVE" and setup_completed == 1


def annotate_store_hours(store, now=None):
    """
    Convert sqlite row/dict into dict and attach business-hours status.
    """
    if store is None:
        return {}

    try:
        data = dict(store)
    except Exception:
        data = store.copy() if isinstance(store, dict) else {}

    status = is_store_accepting_orders(data, now=now)

    data["business_hours_status"] = status
    data["accepting_orders"] = status["accepting"]
    data["accepting_orders_code"] = status["code"]
    data["accepting_orders_label"] = status["label"]
    data["accepting_orders_reason"] = status["reason"]
    data["open_days"] = status["open_days"]
    data["open_days_labels"] = status["open_days_labels"]
    data["cutoff_time"] = status["cutoff_time"]

    return data
