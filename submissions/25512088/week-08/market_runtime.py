"""Local kit adapter: role-isolated forum outboxes and buyer research worker."""
import importlib.util
from pathlib import Path
import sys
import threading
import time
from market_intel import Intel
from buyer_publicist import queue_update


def start_research(state, market):
    def loop():
        intel=Intel(state)
        while True:
            try:
                intel.refresh(market)
                queue_update(intel)
            except Exception as exc:
                print('[research]',type(exc).__name__,flush=True)
            time.sleep(30)
    threading.Thread(target=loop,daemon=True).start()


def dispatch_outbox(original, market, md):
    if 'shop' in market.token:original(market,md)
    if 'buyer' in market.token:original(market,md/'buyer-forum')


def main():
    root=Path(__file__).resolve().parents[3]
    spec=importlib.util.spec_from_file_location('course_market_kit',root/'scripts/market_kit.py')
    kit=importlib.util.module_from_spec(spec);spec.loader.exec_module(kit)
    original=kit.outbox
    def outbox(market,md):
        # Buyer must never take a shop post and silently publish it as buyer.
        dispatch_outbox(original,market,md)
    kit.outbox=outbox
    argv=sys.argv[1:]
    if 'join' in argv and '--shop-only' not in argv:
        state=Path(argv[argv.index('--dir')+1] if '--dir' in argv else '.')/'.market'
        market=argv[argv.index('--market')+1]
        start_research(state.resolve(),market.rstrip('/'))
    kit.main()

if __name__=='__main__':main()
