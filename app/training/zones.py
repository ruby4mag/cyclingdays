"""FTP-based power zones.

Seven-zone model after Coggan (Allen & Coggan, *Training and Racing with a
Power Meter*). Boundaries are fractions of FTP:

=====  ==================  ==============
Zone   Name                % FTP
=====  ==================  ==============
Z1     Active recovery     < 55
Z2     Endurance           55 - 75
Z3     Tempo               75 - 90
Z4     Lactate threshold   90 - 105
Z5     VO2max              105 - 120
Z6     Anaerobic capacity  120 - 150
Z7     Neuromuscular       ≥ 150
=====  ==================  ==============

Convention: each zone is the half-open interval ``[lower, upper)``. A power
exactly on a boundary belongs to the higher zone. Watt bounds are rounded to
1 microwatt to remove floating-point noise (0.55 x 175 = 96.25 W exactly).
"""

from __future__ import annotations

from dataclasses import dataclass

FTP_MIN_WATTS = 1
FTP_MAX_WATTS = 600  # same bound as the athlete_profile CHECK constraint

# (zone number, name, lower fraction, upper fraction or None = open-ended)
ZONE_DEFINITIONS: tuple[tuple[int, str, float, float | None], ...] = (
    (1, "Active recovery", 0.0, 0.55),
    (2, "Endurance", 0.55, 0.75),
    (3, "Tempo", 0.75, 0.90),
    (4, "Lactate threshold", 0.90, 1.05),
    (5, "VO2max", 1.05, 1.20),
    (6, "Anaerobic capacity", 1.20, 1.50),
    (7, "Neuromuscular", 1.50, None),
)


class InvalidFTPError(ValueError):
    """FTP missing or outside the plausible range."""


@dataclass(frozen=True)
class PowerZone:
    number: int
    name: str
    lower_fraction: float
    upper_fraction: float | None
    lower_watts: float
    upper_watts: float | None  # exclusive; None = no upper bound

    def contains(self, watts: float) -> bool:
        return watts >= self.lower_watts and (self.upper_watts is None or watts < self.upper_watts)


def validate_ftp(ftp_watts: float | None) -> float:
    """Return FTP as float or raise. Never guess a missing FTP."""
    if ftp_watts is None:
        raise InvalidFTPError("FTP is unknown")
    if isinstance(ftp_watts, bool) or not FTP_MIN_WATTS <= ftp_watts <= FTP_MAX_WATTS:
        raise InvalidFTPError(f"FTP {ftp_watts!r} W outside {FTP_MIN_WATTS}-{FTP_MAX_WATTS} W")
    return float(ftp_watts)


def _watts(fraction: float, ftp: float) -> float:
    # Rounded to 1 microwatt so 0.55 x 175 is 96.25, not 96.25000000000001;
    # otherwise a power exactly on a boundary could fall into the lower zone.
    return round(fraction * ftp, 6)


def power_zones(ftp_watts: float | None) -> list[PowerZone]:
    ftp = validate_ftp(ftp_watts)
    return [
        PowerZone(
            number=number,
            name=name,
            lower_fraction=lower,
            upper_fraction=upper,
            lower_watts=_watts(lower, ftp),
            upper_watts=None if upper is None else _watts(upper, ftp),
        )
        for number, name, lower, upper in ZONE_DEFINITIONS
    ]


def zone_number(watts: float, ftp_watts: float | None) -> int:
    """Zone (1-7) for a power value. Negative power is invalid."""
    if watts < 0:
        raise ValueError(f"negative power {watts} W")
    # Compare in watts exactly like PowerZone.contains, so zone tables and
    # sample classification can never disagree on a boundary.
    for zone in power_zones(ftp_watts):
        if zone.contains(watts):
            return zone.number
    raise AssertionError("zone table does not cover all non-negative values")  # pragma: no cover
