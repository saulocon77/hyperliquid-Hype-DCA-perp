"""
Bot Hyperliquid (HYPE perp, margen aislado, 10x) — versión simple.

Al arrancar: una compra a mercado de POSITION_USD (nocional, default 30 USDC).
Take profit: cuando el PnL no realizado >= TAKE_PROFIT_PCT * valor nocional de la posición
(default 25% del tamaño de la posición en USDC). Sin stop loss. Al cumplir el TP cierra
y apaga el proceso (exit 0).

Compras adicionales: cada vez que el precio de marca cae un DIP_PCT (default 9%) por
debajo del precio medio de entrada (entryPx de la API), se repite una compra del mismo
tamaño. Tras cada compra por caída, hace falta que el precio suba de nuevo por encima
del umbral de rearmado antes de poder disparar otra (evita compras en ráfaga).

Variables de entorno (Railway):
  PRIVATE_KEY          — API wallet (0x...)
  ACCOUNT_ADDRESS      — wallet principal (0x...)
  COIN                 — default HYPE
  POSITION_USD         — nocional por compra en USDC (default 30)
  LEVERAGE             — default 10
  TAKE_PROFIT_PCT      — fracción sobre el nocional (default 0.25 = 25%)
  DIP_PCT              — fracción de caída desde el precio medio (default 0.09 = 9%)
  REARM_PCT            — tras compra en caída, el precio debe superar dip_trigger*(1+REARM_PCT) (default 0.01)
  POLL_INTERVAL_SEC    — default 2.0
  TESTNET              — default false
  SLIPPAGE             — default 0.05
"""

from __future__ import annotations

import logging
import math
import os
import sys
import time
from typing import Any, Optional

import eth_account
from eth_account.signers.local import LocalAccount
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("hype_bot")


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return float(v)


def _require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v or not v.strip():
        log.error("Falta la variable de entorno obligatoria: %s", name)
        sys.exit(1)
    return v.strip()


def find_position(state: dict[str, Any], coin: str) -> Optional[dict[str, Any]]:
    for ap in state.get("assetPositions", []):
        pos = ap.get("position") or {}
        if pos.get("coin") == coin:
            szi = float(pos.get("szi", 0) or 0)
            if abs(szi) > 1e-12:
                return pos
    return None


def get_mid_price(info: Info, coin: str) -> float:
    mids = info.all_mids()
    if coin in mids:
        return float(mids[coin])
    return float(mids[info.name_to_coin[coin]])


def assert_long_only(pos: dict[str, Any], coin: str) -> None:
    szi = float(pos.get("szi", 0) or 0)
    if szi < 0:
        log.error("Hay posición corta en %s; este bot solo opera long.", coin)
        sys.exit(1)


def round_size(info: Info, coin: str, sz: float) -> float:
    asset = info.name_to_asset(coin)
    d = info.asset_to_sz_decimals[asset]
    factor = 10**d
    return math.floor(sz * factor + 1e-12) / factor


def usd_to_size(info: Info, coin: str, usd_notional: float, mark: float) -> float:
    if mark <= 0:
        raise ValueError("mark inválido")
    return round_size(info, coin, usd_notional / mark)


def main() -> None:
    private_key = _require_env("PRIVATE_KEY")
    account = _require_env("ACCOUNT_ADDRESS")
    coin = os.environ.get("COIN", "HYPE").strip()
    leverage = int(_env_float("LEVERAGE", 10))
    position_usd = _env_float("POSITION_USD", 30.0)
    tp_pct = _env_float("TAKE_PROFIT_PCT", 0.25)
    dip_pct = _env_float("DIP_PCT", 0.09)
    rearm_pct = _env_float("REARM_PCT", 0.01)
    poll_sec = _env_float("POLL_INTERVAL_SEC", 2.0)
    slippage = _env_float("SLIPPAGE", 0.05)
    testnet = _env_bool("TESTNET", False)

    base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL

    wallet: LocalAccount = eth_account.Account.from_key(private_key)
    exchange = Exchange(wallet, base_url, account_address=account)
    info = exchange.info

    log.info(
        "Configurando %s | %sx aislado | compra %.2f USDC nocional | TP %.0f%% del nocional | dip -%.0f%%",
        coin,
        leverage,
        position_usd,
        tp_pct * 100,
        dip_pct * 100,
    )
    exchange.update_leverage(leverage, coin, is_cross=False)

    state0 = info.user_state(account)
    pos0 = find_position(state0, coin)
    mark0 = get_mid_price(info, coin)

    if pos0:
        log.info("Ya hay posición en %s; no se hace compra inicial.", coin)
    else:
        sz0 = usd_to_size(info, coin, position_usd, mark0)
        if sz0 <= 0:
            log.error("Tamaño de orden 0; abortando.")
            sys.exit(1)
        log.info("Activación: compra inicial a mercado | sz=%s | mid≈%s", sz0, mark0)
        exchange.market_open(coin, True, sz0, slippage=slippage)

    armed = True
    rearm_floor: Optional[float] = None

    while True:
        try:
            state = info.user_state(account)
            pos = find_position(state, coin)
            mark = get_mid_price(info, coin)

            if not pos:
                log.warning("Sin posición en %s; el bot se detiene.", coin)
                sys.exit(0)

            assert_long_only(pos, coin)

            entry_px = pos.get("entryPx")
            if entry_px is None or str(entry_px).strip() == "":
                log.warning("entryPx no disponible; reintentando…")
                time.sleep(poll_sec)
                continue

            avg_entry = float(entry_px)
            pos_value = float(pos.get("positionValue", 0) or 0)
            pnl = float(pos.get("unrealizedPnl", 0) or 0)

            # TP: 25% de ganancia sobre el nocional (tamaño) de la posición
            tp_threshold = tp_pct * abs(pos_value)
            if pnl >= tp_threshold:
                log.info(
                    "Take profit: PnL %.4f >= %.4f (%.0f%% del nocional %.2f) — cerrando y apagando bot",
                    pnl,
                    tp_threshold,
                    tp_pct * 100,
                    abs(pos_value),
                )
                exchange.market_close(coin, slippage=slippage)
                log.info("Bot desactivado tras TP (exit 0).")
                sys.exit(0)

            dip_trigger = avg_entry * (1.0 - dip_pct)

            if rearm_floor is not None and mark > rearm_floor:
                floor = rearm_floor
                armed = True
                rearm_floor = None
                log.info("Rearmado dip: mark %.6f > %.6f", mark, floor)

            if armed and mark <= dip_trigger:
                prev_avg = avg_entry
                sz = usd_to_size(info, coin, position_usd, mark)
                if sz <= 0:
                    log.warning("Tamaño 0 en compra por caída; omitiendo.")
                else:
                    log.info(
                        "Compra por caída: mark %.6f <= medio*%.4f (dip %.6f) | sz=%s | medio=%.6f",
                        mark,
                        1.0 - dip_pct,
                        dip_trigger,
                        sz,
                        avg_entry,
                    )
                    exchange.market_open(coin, True, sz, slippage=slippage)
                    armed = False
                    rearm_floor = prev_avg * (1.0 - dip_pct) * (1.0 + rearm_pct)

            log.info(
                "mark=%.6f | medio=%.6f | dip_trigger=%.6f | PnL=%.4f | nocional=%.2f | armed=%s",
                mark,
                avg_entry,
                dip_trigger,
                pnl,
                abs(pos_value),
                armed,
            )

        except Exception as e:
            log.exception("Error: %s", e)

        time.sleep(poll_sec)


if __name__ == "__main__":
    main()
