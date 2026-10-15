"""Unit strings to base units (ARCHITECTURE §4.2 stage 11).

Spellings are matched after verify.normalize, so Cyrillic and Latin spellings
of the same unit share one key. Base units:
  W (power), mm (length), kg (mass), GB (data), month (time), day (time),
  celsius (temperature), UZS (currency), percent, piece (count).

Decisions:
- Data uses decimal prefixes (1 GB = 1000 MB). GiB/MiB/TiB are listed with
  binary factors, so "16 GiB" is 17.18 GB and is never silently equated with 16 GB.
- 1 year = 12 months. Days are a different base and are never converted to months.
- "г" alone is not a unit (it means gram or year in Russian), and neither are
  "мвт"/"MW" (megawatt and milliwatt collide after lower-casing). Both are unknown,
  so a comparison involving them is not comparable.
- Temperature is affine (Fahrenheit -> Celsius), so the spec carries an offset.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.verify import normalize


@dataclass(frozen=True)
class UnitSpec:
    base: str
    factor: float = 1.0
    offset: float = 0.0  # base = value * factor + offset (only temperature uses an offset)


_INCH_SIGNS = frozenset({'"', "″", "”"})
_INCH = UnitSpec("mm", 25.4)

_RAW: dict[str, UnitSpec] = {}


def _add(spec: UnitSpec, *names: str) -> None:
    for name in names:
        _RAW[name] = spec


_add(UnitSpec("W"), "W", "Вт", "Vt", "vt")
_add(UnitSpec("W", 1000.0), "kW", "кВт", "kVt", "kvt")

_add(UnitSpec("mm"), "mm", "мм")
_add(UnitSpec("mm", 10.0), "cm", "см")
_add(UnitSpec("mm", 1000.0), "m", "м")
_add(UnitSpec("mm", 1_000_000.0), "km", "км")
_add(_INCH, "дюйм", "дюйма", "дюймов", "inch", "inches")

_add(UnitSpec("kg"), "kg", "кг")
_add(UnitSpec("kg", 0.001), "g", "gr", "гр")
_add(UnitSpec("kg", 1000.0), "t", "т", "тонна", "тонн", "тонны", "tonna")

_add(UnitSpec("GB"), "GB", "ГБ", "gb", "гб")
_add(UnitSpec("GB", 1000.0), "TB", "ТБ", "tb", "тб")
_add(UnitSpec("GB", 0.001), "MB", "МБ", "mb", "мб")
_add(UnitSpec("GB", 0.000001), "KB", "КБ", "kb", "кб")
_add(UnitSpec("GB", 1.073741824), "GiB", "ГиБ", "gib", "гиб")
_add(UnitSpec("GB", 1099.511627776), "TiB", "ТиБ", "tib", "тиб")
_add(UnitSpec("GB", 0.001048576), "MiB", "МиБ", "mib", "миб")

_add(UnitSpec("month"), "мес", "месяц", "месяца", "месяцев", "oy")
_add(UnitSpec("month", 12.0), "год", "года", "лет", "year", "years", "yil")
_add(UnitSpec("day"), "день", "дня", "дней", "дн", "day", "days", "kun")

_add(UnitSpec("celsius"), "°C", "°С", "℃")
_add(UnitSpec("celsius", 5.0 / 9.0, -160.0 / 9.0), "°F", "℉")

_add(UnitSpec("percent"), "%", "процент", "процентов", "foiz")

_add(UnitSpec("UZS"), "сум", "сўм", "so'm", "uzs")
_add(UnitSpec("UZS", 1e6), "млн сум", "млн сўм", "млн so'm", "mln so'm", "mln uzs", "млн uzs")
_add(UnitSpec("UZS", 1e9), "млрд сум", "млрд сўм", "млрд so'm", "mlrd so'm", "mlrd uzs", "млрд uzs")
_add(UnitSpec("UZS", 1e3), "ming so'm", "тыс сум", "тыс сўм", "тыс so'm")

_add(UnitSpec("piece"), "шт", "штук", "dona", "pcs", "pc", "piece", "pieces")


def _key(name: str) -> str:
    return " ".join(normalize(name)[0].replace(".", " ").split())


_TABLE: dict[str, UnitSpec] = {}
for _name, _spec in _RAW.items():
    _TABLE.setdefault(_key(_name), _spec)


def lookup(unit: str) -> UnitSpec | None:
    """Spec for a unit string, or None if the unit is unknown."""
    raw = unit.strip()
    if raw in _INCH_SIGNS:
        return _INCH
    key = _key(raw)
    return _TABLE.get(key) if key else None


def convert(value: float, unit: str) -> tuple[float, str] | None:
    """Value in `unit` as (value in base unit, base unit name), or None if unknown."""
    spec = lookup(unit)
    if spec is None:
        return None
    return value * spec.factor + spec.offset, spec.base


__all__ = ["UnitSpec", "convert", "lookup"]
