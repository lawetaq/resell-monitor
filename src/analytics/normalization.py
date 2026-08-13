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
    ram_type: str | None = None
    ram_module_type: str | None = None
    ecc: bool | None = None
    frequency_mhz: int | None = None
    gpu_model: str | None = None
    vram_gb: int | None = None
    cpu_model: str | None = None
    socket: str | None = None
    chipset: str | None = None
    ssd_capacity: str | None = None
    ssd_interface: str | None = None
    module_count: int | None = None
    module_capacity_gb: int | None = None
    total_capacity_gb: int | None = None
    multi_item: bool = False
    price_ambiguous: bool = False


BRANDS = {
    "asus": "ASUS", "gigabyte": "Gigabyte", "msi": "MSI", "palit": "Palit",
    "zotac": "Zotac", "sapphire": "Sapphire", "powercolor": "PowerColor",
    "amd": "AMD", "intel": "Intel", "nvidia": "NVIDIA", "kingston": "Kingston",
    "crucial": "Crucial", "samsung": "Samsung", "western digital": "Western Digital",
    "wd": "Western Digital", "seagate": "Seagate", "adata": "ADATA", "corsair": "Corsair",
    "foxline": "Foxline", "palit": "Palit",
}


def normalize_product(title: str, description: str | None = None) -> ProductIdentity:
    raw_text = f"{title} {description or ''}".casefold()
    text = _clean(raw_text)
    brand = next((value for token, value in BRANDS.items() if re.search(rf"\b{re.escape(token)}\b", text)), None)

    gpu_signal = re.search(r"(?:видеокарт|video\s*(?:card|adapter)|geforce|radeon|\b(?:rtx|gtx|rx)\s*\d)", text)
    gpu_matches = list(re.finditer(r"\b(rtx|gtx|rx)\s*[- ]?(\d{3,4})(?:\s*(ti|super|xt|xtx))?\b", text))
    legacy_gpu = re.search(r"\b(r7\s*[- ]?\d{3}|(?:radeon\s*)?hd\s*[- ]?\d{4})\b", text)
    gpu = gpu_matches[0] if gpu_matches else None
    if gpu or legacy_gpu or gpu_signal:
        if gpu:
            family, number, suffix = gpu.groups()
            model = f"{family.upper()} {number}" + (f" {suffix.upper()}" if suffix else "")
        elif legacy_gpu:
            model = re.sub(r"\s+", " ", legacy_gpu.group(1).upper().replace("-", " "))
        else:
            return ProductIdentity("gpu", brand, None, None, None, None, None,
                                   "insufficient")
        memory_match = re.search(r"\b(2|4|6|8|10|12|16|20|24)\s*(?:gb|гб)\b", text)
        memory = f"{memory_match.group(1)}GB" if memory_match else None
        # VRAM materially changes GPU comparability. Unknown VRAM is deliberately separate.
        variant = memory or "memory-unknown"
        multi = len({match.group(0) for match in gpu_matches}) > 1
        return _identity("gpu", brand, model, variant, None, memory,
                         "low" if multi else "high" if memory else "medium",
                         gpu_model=model, vram_gb=int(memory_match.group(1)) if memory_match else None,
                         multi_item=multi, price_ambiguous=multi)

    ram_type = re.search(r"\bddr\s*([2345])\b", text)
    ram_hint = ram_type or re.search(r"(?:оперативн|озу|ram\b)", text)
    if ram_hint:
        capacities = re.findall(r"\b(4|8|16|32|64|128)\s*(?:gb|гб)\b", text)
        slash_capacities = re.search(r"\b(?:4|8|16|32)(?:\s*/\s*(?:4|8|16|32)){1,3}\s*(?:gb|гб)\b", raw_text)
        kit = _ram_kit(raw_text)
        capacity_match = re.search(r"\b(4|8|16|32|64|128)\s*(?:gb|гб)\b", text)
        total_capacity = kit[2] if kit else int(capacity_match.group(1)) if capacity_match else None
        speed_match = re.search(r"\b(2133|2400|2666|2800|3000|3200|3600|4000|4800|5200|5600|6000|6400)\s*(?:mhz|мгц)?\b", text)
        module = ("LRDIMM" if "lrdimm" in text else "RDIMM" if re.search(r"\b(rdim|rdimm|reg|registered|серверн)", text)
                  else "SODIMM" if re.search(r"(?:\bso\s*dimm\b|для\s+ноутбука|ноутбучн(?:ая|ой)\s+памят|памят\S*\s+для\s+ноутбука|laptop\s+memory|notebook\s+memory)", text)
                  else "UDIMM" if "udimm" in text else "unknown")
        ecc = False if re.search(r"\bnon[ -]?ecc\b", text) else True if re.search(r"\becc\b", text) else None
        if not (ram_type and total_capacity):
            return ProductIdentity("ram", brand, f"DDR{ram_type.group(1)}" if ram_type else None,
                                   None, f"{total_capacity}GB" if total_capacity else None,
                                   None, None, "low" if total_capacity or module != "unknown" else "insufficient",
                                   ram_type=f"DDR{ram_type.group(1)}" if ram_type else None,
                                   ram_module_type=module, ecc=ecc,
                                   frequency_mhz=int(speed_match.group(1)) if speed_match else None,
                                   module_count=kit[0] if kit else None,
                                   module_capacity_gb=kit[1] if kit else None,
                                   total_capacity_gb=total_capacity)
        model = f"DDR{ram_type.group(1)}"
        capacity = f"{total_capacity}GB"
        speed = f"{speed_match.group(1)}MHz" if speed_match else None
        multi = bool(slash_capacities or (len(set(capacities)) > 1 and not kit))
        variant = " ".join(part for part in (
            capacity, speed, module,
            "ecc" if ecc is True else "ecc-unknown" if ecc is None else "non-ecc"
        ) if part)
        return _identity("ram", brand, model, variant, capacity, None,
                         "low" if multi else "high" if module != "unknown" and ecc is not None else "medium",
                         ram_type=model, ram_module_type=module, ecc=ecc,
                         frequency_mhz=int(speed_match.group(1)) if speed_match else None,
                         module_count=kit[0] if kit else None,
                         module_capacity_gb=kit[1] if kit else None,
                         total_capacity_gb=total_capacity,
                         multi_item=multi, price_ambiguous=multi)

    cpu = re.search(r"\b(?:core\s*)?(i[3579][ -]?\d{4,5}[a-z]{0,2})\b", text)
    ryzen = re.search(r"\b(ryzen\s*[3579]\s*[- ]?\d{4}[a-z]{0,2})\b", text)
    if cpu or ryzen:
        raw = (cpu or ryzen).group(1)
        model = re.sub(r"\s+", " ", raw.upper().replace("-", " "))
        maker = brand or ("Intel" if cpu else "AMD")
        return _identity("cpu", maker, model, None, None, None, "high", cpu_model=model)

    chipset_match = re.search(r"\b(a520|b450|b550|x570|b560|b660|b760)\b", text)
    socket_match = re.search(r"\b(am4|am5|lga\s*1200|lga\s*1700)\b", text)
    if chipset_match or socket_match or re.search(r"(материнск|motherboard)", text):
        chipset = chipset_match.group(1).upper() if chipset_match else None
        socket = socket_match.group(1).upper().replace(" ", "") if socket_match else None
        if not (chipset or socket):
            return _unknown("motherboard", brand)
        model = " ".join(value for value in (socket, chipset) if value)
        return _identity("motherboard", brand, model, None, None, None,
                         "high" if chipset and socket else "medium", socket=socket, chipset=chipset)

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
        return _identity("ssd", brand, interface, capacity, capacity, None, "medium",
                         ssd_capacity=capacity, ssd_interface=interface)

    return _unknown(None, brand)


def _identity(category: str, brand: str | None, model: str, variant: str | None,
              capacity: str | None, memory: str | None, confidence: str, **details: object) -> ProductIdentity:
    parts = [category, model.casefold().replace(" ", "-")]
    if variant:
        parts.append(variant.casefold().replace(" ", "-"))
    return ProductIdentity(category, brand, model, variant, capacity, memory, ":".join(parts), confidence,
                           **details)  # type: ignore[arg-type]


def _unknown(category: str | None, brand: str | None) -> ProductIdentity:
    return ProductIdentity(category, brand, None, None, None, None, None, "insufficient")


def _clean(value: str) -> str:
    return re.sub(r"[^a-zа-яё0-9.]+", " ", value.casefold()).strip()


def _ram_kit(value: str) -> tuple[int, int, int] | None:
    normalized = value.casefold().replace("×", "x").replace("х", "x").replace("*", "x")
    multiplied = re.search(r"\b([2-8])\s*x\s*(4|8|16|32|64)\s*(?:gb|гб)\b", normalized)
    if multiplied:
        count, each = map(int, multiplied.groups())
        return count, each, count * each
    reversed_pair = re.search(r"\b(4|8|16|32|64)\s*x\s*2\s*(?:gb|гб)?\b", normalized)
    if reversed_pair and re.search(r"(?:ddr|оперативн|озу|ram|памят)", normalized):
        each = int(reversed_pair.group(1))
        return 2, each, each * 2
    added = re.search(r"\b(4|8|16|32|64)\s*\+\s*\1\s*(?:gb|гб)\b", normalized)
    if added:
        each = int(added.group(1))
        return 2, each, each * 2
    total_sticks = re.search(r"\b(8|16|32|64)\s*(?:gb|гб)\s*(?:/|,)?\s*2\s*(?:плашк|модул|sticks?)", normalized)
    if total_sticks:
        total = int(total_sticks.group(1))
        if total % 2 == 0:
            return 2, total // 2, total
    return None
