#!/usr/bin/env python3
"""Entropy Volume Program: trade volume on a Hyperliquid HIP-3 market (default
Entropy io:SNDK) by opening and immediately closing small positions.

Each cycle:
  1. read the order book; skip if the spread is wider than max_spread_bps
  2. open: IOC taker order for ~size_usd (buy and sell alternate per cycle)
  3. close: reduce-only IOC for the filled size, retried with a fresh book
  4. sleep interval_sec

This is NOT an arbitrage or a money-making strategy. Every round trip pays
the taker fee twice plus about one bid/ask spread. You are buying volume.

Safety:
  - use an API wallet (agent) key: it can trade but cannot withdraw. The
    script refuses to run with a main-wallet key
  - the key is only used to sign orders locally; it is never printed, logged
    or sent anywhere (only signatures go to api.hyperliquid.xyz)
  - daily_loss_limit_usd stops new cycles for the rest of the UTC day
  - create a file named STOP next to this script to flatten and exit
  - the script refuses to start if the coin already has an open position
    (set flatten_on_start: true to close it first)

    python3 churn.py --dry-run      # read-only: prints what it would do
    python3 churn.py                # LIVE: real orders
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Optional

import yaml
from dotenv import load_dotenv

log = logging.getLogger("churn")
STOP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "STOP")
MIN_ORDER_USD = 10.0          # Hyperliquid minimum order value


@dataclass
class Config:
    coin: str = "io:SNDK"                 # dex:symbol, e.g. io:SNDK, io:DRAM
    size_usd: float = 60.0                # notional per order (one side)
    interval_sec: float = 3.0             # pause between cycles
    max_spread_bps: float = 3.0           # skip when the book is wider than this
    max_spread_ticks: int = 1             # ...or wider than this many price ticks (0 = off)
    stable_ms: int = 300                  # best bid/ask must be unchanged for this long (0 = off)
    slippage_bps: float = 5.0             # IOC price buffer past the touch
    leverage: int = 3                     # isolated leverage set at start
    daily_volume_target_usd: float = 0.0  # stop for the day once reached (0 = no target)
    daily_loss_limit_usd: float = 5.0     # stop for the day once fees + spread losses reach this
    max_position_usd: float = 200.0       # never hold more than this (a stuck close)
    flatten_on_start: bool = False        # close an existing position before starting
    status_every_sec: float = 60.0

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        names = {f.name for f in fields(cls)}
        unknown = set(raw) - names
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        cfg = cls(**raw)
        if ":" not in cfg.coin:
            raise ValueError("coin must be dex:symbol, e.g. io:SNDK")
        if cfg.size_usd < MIN_ORDER_USD * 1.1:
            raise ValueError(f"size_usd must be at least {MIN_ORDER_USD * 1.1:.0f}")
        if cfg.interval_sec < 1.0:
            raise ValueError("interval_sec must be >= 1 (exchange rate limits)")
        if not 1 <= cfg.leverage <= 10:
            raise ValueError("leverage must be 1..10")
        return cfg


# --------------------------------------------------------------- helpers

def round_down(x: float, decimals: int) -> float:
    f = 10 ** decimals
    return math.floor(x * f + 1e-9) / f


def round_px(px: float, sz_decimals: int, up: bool) -> float:
    """Hyperliquid perp prices: at most 5 significant figures and at most
    (6 - szDecimals) decimals."""
    if px <= 0:
        return px
    max_dec = max(0, 6 - sz_decimals)
    sig_dec = 4 - math.floor(math.log10(px))
    dec = max(0, min(max_dec, sig_dec))
    f = 10.0 ** dec
    v = math.ceil(px * f - 1e-9) / f if up else math.floor(px * f + 1e-9) / f
    return round(v, 8)


def tick_size(px: float, sz_decimals: int) -> float:
    """Smallest price step at this price (5 significant figures, at most
    6 - szDecimals decimals)."""
    dec = max(0, min(max(0, 6 - sz_decimals), 4 - math.floor(math.log10(px))))
    return 10.0 ** -dec


def parse_order(resp) -> tuple:
    """(filled_size, avg_px, error) from an order response."""
    try:
        if resp.get("status") != "ok":
            return 0.0, None, str(resp)[:200]
        st = resp["response"]["data"]["statuses"][0]
    except (AttributeError, KeyError, IndexError, TypeError):
        return 0.0, None, f"malformed response: {str(resp)[:200]}"
    if "filled" in st:
        f = st["filled"]
        return float(f.get("totalSz") or 0), float(f.get("avgPx") or 0) or None, None
    if "error" in st:
        msg = str(st["error"])
        if "could not immediately match" in msg.lower():
            return 0.0, None, None          # IOC found nothing: not an error
        return 0.0, None, msg
    return 0.0, None, f"unexpected status: {st}"


# --------------------------------------------------------------- the bot

class Churner:
    def __init__(self, cfg: Config, info, exchange, address: str, dry_run: bool) -> None:
        self.cfg = cfg
        self.info = info
        self.ex = exchange
        self.address = address
        self.dry = dry_run
        self.dex = cfg.coin.split(":", 1)[0]
        asset = info.name_to_asset(cfg.coin)
        self.sz_dec = info.asset_to_sz_decimals[asset]
        self.buy_first = True
        self.day = None
        self.day_volume = 0.0
        self.day_gross = 0.0          # price PnL of closed round trips (from our own fills)
        self.day_fees = 0.0           # fee estimate; replaced by exchange fills in status()
        self.fee_bps_est = 0.9
        self.start_ms = int(time.time() * 1000)
        self.paused_until_day = None
        self.last_status = 0.0

    # ---- market data
    def book(self) -> Optional[tuple]:
        b = self.info.l2_snapshot(self.cfg.coin)
        levels = b.get("levels") or [[], []]
        if not levels[0] or not levels[1]:
            return None
        return float(levels[0][0]["px"]), float(levels[1][0]["px"])

    def position(self) -> float:
        st = self.info.user_state(self.address, self.dex)
        for ap in st.get("assetPositions") or []:
            p = ap.get("position") or {}
            if p.get("coin") == self.cfg.coin:
                return float(p.get("szi") or 0)
        return 0.0

    # ---- orders
    def ioc(self, is_buy: bool, sz: float, px: float, reduce_only: bool,
            touch: Optional[float] = None) -> tuple:
        if self.dry:
            fill = touch or px      # simulate a fill at the best price, not at the buffered limit
            log.info("[dry-run] %s %s %.6g limit %.6g (would fill ~%.6g)%s", "BUY" if is_buy else "SELL",
                     self.cfg.coin, sz, px, fill, " reduce-only" if reduce_only else "")
            return sz, fill, None
        resp = self.ex.order(self.cfg.coin, is_buy, sz, px, {"limit": {"tif": "Ioc"}},
                             reduce_only=reduce_only)
        return parse_order(resp)

    def close(self, is_buy: bool, sz: float, attempts: int = 3) -> tuple:
        """Reduce-only close with a fresh book each try, widening the buffer."""
        done, value = 0.0, 0.0
        for i in range(attempts):
            bk = self.book()
            if bk is None:
                time.sleep(0.5)
                continue
            bid, ask = bk
            slip = self.cfg.slippage_bps * (1 + i) / 1e4
            px = round_px(ask * (1 + slip) if is_buy else bid * (1 - slip), self.sz_dec, up=is_buy)
            left = round_down(sz - done, self.sz_dec)
            if left <= 0:
                break
            f, apx, err = self.ioc(is_buy, left, px, reduce_only=True, touch=ask if is_buy else bid)
            if err:
                log.warning("close attempt %d: %s", i + 1, err)
            if f:
                done += f
                value += f * (apx or px)
            if sz - done < 10 ** -self.sz_dec / 2:
                break
        return done, (value / done if done else None)

    # ---- accounting
    def roll_day(self) -> None:
        d = datetime.now(timezone.utc).date()
        if d != self.day:
            if self.day is not None:
                log.info("day %s done: volume $%.0f, est. cost $%.2f", self.day,
                         self.day_volume, self.day_fees - self.day_gross)
            self.day, self.day_volume, self.day_gross, self.day_fees = d, 0.0, 0.0, 0.0

    def day_cost(self) -> float:
        return self.day_fees - self.day_gross

    def status(self) -> None:
        """Exact fees and PnL from the exchange's own fill records."""
        if self.dry:
            return
        try:
            fills = self.info.user_fills_by_time(self.address, self.start_ms)
        except Exception as e:
            log.warning("fills read failed: %r", e)
            return
        mine = [f for f in fills if f.get("coin") == self.cfg.coin]
        vol = sum(float(f["px"]) * float(f["sz"]) for f in mine)
        fees = sum(float(f["fee"]) for f in mine)
        pnl = sum(float(f["closedPnl"]) for f in mine)
        net = pnl - fees
        per_m = -net / vol * 1e6 if vol else 0.0
        if vol:
            self.fee_bps_est = fees / vol * 1e4
        log.info("[status] since start: volume $%.0f | fees $%.4f | price PnL %+.4f | net %+.4f "
                 "| cost per $1M volume ≈ $%.0f (before any rebate)", vol, fees, pnl, net, per_m)

    # ---- one cycle
    def cycle(self) -> bool:
        """Returns False when the bot should stop for today."""
        self.roll_day()
        cfg = self.cfg
        if cfg.daily_volume_target_usd and self.day_volume >= cfg.daily_volume_target_usd:
            return False
        if self.day_cost() >= cfg.daily_loss_limit_usd:
            log.warning("daily loss limit reached ($%.2f) — pausing until the next UTC day",
                        self.day_cost())
            return False
        bk = self.book()
        if bk is None:
            return True
        bid, ask = bk
        mid = (bid + ask) / 2
        spread = (ask - bid) / mid * 1e4
        if spread > cfg.max_spread_bps:
            log.debug("spread %.2f bp > %.2f — skip", spread, cfg.max_spread_bps)
            return True
        if cfg.max_spread_ticks and ask - bid > cfg.max_spread_ticks * tick_size(mid, self.sz_dec) * 1.001:
            log.debug("spread %d ticks > %d — skip", round((ask - bid) / tick_size(mid, self.sz_dec)),
                      cfg.max_spread_ticks)
            return True
        if cfg.stable_ms:
            # only trade a book that is sitting still: a moving book means the
            # close will cross a worse price than the open
            time.sleep(cfg.stable_ms / 1000)
            again = self.book()
            if again != bk:
                log.debug("book moved within %d ms — skip", cfg.stable_ms)
                return True
        sz = round_down(cfg.size_usd / mid, self.sz_dec)
        if sz * mid < MIN_ORDER_USD or sz * mid > cfg.max_position_usd:
            log.error("size %.6g ($%.0f) outside [$%.0f, max_position_usd]", sz, sz * mid, MIN_ORDER_USD)
            return False
        is_buy = self.buy_first
        self.buy_first = not self.buy_first
        slip = cfg.slippage_bps / 1e4
        px = round_px(ask * (1 + slip) if is_buy else bid * (1 - slip), self.sz_dec, up=is_buy)
        f1, p1, err = self.ioc(is_buy, sz, px, reduce_only=False, touch=ask if is_buy else bid)
        if err:
            log.warning("open failed: %s", err)
            return True
        if not f1:
            return True
        p1 = p1 or px
        f2, p2 = self.close(not is_buy, f1)
        p2 = p2 or p1
        gross = (p2 - p1) * min(f1, f2) * (1 if is_buy else -1)
        vol = f1 * p1 + f2 * p2
        self.day_volume += vol
        self.day_gross += gross
        self.day_fees += vol * self.fee_bps_est / 1e4
        log.info("%s %.6g @ %.6g -> %s %.6g @ %.6g | spread %.2f bp | round trip %+.4f | "
                 "today $%.0f volume, est. cost $%.2f", "BUY" if is_buy else "SELL", f1, p1,
                 "SELL" if is_buy else "BUY", f2, p2, spread, gross, self.day_volume, self.day_cost())
        if f1 - f2 > 10 ** -self.sz_dec / 2:
            log.error("close incomplete: %.6g of %.6g still open — stopping new cycles; "
                      "check the position by hand", f1 - f2, f1)
            return False
        return True

    def flatten(self) -> None:
        pos = 0.0 if self.dry else self.position()
        if abs(pos) > 0:
            log.warning("flattening %+.6g %s", pos, self.cfg.coin)
            self.close(pos < 0, abs(pos), attempts=5)

    def run(self) -> None:
        log.info("%s on %s | $%.0f per order, every %.1fs, max spread %.1f bp | daily loss limit "
                 "$%.2f%s", "DRY RUN" if self.dry else "LIVE", self.cfg.coin, self.cfg.size_usd,
                 self.cfg.interval_sec, self.cfg.max_spread_bps, self.cfg.daily_loss_limit_usd,
                 f" | daily target ${self.cfg.daily_volume_target_usd:,.0f}"
                 if self.cfg.daily_volume_target_usd else "")
        while True:
            if os.path.exists(STOP_FILE):
                log.warning("STOP file found — flattening and exiting")
                self.flatten()
                return
            try:
                today = datetime.now(timezone.utc).date()
                if self.paused_until_day == today:
                    time.sleep(30)
                    continue
                if not self.cycle():
                    self.paused_until_day = today
                    log.info("paused until 00:00 UTC")
                if time.time() - self.last_status >= self.cfg.status_every_sec:
                    self.last_status = time.time()
                    self.status()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("cycle failed")
                time.sleep(5)
            time.sleep(self.cfg.interval_sec)


def check_env_file(path: str) -> None:
    """Warn when .env can be read by other users on this machine."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return
    if os.name == "posix" and mode & 0o077:
        log.warning("%s is readable by other users on this machine — run: chmod 600 %s", path, path)


def check_api_wallet(info, signer: str, account: str) -> None:
    """Refuse to trade unless the key belongs to an API wallet (agent) of
    `account`. A main-wallet key can withdraw funds; an API wallet cannot."""
    if signer.lower() == account.lower():
        sys.exit("HL_API_PRIVATE_KEY is your MAIN wallet's key. Do not use it here. Create an "
                 "API wallet on app.hyperliquid.xyz -> More -> API and use that key instead.")
    try:
        r = info.post("/info", {"type": "userRole", "user": signer})
    except Exception as e:
        sys.exit(f"could not verify the API wallet ({e!r}); not starting")
    role = (r or {}).get("role")
    owner = ((r or {}).get("data") or {}).get("user", "")
    if role != "agent":
        sys.exit(f"HL_API_PRIVATE_KEY is not an API wallet (role: {role}). It may be a main "
                 "wallet key, which can withdraw funds. Use an API wallet key instead.")
    if owner.lower() != account.lower():
        sys.exit("this API wallet is not authorized for HL_ACCOUNT_ADDRESS. Check that "
                 "HL_ACCOUNT_ADDRESS is your MAIN account address.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Hyperliquid HIP-3 volume churner")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--dry-run", action="store_true", help="read-only, no orders, no keys needed")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%m-%d %H:%M:%S")
    try:
        cfg = Config.load(args.config)
    except (OSError, ValueError) as e:
        sys.exit(f"config error: {e}")
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    dex = cfg.coin.split(":", 1)[0]
    info = Info(constants.MAINNET_API_URL, skip_ws=True, perp_dexs=["", dex])
    exchange, address = None, "0x0000000000000000000000000000000000000000"
    if not args.dry_run:
        check_env_file(args.env_file)
        load_dotenv(args.env_file)
        key = (os.getenv("HL_API_PRIVATE_KEY") or "").strip()
        address = (os.getenv("HL_ACCOUNT_ADDRESS") or "").strip()
        if not key or not address:
            sys.exit("set HL_API_PRIVATE_KEY (API wallet key) and HL_ACCOUNT_ADDRESS "
                     "(your main account address) in .env")
        import eth_account
        from hyperliquid.exchange import Exchange
        try:
            wallet = eth_account.Account.from_key(key)
        except Exception:
            sys.exit("HL_API_PRIVATE_KEY is not a valid private key")
        del key
        check_api_wallet(info, wallet.address, address)
        exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=address,
                            perp_dexs=["", dex])
        r = exchange.update_leverage(cfg.leverage, cfg.coin, is_cross=False)
        if r.get("status") != "ok":
            log.warning("could not set %dx isolated leverage: %s", cfg.leverage, str(r)[:200])
    bot = Churner(cfg, info, exchange, address, args.dry_run)
    if not args.dry_run:
        pos = bot.position()
        if pos:
            if not cfg.flatten_on_start:
                sys.exit(f"{cfg.coin} already has a position ({pos:+g}). Close it first, "
                         f"or set flatten_on_start: true")
            bot.flatten()
    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("Ctrl-C — flattening")
        bot.flatten()
        bot.status()


if __name__ == "__main__":
    main()
