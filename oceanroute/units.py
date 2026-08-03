"""Explicit input length units; engineering calculations always use metres."""


def length_factor(unit="m"):
    factors = {"m": 1.0, "metre": 1.0, "meter": 1.0, "ft": .3048, "feet": .3048,
               "fathom": 1.8288, "fm": 1.8288, "km": 1000.0}
    value = str(unit).strip().lower()
    if value not in factors:
        raise ValueError("长度单位须为m、km、ft或fathom；不能按数值猜测单位")
    return factors[value]
