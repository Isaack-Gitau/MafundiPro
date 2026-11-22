import re

_KENYAN_MOBILE = re.compile(r"\+254[17][0-9]{8}")


def normalize_kenyan_mobile(value: str) -> str:
    """Normalize a Kenyan mobile number to +254XXXXXXXXX or raise ValueError."""
    if not isinstance(value, str):
        raise ValueError("Enter a valid Kenyan mobile phone number.")

    cleaned = re.sub(r"[\s-]", "", value)
    if cleaned.startswith("0"):
        cleaned = "+254" + cleaned[1:]
    elif cleaned.startswith("254"):
        cleaned = "+" + cleaned

    if not _KENYAN_MOBILE.fullmatch(cleaned):
        raise ValueError("Enter a valid Kenyan mobile phone number.")

    return cleaned
