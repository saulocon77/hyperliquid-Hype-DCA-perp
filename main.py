"""
Bot Hyperliquid (HYPE perp, margen aislado, 10x) — versión simple.

Al arrancar: una compra a mercado de POSITION_USD (nocional, default 30 USDC).
Take profit: cuando el PnL no realizado >= TAKE_PROFIT_PCT * valor nocional de la posición
(default 25% del tamaño de la posición en USDC). Sin stop loss. Al cumplir el TP cierra
y apaga el proceso (exit 0).

Compras defensivas (promediar / defender): se monitorea liquidationPx de la API. Si el
precio de mercado (mid) cae de forma que mark <= liquidationPx + LIQUIDATION_BUFFER_USD
(default 0.10 en unidades de precio del perp, equivalente práctico a USDT/USDC), se
ejecuta de inmediato una compra a mercado del mismo nocional. Tras cada compra defensiva,
el precio debe superar (liquidación+buffer)*(1+REARM_PCT) antes de poder volver a armar
(evita ráfagas).

Riesgos: acercarse a la liquidación aumenta el riesgo de pérdida. Un buffer fijo en
precio (LIQUIDATION_BUFFER_USD) no representa el mismo margen relativo al precio de HYPE
según cotice el activo; conviene revisar y ajustar ese valor en el entorno según tu
tolerancia y el contexto de mercado.

Variables de entorno (Railway):
  PRIVATE_KEY               — API wallet (0x...)
  ACCOUNT_ADDRESS           — wallet principal (0x...)
  COIN                      — default HYPE
  POSITION_USD              — nocional por compra en USDC (default 30)
  LEVERAGE                  — default 10
  TAKE_PROFIT_PCT           — fracción sobre el nocional (default 0.25 = 25%)
  LIQUIDATION_BUFFER_USD    — distancia máxima por encima de liquidationPx (default 0.10)
  REARM_PCT                 — tras defensa, mark debe superar umbral*(1+REARM_PCT) (default 0.01)
  POLL_INTERVAL_SEC         — default 2.0
  TESTNET                   — default false
  SLIPPAGE                  — default 0.05
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


def parse_liquidation_px(pos: dict[str, Any]) -> Optional[float]:
    raw = pos.get("liquidationPx")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def main() -> None:
    private_key = _require_env("PRIVATE_KEY")
    account = _require_env("ACCOUNT_ADDRESS")
    coin = os.environ.get("COIN", "HYPE").strip()
    leverage = int(_env_float("LEVERAGE", 10))
    position_usd = _env_float("POSITION_USD", 30.0)
    tp_pct = _env_float("TAKE_PROFIT_PCT", 0.25)
    liq_buffer = _env_float("LIQUIDATION_BUFFER_USD", 0.10)
    rearm_pct = _env_float("REARM_PCT", 0.01)
    poll_sec = _env_float("POLL_INTERVAL_SEC", 2.0)
    slippage = _env_float("SLIPPAGE", 0.05)
    testnet = _env_bool("TESTNET", False)

    base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL

    wallet: LocalAccount = eth_account.Account.from_key(private_key)
    exchange = Exchange(wallet, base_url, account_address=account)
    info = exchange.info

    log.info(
        "Configurando %s | %sx aislado | compra %.2f USDC nocional | TP %.0f%% nocional | "
        "defensa si mark <= liq + %.2f | rearm tras defensa %.1f%%",
        coin,
        leverage,
        position_usd,
        tp_pct * 100,
        liq_buffer,
        rearm_pct * 100,
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

            pos_value = float(pos.get("positionValue", 0) or 0)
            pnl = float(pos.get("unrealizedPnl", 0) or 0)

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

            liq = parse_liquidation_px(pos)
            defense_threshold: Optional[float] = None
            if liq is not None:
                defense_threshold = liq + liq_buffer
            else:
                log.warning("liquidationPx no disponible; sin compra defensiva este ciclo.")

            if rearm_floor is not None and mark > rearm_floor:
                floor = rearm_floor
                armed = True
                rearm_floor = None
                log.info("Rearmado defensa: mark %.6f > %.6f", mark, floor)

            if armed and defense_threshold is not None and mark <= defense_threshold:
                T = defense_threshold
                sz = usd_to_size(info, coin, position_usd, mark)
                if sz <= 0:
                    log.warning("Tamaño 0 en compra defensiva; omitiendo.")
                else:
                    log.info(
                        "Compra defensiva: mark %.6f <= liq+%.4f (umbral %.6f, liq=%.6f) | sz=%s",
                        mark,
                        liq_buffer,
                        defense_threshold,
                        liq,
                        sz,
                    )
                    exchange.market_open(coin, True, sz, slippage=slippage)
                    armed = False
                    rearm_floor = T * (1.0 + rearm_pct)

            log.info(
                "mark=%.6f | liq=%s | umbral_def=%s | PnL=%.4f | nocional=%.2f | armed=%s",
                mark,
                f"{liq:.6f}" if liq is not None else "n/a",
                f"{defense_threshold:.6f}" if defense_threshold is not None else "n/a",
                pnl,
                abs(pos_value),
                armed,
            )

        except Exception as e:
            log.exception("Error: %s", e)

        time.sleep(poll_sec)


if __name__ == "__main__":
    main()
