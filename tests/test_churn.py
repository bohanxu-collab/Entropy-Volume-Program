"""Round-trip logic against a fake exchange (no network, no keys)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from churn import (Churner, Config, check_api_wallet, parse_order, round_down,  # noqa: E402
                   round_px, tick_size)


class FakeInfo:
    def __init__(self, bid=1500.0, ask=1500.1):
        self.bid, self.ask = bid, ask
        self.asset_to_sz_decimals = {200002: 3}
        self.pos = 0.0

    def name_to_asset(self, name):
        return 200002

    def l2_snapshot(self, coin):
        return {"levels": [[{"px": str(self.bid), "sz": "10"}], [{"px": str(self.ask), "sz": "10"}]]}

    def user_state(self, address, dex):
        return {"assetPositions": [{"position": {"coin": "io:SNDK", "szi": str(self.pos)}}] if self.pos else []}

    def user_fills_by_time(self, address, start):
        return []


class FakeExchange:
    def __init__(self, info, fill_close=True):
        self.info, self.orders, self.fill_close = info, [], fill_close

    def order(self, coin, is_buy, sz, px, order_type, reduce_only=False):
        self.orders.append((is_buy, sz, px, reduce_only))
        if reduce_only and not self.fill_close:
            return {"status": "ok", "response": {"data": {"statuses": [
                {"error": "Order could not immediately match against any resting orders."}]}}}
        fill_px = self.info.ask if is_buy else self.info.bid
        return {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"totalSz": str(sz), "avgPx": str(fill_px), "oid": 1}}]}}}


def make(**kw):
    cfg = Config(**{"size_usd": 60, "interval_sec": 1, "stable_ms": 0, **kw})
    info = FakeInfo()
    ex = FakeExchange(info)
    return Churner(cfg, info, ex, "0xabc", dry_run=False), info, ex


def test_round_trip_alternates_and_counts_volume():
    bot, info, ex = make()
    assert bot.cycle() and bot.cycle()
    sides = [(o[0], o[3]) for o in ex.orders]
    assert sides == [(True, False), (False, True), (False, False), (True, True)]
    assert 230 < bot.day_volume < 250            # 4 fills of ~$60
    assert bot.day_gross < 0                     # paid the spread twice


def test_skips_wide_spread_and_respects_daily_loss_limit():
    bot, info, ex = make(max_spread_bps=1.0)
    info.ask = 1501.0                            # ~6.7 bp spread
    assert bot.cycle() and ex.orders == []
    bot2, info2, ex2 = make(daily_loss_limit_usd=0.01)
    bot2.cycle()
    assert bot2.cycle() is False                 # cost of the first round trip is over 1 cent


def test_incomplete_close_stops_new_cycles():
    bot, info, ex = make()
    ex.fill_close = False
    assert bot.cycle() is False
    assert sum(1 for o in ex.orders if o[3]) == 3   # three reduce-only retries


def test_helpers_and_config_validation():
    assert round_down(0.03999, 3) == 0.039
    assert round_px(1500.123, 3, up=True) == 1500.2 and round_px(1500.123, 3, up=False) == 1500.1
    assert parse_order({"status": "ok", "response": {"data": {"statuses": [
        {"error": "Order could not immediately match"}]}}}) == (0.0, None, None)
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write("coin: SNDK\n")
    f.close()
    try:
        Config.load(f.name)
        assert False
    except ValueError:
        pass


def test_one_tick_filter_and_stability_check():
    assert abs(tick_size(1500.0, 3) - 0.1) < 1e-12 and abs(tick_size(60.0, 1) - 0.001) < 1e-12
    bot, info, ex = make(max_spread_ticks=1)
    info.ask = 1500.2                            # two ticks wide: skip
    assert bot.cycle() and ex.orders == []
    info.ask = 1500.1                            # one tick: trade
    assert bot.cycle() and len(ex.orders) == 2

    class Moving(FakeInfo):
        calls = 0
        def l2_snapshot(self, coin):
            Moving.calls += 1
            px = self.bid + (0.1 if Moving.calls % 2 == 0 else 0.0)
            return {"levels": [[{"px": str(px), "sz": "10"}], [{"px": str(px + 0.1), "sz": "10"}]]}
    info2 = Moving()
    ex2 = FakeExchange(info2)
    bot2 = Churner(Config(size_usd=60, interval_sec=1, stable_ms=1), info2, ex2, "0xabc", dry_run=False)
    assert bot2.cycle() and ex2.orders == []     # book moved between the two looks: skip


class _RoleInfo:
    def __init__(self, resp):
        self.resp = resp

    def post(self, path, payload):
        return self.resp


def _exits(fn):
    try:
        fn()
    except SystemExit:
        return True
    return False


def test_refuses_main_wallet_key():
    main = "0x" + "a" * 40
    ok = {"role": "agent", "data": {"user": main}}
    assert _exits(lambda: check_api_wallet(_RoleInfo(ok), main, main))
    assert _exits(lambda: check_api_wallet(_RoleInfo({"role": "user"}), "0x" + "b" * 40, main))
    other = {"role": "agent", "data": {"user": "0x" + "c" * 40}}
    assert _exits(lambda: check_api_wallet(_RoleInfo(other), "0x" + "b" * 40, main))
    assert not _exits(lambda: check_api_wallet(_RoleInfo(ok), "0x" + "b" * 40, main))
