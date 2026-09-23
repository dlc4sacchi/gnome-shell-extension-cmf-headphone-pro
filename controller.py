#!/usr/bin/env python3
"""CMF Headphone Pro Bluetooth controller. Prints one JSON result per invocation."""

import fcntl
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path


CHANNEL = 28
ADDRESS_NAME = "CMF Headphone Pro"
LOCK_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "cmf-headphone-pro.lock"
RING_TOKEN_PATH = LOCK_PATH.with_suffix(".ring")
READ_COMMANDS = {
    "battery": 0xC007, "ring": 0xC002, "standby": 0xC011,
    "anc": 0xC01E, "eq": 0xC050, "custom_eq": 0xC044,
    "low_lag": 0xC041, "spatial": 0xC04F, "gestures": 0xC018,
    "dual_connection": 0xC027, "personal_sound": 0xC05A,
}
QUICK_KEYS = ("battery", "anc", "eq", "low_lag", "spatial", "personal_sound")
GESTURES = {
    "button_single": (6, 10, 1), "button_hold": (6, 10, 7),
    "slider": (6, 5, 1), "roller_hold": (6, 1, 7),
}
GESTURE_ACTIONS = {
    "button_single": {1, 10, 11, 20, 21, 22, 27, 29},
    "button_hold": {1, 10, 11, 20, 21, 22, 27, 29},
    "slider": {35, 36}, "roller_hold": {1, 10, 20, 21, 22},
}


def crc16(data):
    value = 0xFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0xA001 if value & 1 else 0)
    return value


class Headset:
    def __init__(self, address):
        self.socket = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        self.socket.settimeout(3)
        self.socket.connect((address, CHANNEL))
        self.buffer = b""
        self.sequence = 0

    def close(self):
        self.socket.close()

    def send(self, command, payload=b""):
        self.sequence = (self.sequence + 1) % 256
        body = b"\x55\x60\x01" + struct.pack("<HHB", command, len(payload), self.sequence) + payload
        self.socket.sendall(body + struct.pack("<H", crc16(body)))
        return self.sequence

    def query(self, command):
        sequence = self.send(command)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            while len(self.buffer) >= 10:
                start = self.buffer.find(b"\x55\x60\x01")
                if start < 0:
                    self.buffer = self.buffer[-2:]
                    break
                self.buffer = self.buffer[start:]
                if len(self.buffer) < 10:
                    break
                length = struct.unpack_from("<H", self.buffer, 5)[0]
                if length > 2048:
                    self.buffer = self.buffer[1:]
                    continue
                size = length + 10
                if len(self.buffer) < size:
                    break
                frame, self.buffer = self.buffer[:size], self.buffer[size:]
                if crc16(frame[:-2]) != struct.unpack_from("<H", frame, size - 2)[0]:
                    continue
                reply = struct.unpack_from("<H", frame, 3)[0]
                if reply == command & 0x7FFF and frame[7] == sequence:
                    return frame[8:-2]
            self.socket.settimeout(max(.1, deadline - time.monotonic()))
            self.buffer += self.socket.recv(4096)
        raise TimeoutError(f"No response to {command:04x}")

    def write(self, command, payload):
        self.send(command, payload)
        time.sleep(.12)


def connected_address():
    result = subprocess.run(
        ["bluetoothctl", "devices", "Connected"], capture_output=True, text=True,
        timeout=5, check=True,
    )
    for line in result.stdout.splitlines():
        match = re.fullmatch(r"Device ([0-9A-Fa-f:]{17}) (.+)", line)
        if match and ADDRESS_NAME in match.group(2):
            return match.group(1)
    raise ConnectionError("CMF Headphone Pro is disconnected")


def decode_gestures(payload):
    found = {}
    if payload:
        for offset in range(1, min(len(payload), 1 + payload[0] * 4), 4):
            slot = tuple(payload[offset:offset + 3])
            for name, code in GESTURES.items():
                if slot == code and offset + 3 < len(payload):
                    found[name] = payload[offset + 3]
    return found


def read_state(headset, quick=False, include=()):
    keys = list(dict.fromkeys((*QUICK_KEYS, *include))) if quick else READ_COMMANDS
    raw, errors = {}, {}
    for key in keys:
        try:
            raw[key] = headset.query(READ_COMMANDS[key])
        except (OSError, TimeoutError, ValueError) as error:
            errors[key] = str(error)
    if not raw:
        raise ConnectionError("Headphones did not respond")
    state = {"connected": True, "errors": errors}
    if payload := raw.get("battery"):
        state["battery"] = payload[-1]
    if payload := raw.get("anc"):
        mode = payload[1] if len(payload) > 1 else None
        level = payload[4] if len(payload) > 4 else None
        state["anc"] = {"mode": mode, "level": level}
    if payload := raw.get("eq"):
        state["eq"] = payload[0]
    if payload := raw.get("low_lag"):
        state["low_lag"] = payload[0] == 1
    if payload := raw.get("spatial"):
        state["spatial"] = payload[0]
    if payload := raw.get("personal_sound"):
        state["personal_sound"] = payload[0] == 1
    if payload := raw.get("dual_connection"):
        state["dual_connection"] = payload[0] == 1
    if payload := raw.get("standby"):
        if len(payload) >= 3 and payload[0] == 1:
            state["standby"] = struct.unpack_from("<H", payload, 1)[0]
    if payload := raw.get("ring"):
        if len(payload) >= 3 and payload[1] == 6:
            state["ring"] = payload[2] == 1
    if payload := raw.get("gestures"):
        state["gestures"] = decode_gestures(payload)
    if payload := raw.get("custom_eq"):
        if len(payload) >= 36:
            state["custom_eq"] = {
                "bass": round(struct.unpack_from("<f", payload, 32)[0], 2),
                "mid": round(struct.unpack_from("<f", payload, 6)[0], 2),
                "treble": round(struct.unpack_from("<f", payload, 19)[0], 2),
            }
    return state


def confirmed(state, name, value):
    actual = state.get(name)
    if name == "anc":
        return state.get("anc", {}).get("mode") == value
    if name in GESTURES:
        return state.get("gestures", {}).get(name) == value
    if name == "custom_eq":
        actual = state.get("custom_eq")
        return (state.get("eq") == 6 and isinstance(actual, dict)
                and all(key in actual and abs(actual[key] - value[key]) < .11 for key in value))
    return actual == value


def write_setting(headset, name, value):
    if name == "anc" and type(value) is int and value in {1, 2, 3, 4, 5, 7}:
        headset.write(0xF00F, bytes([1, value, 0]))
    elif name == "spatial" and type(value) is int and value in {0, 2, 3}:
        headset.write(0xF052, bytes([value, 0]))
    elif name == "eq" and type(value) is int and value in {1, 2, 3, 4, 5, 6}:
        headset.write(0xF01D, bytes([value, 0]))
    elif name == "low_lag" and type(value) is bool:
        headset.write(0xF040, bytes([1 if value else 2]))
    elif name == "personal_sound" and type(value) is bool:
        headset.write(0xF05C, bytes([1 if value else 0]))
    elif name == "dual_connection" and type(value) is bool:
        headset.write(0xF01A, bytes([1 if value else 0]))
    elif name == "standby" and type(value) is int and value in {30, 60, 120, 180, 240}:
        headset.write(0xF00B, b"\x01" + struct.pack("<H", value))
    elif name == "ring" and type(value) is bool:
        headset.write(0xF002, bytes([6, 1 if value else 0]))
    elif name in GESTURES and type(value) is int and value in GESTURE_ACTIONS[name]:
        headset.write(0xF003, bytes([1, *GESTURES[name], value]))
    elif name == "custom_eq" and isinstance(value, dict) and set(value) == {"bass", "mid", "treble"}:
        gains = [value[key] for key in ("bass", "mid", "treble")]
        if any(type(gain) not in {int, float} or not -6 <= gain <= 6 for gain in gains):
            raise ValueError("Custom EQ levels must be between -6 and +6")
        original = headset.query(READ_COMMANDS["custom_eq"])
        if len(original) < 53:
            raise ValueError("Headset returned an incomplete EQ curve")
        payload = bytearray(original[:53])
        for key, offset in (("bass", 32), ("mid", 6), ("treble", 19)):
            struct.pack_into("<f", payload, offset, float(value[key]))
        struct.pack_into("<f", payload, 1, -max(0, *gains))
        headset.write(0xF041, bytes(payload))
        headset.write(0xF01D, b"\x06\x00")
    else:
        raise ValueError("Unsupported setting or value")


def main(argv):
    if len(argv) == 2 and argv[0] == "stop-ring-after":
        time.sleep(10)
        with LOCK_PATH.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not RING_TOKEN_PATH.exists() or RING_TOKEN_PATH.read_text() != argv[1]:
                return {"stopped": False}
            headset = Headset(connected_address())
            try:
                write_setting(headset, "ring", False)
            finally:
                headset.close()
                RING_TOKEN_PATH.unlink(missing_ok=True)
        return {"stopped": True}
    if argv == ["read"]:
        action, name, value = "read", None, None
    elif argv == ["quick"]:
        action, name, value = "quick", None, None
    elif len(argv) == 3 and argv[0] == "set":
        action, name, value = "set", argv[1], json.loads(argv[2])
    else:
        raise ValueError("Usage: controller.py read|quick|set NAME JSON_VALUE")

    with LOCK_PATH.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        headset = Headset(connected_address())
        try:
            if action == "set":
                write_setting(headset, name, value)
            confirmation_key = "gestures" if name in GESTURES else name
            state = read_state(headset, quick=action != "read",
                               include=(confirmation_key,) if confirmation_key else ())
            if action == "set" and not confirmed(state, name, value):
                raise RuntimeError(f"Could not confirm {name} from the headphones")
            if name == "ring":
                if value:
                    token = str(uuid.uuid4())
                    RING_TOKEN_PATH.write_text(token)
                    subprocess.Popen(
                        [sys.executable, __file__, "stop-ring-after", token],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True,
                    )
                else:
                    RING_TOKEN_PATH.unlink(missing_ok=True)
            return state
        finally:
            headset.close()


if __name__ == "__main__":
    try:
        print(json.dumps(main(sys.argv[1:])))
    except Exception as error:
        print(json.dumps({"connected": False, "error": str(error)}))
        raise SystemExit(1)
