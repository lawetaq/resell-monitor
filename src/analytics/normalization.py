from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ProductIdentity:
    product_category: str | None
    manufacturer: str | None
    normalized_model: str | None
    variant: str | None
    capacity: str | None
    memory: str | None
    comparable_key: str | None
    match_confidence: str


BRANDS = {
    "asus": "ASUS", "gigabyte": "Gigabyte", "msi": "MSI", "palit": "Palit",
    "zotac": "Zotac", "sapphire": "Sapphire", "powercolor": "PowerColor",
    "amd": "AMD", "intel": "Intel", "nvidia": "NVIDIA", "kingston": "Kingston",
    "crucial": "Crucial", "samsung": "Samsung", "western digital": "Western Digital",
    "wd": "Western Digital", "seagate": "Seagate", "adata": "ADATA", "corsair": "Corsair",
}


def normalize_product(title: str, description: str | None = None) -> ProductIdentity:
    text = _clean(f"{title} {description or ''}")
    brand = next((value for token, value in BRANDS.items() if re.search(rf"\b{re.escape(token)}\b", text)), None)

    gpu = re.search(r"\b(rtx|gtx|rx)\s*[- ]?(\d{3,4})(?:\s*(ti|super|xt|xtx))?\b", text)
    if gpu:
        family, number, suffix = gpu.groups()
        model = f"{family.upper()} {number}" + (f" {suffix.upper()}" if suffix else "")
        memory_match = re.search(r"\b(4|6|8|10|12|16|20|24)\s*(?:gb|гб)\b", text)
        memory = f"{memory_match.group(1)}GB" if memory_match else None
        # VRAM materially changes GPU comparability. Unknown VRAM is deliberately separate.
        variant = memory or "memory-unknown"
        return _identity("gpu", brand, model, variant, None, memory, "high" if memory else "medium")

    ram_type = re.search(r"\bddr\s*([345])\b", text)
    ram_hint = ram_type or re.search(r"(?:оперативн|озу|ram\b)", text)
    if ram_hint:
        capacity_match = re.search(r"\b(4|8|16|32|64|128)\s*(?:gb|гб)\b", text)
        speed_match = re.search(r"\b(2133|2400|2666|2800|3000|3200|3600|4000|4800|5200|5600|6000|6400)\s*(?:mhz|мгц)?\b", text)
        if not (ram_type and capacity_match):
            return _unknown("ram", brand)
        model = f"DDR{ram_type.group(1)}"
        capacity = f"{capacity_match.group(1)}GB"
        speed = f"{speed_match.group(1)}MHz" if speed_match else None
        variant = " ".join(part for part in (capacity, speed) if part)
        return _identity("ram", brand, model, variant, capacity, None, "high" if speed else "medium")

    cpu = re.search(r"\b(?:core\s*)?(i[3579][ -]?\d{4,5}[a-z]{0,2})\b", text)
    ryzen = re.search(r"\b(ryzen\s*[3579]\s*[- ]?\d{4}[a-z]{0,2})\b", text)
    if cpu or ryzen:
        raw = (cpu or ryzen).group(1)
        model = re.sub(r"\s+", " ", raw.upper().replace("-", " "))
        maker = brand or ("Intel" if cpu else "AMD")
        return _identity("cpu", maker, model, None, None, None, "high")

    storage_hint = re.search(r"\b(ssd|nvme|m\.2|sata)\b", text)
    if storage_hint:
        terabyte_match = re.search(r"\b(1|2|4)\s*(?:tb|тб)\b", text)
        capacity_match = re.search(r"\b(120|128|240|250|256|480|500|512|960|1000|1024|2000|2048|4000)\s*(?:gb|гб)?\b", text)
        if not (terabyte_match or capacity_match):
            return _unknown("ssd", brand)
        amount = int((terabyte_match or capacity_match).group(1))
        capacity = f"{amount}TB" if terabyte_match else f"{amount // 1000}TB" if amount >= 1000 else f"{amount}GB"
        interface = "NVMe" if re.search(r"\b(nvme|m\.2)\b", text) else "SATA" if "sata" in text else None
        if not interface:
            return _unknown("ssd", brand)
        # Brand/model-family text is too inconsistent to guess safely; interface+capacity is conservative.
        return _identity("ssd", brand, interface, capacity, capacity, None, "medium")

    return _unknown(None, brand)


def _identity(category: str, brand: str | None, model: str, variant: str | None,
              capacity: str | None, memory: str | None, confidence: str) -> ProductIdentity:
    parts = [category, model.casefold().replace(" ", "-")]
    if variant:
        parts.append(variant.casefold().replace(" ", "-"))
    return ProductIdentity(category, brand, model, variant, capacity, memory, ":".join(parts), confidence)


def _unknown(category: str | None, brand: str | None) -> ProductIdentity:
    return ProductIdentity(category, brand, None, None, None, None, None, "insufficient")


def _clean(value: str) -> str:
    return re.sub(r"[^a-zа-яё0-9.]+", " ", value.casefold()).strip()
