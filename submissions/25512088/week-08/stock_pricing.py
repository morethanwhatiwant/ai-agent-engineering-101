"""Initial grade-aware prices, not learned optimal prices. No acquisition-cost floor.

Food additionally loses value over its last 24 hours. Recompute its grade from
catalog shelf life when available, because inventory snapshots may be stale.
"""
import math
import time
from decimal import Decimal, ROUND_CEILING

RATES = {'goods': {'A': 95, 'B': 80, 'C': 60},
         'food': {'A': 95, 'B': 75, 'C': 45}}


def receipt_grade(unit, catalog=None, now=None):
    """Grade at issuance; omit uncertain food claims near a transition."""
    now=time.time() if now is None else now
    product=(catalog or {}).get(unit['sku'],{})
    kind=unit.get('kind') or product.get('kind') or ('food' if unit.get('expires_at') else 'goods')
    grade=unit.get('grade')
    if grade not in ('A','B','C'):return None
    if kind=='goods':return grade
    if kind!='food':return None
    expiry=unit.get('expires_at');shelf=product.get('shelf_hours')
    if (not isinstance(expiry,(int,float)) or not math.isfinite(expiry) or
        not isinstance(shelf,(int,float)) or not math.isfinite(shelf) or shelf<=0 or expiry<=now+120):
        return None
    def at(ts):
        fraction=(expiry-ts)/(3600*shelf)
        return 'A' if fraction>=.8 else 'B' if fraction>=.45 else 'C'
    current=at(now)
    # A fresher catalog computation conflicting with the snapshot is uncertain.
    if current<grade or at(now+120)!=current:return None
    return current


def public_price(unit, catalog=None, now=None):
    now = time.time() if now is None else now
    product = (catalog or {}).get(unit['sku'], {})
    kind = unit.get('kind') or product.get('kind') or ('food' if unit.get('expires_at') else 'goods')
    grade = unit.get('grade')
    price = unit['list_price']
    if kind not in RATES or grade not in RATES[kind]:
        raise ValueError('Unknown product kind or grade: cannot quote')
    if not isinstance(price, (int, float)) or isinstance(price, bool) or not math.isfinite(price) or price <= 0:
        raise ValueError('Invalid list price')
    freshness = Decimal(1)
    if kind == 'food':
        expiry = unit.get('expires_at')
        if not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or expiry <= now:
            raise ValueError('Expired food or missing expiry: cannot quote')
        hours = (Decimal(str(expiry))-Decimal(str(now)))/3600
        shelf = product.get('shelf_hours')
        if isinstance(shelf, (int, float)) and math.isfinite(shelf) and shelf > 0:
            fraction = hours/Decimal(str(shelf))
            calculated = 'A' if fraction >= Decimal('.8') else 'B' if fraction >= Decimal('.45') else 'C'
            # Never upgrade the market's stated grade on an inferred calculation.
            grade = max(grade, calculated)
        freshness = Decimal('.2') + Decimal('.8') * min(Decimal(1), hours/24)
    amount = Decimal(str(price)) * RATES[kind][grade]/100 * freshness
    return max(1, int(amount.to_integral_value(rounding=ROUND_CEILING)))
