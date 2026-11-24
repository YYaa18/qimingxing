from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Dict

import websockets

# Allow running directly as `python mock_senses/simulator.py` from repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from protocol.models import (  # noqa: E402
    ClientCapabilities,
    ContextGameStatePayload,
    ContextOcrSnippetPayload,
    ContextWindowChangedPayload,
    SystemHelloPayload,
    SystemPingPayload,
    make_envelope,
    now_ms,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("qimingxing.mock_senses")

PRESETS: Dict[str, dict] = {
    "elden_ring": {
        "window": ContextWindowChangedPayload(
            app_name="ELDEN RING",
            process_name="eldenring.exe",
            window_title="ELDEN RING™",
            scene="aaa_game",
        ),
        "game_state": ContextGameStatePayload(
            game_id="elden_ring",
            provider="mock_save_parser",
            summary_version=12,
            data={
                "location": "Redmane Castle",
                "level": 68,
                "stats": {"vigor": 32, "dexterity": 35, "endurance": 24},
                "equipped": ["Bloodhound's Fang +6", "Radahn Soldier Armor"],
                "flasks": {"crimson": 8, "cerulean": 2},
            },
        ),
    },
    "office": {
        "window": ContextWindowChangedPayload(
            app_name="VS Code",
            process_name="Code.exe",
            window_title="qimingxing - Visual Studio Code",
            scene="coding_office",
        ),
        "ocr": ContextOcrSnippetPayload(
            text="ValidationError: 1 validation error for SystemHelloPayload",
            region="terminal_bottom",
            confidence=0.96,
        ),
    },
}


async def send_preset(ws: websockets.WebSocketClientProtocol, preset_name: str, version: int) -> None:
    preset = PRESETS[preset_name]
    if "window" in preset:
        env = make_envelope("senses", "context.window_changed", preset["window"])
        await ws.send(env.model_dump_json())
        logger.info("Sent context.window_changed: %s", preset["window"].app_name)
    if "game_state" in preset:
        gs: ContextGameStatePayload = preset["game_state"].model_copy(
            update={"summary_version": version}
        )
        env = make_envelope("senses", "context.game_state", gs)
        await ws.send(env.model_dump_json())
        logger.info("Sent context.game_state: game_id=%s ver=%d", gs.game_id, gs.summary_version)
    if "ocr" in preset:
        env = make_envelope("senses", "context.ocr_snippet", preset["ocr"])
        await ws.send(env.model_dump_json())
        logger.info("Sent context.ocr_snippet: region=%s", preset["ocr"].region)


async def heartbeat_loop(ws: websockets.WebSocketClientProtocol) -> None:
    while True:
        await asyncio.sleep(15)
        ping = make_envelope("senses", "system.ping", SystemPingPayload(client_ts=now_ms()))
        await ws.send(ping.model_dump_json())


async def interactive_loop(
    ws: websockets.WebSocketClientProtocol,
    initial_preset: str,
) -> None:
    version = 1
    await send_preset(ws, initial_preset, version)
    logger.info(
        "Interactive commands: [1] elden_ring  [2] office  [g <level> <location>] custom game_state  [q] quit"
    )
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            await asyncio.sleep(1)
            continue
        cmd = line.strip()
        if cmd == "1":
            version += 1
            await send_preset(ws, "elden_ring", version)
        elif cmd == "2":
            version += 1
            await send_preset(ws, "office", version)
        elif cmd.startswith("g "):
            parts = cmd.split(maxsplit=2)
            level = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 70
            location = parts[2] if len(parts) > 2 else "Caelid"
            version += 1
            gs = ContextGameStatePayload(
                game_id="elden_ring",
                provider="mock_senses_cli",
                summary_version=version,
                data={"level": level, "location": location, "equipped": ["Uchigatana +12"]},
            )
            await ws.send(make_envelope("senses", "context.game_state", gs).model_dump_json())
            logger.info("Sent custom game_state: level=%d location=%s", level, location)
        elif cmd.lower() in ("q", "quit", "exit"):
            raise KeyboardInterrupt


async def run_client(ws_url: str, preset: str, once: bool, interval: float) -> None:
    attempt = 0
    version = 1
    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                attempt = 0
                hello = make_envelope(
                    "senses",
                    "system.hello",
                    SystemHelloPayload(
                        client_name="mock-senses-cli",
                        role="senses",
                        capabilities=ClientCapabilities(
                            avatar_mode="none",
                            supported_emotions=[],
                            streaming_text=False,
                            ui_cards=[],
                        ),
                    ),
                )
                await ws.send(hello.model_dump_json())
                welcome_raw = await ws.recv()
                logger.info("Connected to Brain: %s", json.loads(welcome_raw).get("type"))

                if once:
                    await send_preset(ws, preset, version)
                    return

                hb = asyncio.create_task(heartbeat_loop(ws))
                try:
                    if interval > 0:
                        while True:
                            await send_preset(ws, preset, version)
                            version += 1
                            await asyncio.sleep(interval)
                    else:
                        await interactive_loop(ws, preset)
                finally:
                    hb.cancel()
        except KeyboardInterrupt:
            logger.info("Exiting Mock Senses.")
            return
        except Exception as exc:
            attempt += 1
            delay = min(1.0 * (2 ** (attempt - 1)), 5.0)
            logger.warning("Disconnected (%s). Reconnecting in %.1fs...", exc, delay)
            await asyncio.sleep(delay)


def main() -> None:
    parser = argparse.ArgumentParser(description="Qimingxing Mock Senses WebSocket Client")
    parser.add_argument("--url", default="ws://127.0.0.1:18790/ws", help="Brain WebSocket URL")
    parser.add_argument(
        "--preset",
        choices=list(PRESETS.keys()),
        default="elden_ring",
        help="Initial context preset to push",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Push preset once and exit",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.0,
        help="Push preset periodically every N seconds (0 = interactive stdin mode)",
    )
    args = parser.parse_args()
    try:
        asyncio.run(run_client(args.url, args.preset, args.once, args.interval))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
