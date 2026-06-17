import re
from datetime import datetime, timedelta, date as date_obj
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from .models import SystemSettings


_DURATION_UNITS = (
    ("month", 30 * 24 * 60 * 60),
    ("week", 7 * 24 * 60 * 60),
    ("day", 24 * 60 * 60),
    ("hour", 60 * 60),
    ("minute", 60),
    ("second", 1),
)


def normalize_machine_code(value):
    text = str(value or "").strip().upper()
    text = re.sub(r"[^A-Z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text


def humanize_duration_seconds(total_seconds):
    """
    Convert a duration to a verbose string from months to seconds.

    Examples:
    - 500 minutes => "8 hours 20 minutes"
    - 30 seconds => "30 seconds"
    """
    try:
        seconds_value = int(
            Decimal(str(total_seconds or 0)).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )
    except (InvalidOperation, TypeError, ValueError):
        seconds_value = 0

    seconds_value = max(seconds_value, 0)
    if seconds_value <= 0:
        return "0 seconds"

    parts = []
    remaining = seconds_value
    for unit_name, unit_seconds in _DURATION_UNITS:
        if remaining < unit_seconds:
            continue
        unit_value, remaining = divmod(remaining, unit_seconds)
        label = unit_name if unit_value == 1 else f"{unit_name}s"
        parts.append(f"{unit_value} {label}")

    return " ".join(parts) if parts else "0 seconds"


def humanize_duration_minutes(total_minutes):
    try:
        total_seconds = Decimal(str(total_minutes or 0)) * Decimal("60")
    except (InvalidOperation, TypeError, ValueError):
        total_seconds = Decimal("0")
    return humanize_duration_seconds(total_seconds)


def normalize_operation_time_minutes(value, unit="min"):
    """
    Normalize a BOM operation duration value to minutes.

    The UI can collect seconds, minutes, or hours while scheduling continues to
    use minute-based setup/run fields internally.
    """
    try:
        amount = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal("0")

    unit_key = str(unit or "min").strip().lower()
    if unit_key in {"s", "sec", "second", "seconds"}:
        minutes = amount / Decimal("60")
    elif unit_key in {"h", "hr", "hour", "hours"}:
        minutes = amount * Decimal("60")
    else:
        minutes = amount

    if minutes < 0:
        minutes = Decimal("0")
    return minutes.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

def is_holiday(check_date, company):
    """
    Check if a given date is a holiday for the company.
    check_date can be datetime or date object.
    """
    if isinstance(check_date, datetime):
        check_date = check_date.date()
        
    settings, _ = SystemSettings.objects.get_or_create(company=company)
    
    # 1. Check Weekly Holidays (0=Monday, 6=Sunday)
    weekday = check_date.weekday()
    if weekday in settings.weekly_holidays:
        return True
        
    # 2. Check Specific Holiday Dates
    holiday_dates = [h['date'] for h in settings.holidays if 'date' in h]
    if check_date.strftime('%Y-%m-%d') in holiday_dates:
        return True
        
    return False

def calculate_end_date(start_date, duration_hours, company):
    """
    Calculates end_date by skipping holidays.
    Simplistic model: assumes 24h production capability, but skips whole days that are holidays.
    If a holiday occurs, the remaining duration is pushed to the next available day.
    """
    if not start_date:
        return None
        
    current_time = start_date
    remaining_hours = float(duration_hours)
    
    # If starting on a holiday, push to the first working day at the same time
    while is_holiday(current_time, company):
        current_time += timedelta(days=1)
    
    # Process duration
    while remaining_hours > 0:
        # How many hours left today? (assuming 24h model for now, unless user specifies shifts)
        # To keep it simple and match "escape friday", we check if the CURRENT day is a holiday.
        # If the duration covers multiple days, we add 24h for each holiday encountered.
        
        # Calculate tentative end
        tentative_end = current_time + timedelta(hours=remaining_hours)
        
        # Check if we cross into new days
        days_crossed = (tentative_end.date() - current_time.date()).days
        
        if days_crossed == 0:
            # Finishes today. No holidays to worry about (we already checked start_date)
            return tentative_end
        else:
            # We cross at least one midnight.
            # Check if the NEXT day is a holiday
            next_day = current_time.date() + timedelta(days=1)
            if is_holiday(next_day, company):
                # It's a holiday! Push the whole day.
                # The time remains the same, but we add 24h to the timeline.
                current_time += timedelta(days=1)
                # remaining_hours stays the same because we didn't "work" this day
            else:
                # Not a holiday. Advance to next midnight or just take one day off the duration
                # To be precise:
                hours_until_midnight = 24 - (current_time.hour + current_time.minute/60 + current_time.second/3600)
                if hours_until_midnight >= remaining_hours:
                    return tentative_end
                else:
                    remaining_hours -= hours_until_midnight
                    current_time = datetime.combine(next_day, datetime.min.time()).replace(tzinfo=current_time.tzinfo)
    
    return current_time

def validate_start_date(start_date, company):
    """
    Returns True if start_date is not a holiday.
    """
    return not is_holiday(start_date, company)
