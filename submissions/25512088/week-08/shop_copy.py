"""Evidence-first copy: no unimplemented warranty or invented customer endorsement."""
import math
import time

SHOP_DESCRIPTION = (
    '식품·생활용품을 실제 보유 재고에서 판매합니다. 상품명 또는 SKU로 문의하면 '
    '등급과 현재 가격을 안내합니다. 시장 인증서는 결제 단계에서 확인할 수 있습니다. '
    '식품은 남은 유통기한 확인 가능, 비식품은 보증을 제공하지 않습니다. '
    '견적 금액을 숫자로 보내 구매하세요. 창립단골 5% 할인 조건은 PROGRAM. '
    'Ask by product name or SKU; send the quoted amount to buy. No goods warranty.'
)


def price_line(unit, quote, now=None):
    now=time.time() if now is None else now
    price=quote['price'];listed=unit['list_price']
    discount=math.floor((listed-price)*100/listed)
    saving=f' · 정가 대비 {discount}%↓' if discount>0 else ''
    expiry=unit.get('expires_at')
    condition=f' · 남은 기한 {max(0,math.floor((expiry-now)/3600))}h' if expiry else ' · 보증 없음'
    return f"{unit['name'][:28]} [{unit['sku']}] | {unit['grade']} | {price:,}원{saving}{condition}"


def stock_ad(name, offers, now=None):
    lines=[f'[같은 운영자 가게 {name} · 현재 재고]']
    lines.extend(price_line(u,q,now) for u,q in offers[:2])
    lines.append('SKU로 문의 → 견적 확인 후 숫자로 구매. 단골 5% 조건: PROGRAM. 가격·재고는 문의 시 재확인.')
    return '\n'.join(lines)


def paid_post(name, sku, price, grade, listed, honest, ledger):
    discount=math.floor((listed-price)*100/listed) if listed else 0
    saving=f' · 정가 대비 {discount}%↓' if discount>0 else ''
    quality={True:'등급 주장 일치',False:'등급 주장 불일치'}.get(honest,'등급 주장 확인 불가')
    return (f'[실제 구매 기록] {name[:40]} [{sku}]\n'
            f'{price:,}원{saving} · 결제 당시 {grade}등급 · {quality}\n'
            f'정산 #{ledger}. 이후 고장 여부는 별도 확인 필요.')
