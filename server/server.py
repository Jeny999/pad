"""Pad — отдельный сервер виртуального Xbox-геймпада.

Телефон открывает страницу в браузере и шлёт состояние стиков/кнопок по
WebSocket. Сервер переводит его в Xbox 360-контроллер через ViGEmBus.
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import io
import json
import logging
import secrets
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

def _page_dir() -> Path:
    """index.html: рядом с исходником или внутри onefile-распаковки."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _data_dir() -> Path:
    """code.txt всегда рядом с exe/скриптом, а не во временной папке."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _data_dir()
PAGE_DIR = _page_dir()
CODE_PATH = ROOT / "code.txt"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LOG = logging.getLogger("pad")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def load_code() -> str:
    try:
        code = CODE_PATH.read_text(encoding="utf-8").strip().upper()
    except FileNotFoundError:
        code = ""
    if not code:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        CODE_PATH.write_text(code + "\n", encoding="utf-8")
    return code


CODE = load_code()
app = FastAPI(title="Pad")
# APK открывает страницу с origin http://tauri.localhost, поэтому проверка
# доступности через fetch('/ping') требует CORS.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_lock = threading.RLock()
_vg: Any = None
_pad: Any = None
_pad_error = ""
_clients = 0
_screen_clients = 0
_screen_backend = ""
_screen_fps = 0.0
_dxcam_lock = threading.Lock()
_buttons = 0
_dpad = 0
_last: dict[str, Any] = {}
SCREEN_FPS = 30
SCREEN_WIDTH = 1280
SCREEN_QUALITY = 72
_rumble_lock = threading.Lock()
_rumble = {"large": 0, "small": 0, "seq": 0}


def on_rumble(client, target, large_motor, small_motor, led_number, user_data):
    """Игра попросила вибрацию — запоминаем для отправки телефону."""
    try:
        large = max(0, min(255, int(large_motor)))
        small = max(0, min(255, int(small_motor)))
    except (TypeError, ValueError):
        return
    with _rumble_lock:
        _rumble["large"] = large
        _rumble["small"] = small
        _rumble["seq"] += 1

BUTTON_BITS = (
    (1, "XUSB_GAMEPAD_A"), (2, "XUSB_GAMEPAD_B"),
    (4, "XUSB_GAMEPAD_X"), (8, "XUSB_GAMEPAD_Y"),
    (16, "XUSB_GAMEPAD_LEFT_SHOULDER"), (32, "XUSB_GAMEPAD_RIGHT_SHOULDER"),
    (64, "XUSB_GAMEPAD_BACK"), (128, "XUSB_GAMEPAD_START"),
    (256, "XUSB_GAMEPAD_LEFT_THUMB"), (512, "XUSB_GAMEPAD_RIGHT_THUMB"),
)
DPAD_BITS = (
    (1, "XUSB_GAMEPAD_DPAD_UP"), (2, "XUSB_GAMEPAD_DPAD_DOWN"),
    (4, "XUSB_GAMEPAD_DPAD_LEFT"), (8, "XUSB_GAMEPAD_DPAD_RIGHT"),
)


def get_pad() -> Any:
    global _vg, _pad, _pad_error
    with _lock:
        if _pad is not None:
            return _pad
        try:
            import vgamepad as vg
            _vg = vg
            _pad = vg.VX360Gamepad()
            try:
                _pad.register_notification(on_rumble)
            except Exception:
                LOG.exception("Не удалось включить вибро-уведомления")
            _pad_error = ""
            LOG.info("Виртуальный Xbox 360-контроллер создан")
        except Exception as exc:
            _pad_error = f"ViGEmBus/vgamepad недоступен: {exc}"
            LOG.exception("Не удалось создать виртуальный геймпад")
            return None
        return _pad


def axis(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(-1.0, min(1.0, number))


def trigger(value: Any) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return int(max(0.0, min(1.0, number)) * 255)


def apply_state(state: dict[str, Any]) -> None:
    global _buttons, _dpad, _last
    pad = get_pad()
    if pad is None:
        return
    with _lock:
        pad.left_joystick(int(axis(state.get("lx")) * 32767), int(-axis(state.get("ly")) * 32767))
        pad.right_joystick(int(axis(state.get("rx")) * 32767), int(-axis(state.get("ry")) * 32767))
        pad.left_trigger(trigger(state.get("lt")))
        pad.right_trigger(trigger(state.get("rt")))
        buttons = int(state.get("b") or 0) & 0x3FF
        for bit, name in BUTTON_BITS:
            if buttons & bit and not _buttons & bit:
                pad.press_button(getattr(_vg.XUSB_BUTTON, name))
            elif not buttons & bit and _buttons & bit:
                pad.release_button(getattr(_vg.XUSB_BUTTON, name))
        _buttons = buttons
        dpad = int(state.get("d") or 0) & 0xF
        for bit, name in DPAD_BITS:
            if dpad & bit and not _dpad & bit:
                pad.press_button(getattr(_vg.XUSB_BUTTON, name))
            elif not dpad & bit and _dpad & bit:
                pad.release_button(getattr(_vg.XUSB_BUTTON, name))
        _dpad = dpad
        pad.update()
        _last = {key: state.get(key) for key in ("lx", "ly", "rx", "ry", "lt", "rt", "b", "d")}


def release_all() -> None:
    global _buttons, _dpad
    pad = get_pad()
    if pad is None:
        return
    with _lock:
        for bit, name in BUTTON_BITS + DPAD_BITS:
            if (_buttons | _dpad) & bit:
                pad.release_button(getattr(_vg.XUSB_BUTTON, name))
        _buttons = 0
        _dpad = 0
        pad.left_joystick(0, 0)
        pad.right_joystick(0, 0)
        pad.left_trigger(0)
        pad.right_trigger(0)
        pad.update()


@app.get("/")
def index():
    page = PAGE_DIR / "index.html"
    if not page.is_file():
        raise HTTPException(status_code=404, detail="index.html не найден")
    return FileResponse(str(page), headers={"Cache-Control": "no-store"})


@app.get("/ping")
def ping():
    return {"ok": True, "service": "pad"}


@app.get("/status")
def status():
    return {"ok": _pad is not None, "error": _pad_error, "clients": _clients,
            "screen_clients": _screen_clients, "screen_backend": _screen_backend,
            "screen_fps": round(_screen_fps, 1),
            "buttons": _buttons, "dpad": _dpad, "state": _last}


@app.websocket("/screen/ws")
async def screen_socket(websocket: WebSocket):
    """Экран ПК: кадры JPEG идут только пока телефон держит просмотр открытым."""
    global _screen_clients
    await websocket.accept()
    supplied = str(websocket.query_params.get("key") or "").strip().upper()
    if not supplied or not hmac.compare_digest(supplied.encode("utf-8", "ignore"),
                                               CODE.encode("utf-8", "ignore")):
        await websocket.send_json({"type": "error", "message": "Неверный код доступа"})
        await websocket.close(code=4401)
        return

    def int_param(name: str, default: int, low: int, high: int) -> int:
        try:
            value = int(websocket.query_params.get(name, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    width = int_param("w", SCREEN_WIDTH, 480, 1920)
    quality = int_param("q", SCREEN_QUALITY, 30, 90)
    fps = int_param("fps", SCREEN_FPS, 5, 120)

    loop = asyncio.get_running_loop()
    frames: asyncio.Queue = asyncio.Queue(maxsize=3)
    stop = threading.Event()

    def offer(frame: bytes) -> None:
        if frames.full():
            try:
                frames.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            frames.put_nowait(frame)
        except asyncio.QueueFull:
            pass

    def worker() -> None:
        global _screen_backend, _screen_fps
        camera = None
        sct = None
        try:
            use_cv2 = False
            cv2 = None
            np = None
            try:
                import cv2 as cv2_module
                import numpy as numpy_module
                cv2 = cv2_module
                np = numpy_module
                use_cv2 = True
            except Exception:
                LOG.warning("OpenCV недоступен, экран будет медленнее")

            if use_cv2:
                import dxcam
                for attempt in range(3):
                    try:
                        with _dxcam_lock:
                            candidate = dxcam.create(output_color="BGR")
                            if candidate is not None:
                                candidate.start(target_fps=min(120, max(30, fps)), video_mode=True)
                                camera = candidate
                                _screen_backend = "dxcam"
                                break
                    except Exception:
                        LOG.warning("DXGI-захват: попытка %s не удалась", attempt + 1,
                                    exc_info=attempt == 2)
                    time.sleep(0.25)
                if camera is None:
                    LOG.warning("DXGI-захват недоступен, использую mss")
            if camera is None:
                import mss
                factory = getattr(mss, "MSS", None) or mss.mss
                sct = factory()
                _screen_backend = "mss"
            LOG.info("Экран: захват через %s", _screen_backend)

            last_frame = None
            interval = 1.0 / fps
            stamps: list[float] = []
            while not stop.is_set():
                started = time.perf_counter()
                if camera is not None:
                    frame = camera.get_latest_frame()
                    if frame is not None:
                        last_frame = frame
                    else:
                        frame = last_frame
                    if frame is None:
                        time.sleep(0.005)
                        continue
                    source = frame
                elif use_cv2:
                    shot = sct.grab(sct.monitors[1])
                    source = np.array(shot)[:, :, :3]
                else:
                    from PIL import Image
                    shot = sct.grab(sct.monitors[1])
                    source = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

                if use_cv2:
                    source_height, source_width = source.shape[0], source.shape[1]
                    target = min(width, source_width)
                    height = max(1, round(source_height * target / source_width))
                    small = cv2.resize(source, (target, height), interpolation=cv2.INTER_AREA)
                    ok, buffer = cv2.imencode(".jpg", small,
                                              [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                    if ok:
                        loop.call_soon_threadsafe(offer, buffer.tobytes())
                else:
                    target = min(width, source.width)
                    height = max(1, round(source.height * target / source.width))
                    image = source.resize((target, height), Image.BILINEAR)
                    buffer = io.BytesIO()
                    image.save(buffer, format="JPEG", quality=quality, optimize=False)
                    loop.call_soon_threadsafe(offer, buffer.getvalue())

                now = time.perf_counter()
                stamps.append(now)
                while stamps and now - stamps[0] > 1.0:
                    stamps.pop(0)
                _screen_fps = float(len(stamps))
                elapsed = now - started
                time.sleep(max(0.0, interval - elapsed))
        except Exception as exc:
            LOG.warning("Захват экрана остановлен: %s", exc)
            loop.call_soon_threadsafe(offer, ("error", f"Захват экрана недоступен: {exc}"))
        finally:
            if camera is not None:
                with _dxcam_lock:
                    try:
                        camera.stop()
                    except Exception:
                        pass
                    time.sleep(0.15)
            if sct is not None:
                try:
                    sct.close()
                except Exception:
                    pass

    thread = threading.Thread(target=worker, name="screen-capture", daemon=True)
    thread.start()
    _screen_clients += 1
    LOG.info("Экран: просмотр открыт (%s клиентов, %spx, %s fps, q%s)",
             _screen_clients, width, fps, quality)
    try:
        while True:
            frame = await frames.get()
            if isinstance(frame, tuple):
                await websocket.send_json({"type": "error", "message": frame[1]})
                break
            await websocket.send_bytes(frame)
    except WebSocketDisconnect:
        pass
    except Exception:
        LOG.debug("Экран: соединение оборвалось", exc_info=True)
    finally:
        stop.set()
        thread.join(timeout=1)
        _screen_clients = max(0, _screen_clients - 1)
        LOG.info("Экран: просмотр закрыт (%s клиентов)", _screen_clients)


async def push_rumble(websocket: WebSocket, send_lock: asyncio.Lock) -> None:
    last_seq = -1
    try:
        while True:
            await asyncio.sleep(0.05)
            with _rumble_lock:
                seq = int(_rumble["seq"])
                large = int(_rumble["large"])
                small = int(_rumble["small"])
            if seq == last_seq:
                continue
            last_seq = seq
            async with send_lock:
                await websocket.send_json({"type": "rumble", "large": large, "small": small})
    except asyncio.CancelledError:
        raise
    except Exception:
        return


@app.websocket("/ws")
async def pad_socket(websocket: WebSocket):
    global _clients
    await websocket.accept()
    supplied = str(websocket.query_params.get("key") or "").strip().upper()
    if not supplied or not hmac.compare_digest(supplied.encode("utf-8", "ignore"),
                                               CODE.encode("utf-8", "ignore")):
        await websocket.send_json({"type": "error", "message": "Неверный код доступа"})
        await websocket.close(code=4401)
        return
    if get_pad() is None:
        await websocket.send_json({"type": "error", "message": _pad_error or "vgamepad недоступен"})
        await websocket.close(code=1011)
        return
    _clients += 1
    LOG.info("Телефон подключён (%s клиентов)", _clients)
    send_lock = asyncio.Lock()
    rumble_task = asyncio.create_task(push_rumble(websocket, send_lock))
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("type") == "ping":
                async with send_lock:
                    await websocket.send_json({"type": "pong", "ts": data.get("ts"),
                                               "clients": _clients})
                continue
            apply_state(data)
    except WebSocketDisconnect:
        pass
    except Exception:
        LOG.debug("Соединение оборвалось", exc_info=True)
    finally:
        rumble_task.cancel()
        _clients = max(0, _clients - 1)
        if _clients == 0:
            release_all()
        LOG.info("Телефон отключён (%s клиентов)", _clients)


def lan_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Pad server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    ip = lan_ip()
    print()
    print("  PAD", flush=True)
    print(f"  ПК:      http://127.0.0.1:{args.port}", flush=True)
    print(f"  Телефон: http://{ip}:{args.port}", flush=True)
    print(f"  Код:     {CODE}", flush=True)
    print("  Остановить: Ctrl+C", flush=True)
    print(flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
