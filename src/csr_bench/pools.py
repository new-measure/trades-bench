from __future__ import annotations

PERSON_NAME_POOL_VERSION = "person-names-v1"
BUSINESS_NAME_POOL_VERSION = "business-names-v1"

FIRST_NAMES: tuple[str, ...] = (
    "James", "Mary", "Robert", "Patricia", "John", "Linda", "Michael", "Barbara",
    "David", "Elizabeth", "William", "Jennifer", "Richard", "Maria", "Joseph", "Susan",
    "Thomas", "Margaret", "Charles", "Dorothy", "Daniel", "Lisa", "Matthew", "Nancy",
    "Anthony", "Karen", "Mark", "Betty", "Steven", "Sandra", "Paul", "Ashley",
    "Andrew", "Kimberly", "Joshua", "Emily", "Kevin", "Donna", "Brian", "Michelle",
    "George", "Carol", "Edward", "Amanda", "Ronald",
)

LAST_NAMES: tuple[str, ...] = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas",
    "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson", "White",
    "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker", "Young",
    "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
    "Green", "Adams", "Nelson", "Baker", "Hall",
)

_BUSINESS_WORD_PRIMARY: tuple[str, ...] = (
    "Quartzvale", "Zephyrmesa", "Cinderbrook", "Palodune", "Ashcanyon", "Duskridge",
    "Verdantholt", "Sundrift", "Coppervale", "Brackenfell", "Solmirage", "Thornquill",
    "Windforge", "Mirageholt", "Emberfield", "Sagewick", "Driftstone", "Glimmerpeak",
    "Rustvale", "Hollowmere",
)

_BUSINESS_WORD_SECONDARY: tuple[str, ...] = (
    "Falconworks", "Ironvale", "Stormcrest", "Bravemark", "Trueforge", "Wayfinder",
    "Steadfast", "Clearwell", "Boldstone", "Farreach", "Swiftbind", "Corewise",
    "Nimblecraft", "Vaultline", "Primewell", "Truenorth", "Bravepoint", "Corestead",
    "Fairhaven", "Brightmoor",
)

_COMMERCIAL_SUFFIXES: tuple[str, ...] = ("LLC", "Inc.", "Group", "Enterprises", "Partners")

_APARTMENT_SUFFIXES: tuple[str, ...] = (
    "Property Management", "Residential Group", "Apartments", "Realty Group",
)

_PHONE_LINE_COUNT = 100


def _decompose(u: int, modulus: int) -> tuple[int, int]:
    return u % modulus, u // modulus


def person_name(u: int) -> tuple[str, int]:
    first_idx, u = _decompose(u, len(FIRST_NAMES))
    last_idx, u = _decompose(u, len(LAST_NAMES))
    return f"{FIRST_NAMES[first_idx]} {LAST_NAMES[last_idx]}", u


def business_name(u: int, segment: str) -> tuple[str, int]:
    word_a_idx, u = _decompose(u, len(_BUSINESS_WORD_PRIMARY))
    word_b_idx, u = _decompose(u, len(_BUSINESS_WORD_SECONDARY))
    suffixes = _COMMERCIAL_SUFFIXES if segment == "commercial" else _APARTMENT_SUFFIXES
    suffix_idx, u = _decompose(u, len(suffixes))
    name = (
        f"{_BUSINESS_WORD_PRIMARY[word_a_idx]} {_BUSINESS_WORD_SECONDARY[word_b_idx]} "
        f"{suffixes[suffix_idx]}"
    )
    return name, u


def phone_number(u: int, phone_prefixes: list[str]) -> tuple[str, int]:
    prefix_idx, u = _decompose(u, len(phone_prefixes))
    line_idx, u = _decompose(u, _PHONE_LINE_COUNT)
    raw = phone_prefixes[prefix_idx]
    area, exchange = raw[:3], raw[3:]
    return f"{area}-{exchange}-01{line_idx:02d}", u


def compose_identity(u: int, segment: str, phone_prefixes: list[str]) -> tuple[str, str]:
    if segment == "residential":
        name, u = person_name(u)
    else:
        name, u = business_name(u, segment)
    phone, u = phone_number(u, phone_prefixes)
    return name, phone


__all__ = [
    "BUSINESS_NAME_POOL_VERSION",
    "FIRST_NAMES",
    "LAST_NAMES",
    "PERSON_NAME_POOL_VERSION",
    "business_name",
    "compose_identity",
    "person_name",
    "phone_number",
]
