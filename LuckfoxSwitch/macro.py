"""Bounded, streaming HID macro format shared by host preparation and Linux replay."""
import hashlib
import io
import struct
from pathlib import Path

MAGIC = b"CSHID001"
HEADER = struct.Struct("<8sI32s")
STEP = struct.Struct("<I8s8s")  # minimum hold milliseconds, keyboard, gamepad
KEYBOARD_NEUTRAL = bytes(8)
GAMEPAD_NEUTRAL = bytes.fromhex("0000088080808000")
MAX_BYTES = 8 * 1024 * 1024
MAX_DURATION_MS = 60 * 60 * 1000


def write_macro(path, steps):
    body = b"".join(STEP.pack(*step) for step in steps)
    data = HEADER.pack(MAGIC, len(steps), hashlib.sha256(body).digest()) + body
    if len(data) > MAX_BYTES:
        raise ValueError("Macro exceeds the 8 MiB limit")
    Path(path).write_bytes(data)
    return inspect_macro(path)


def snapshot_macro(path):
    """Read a bounded immutable snapshot, which is also the exact replay input."""
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    return data, inspect_data(data)


def inspect_macro(path):
    return snapshot_macro(path)[1]


def inspect_data(data):
    """Validate all records before opening HID devices or changing USB state."""
    size = len(data)
    if size < HEADER.size or size > MAX_BYTES:
        raise ValueError("Invalid macro size")
    with io.BytesIO(data) as stream:
        header = stream.read(HEADER.size)
        magic, count, digest = HEADER.unpack(header)
        file_hash = hashlib.sha256(header)
        if magic != MAGIC or count < 2 or size != HEADER.size + count * STEP.size:
            raise ValueError("Invalid macro header or record count")
        checksum = hashlib.sha256()
        duration = 0
        for index in range(count):
            record = stream.read(STEP.size)
            checksum.update(record)
            file_hash.update(record)
            hold, keyboard, gamepad = STEP.unpack(record)
            duration += hold
            if hold > MAX_DURATION_MS or duration > MAX_DURATION_MS:
                raise ValueError("Macro exceeds one hour")
            if keyboard[1] != 0 or any(key > 0x65 for key in keyboard[2:]):
                raise ValueError("Invalid keyboard report")
            if index == count - 1:
                if (hold, keyboard, gamepad) != (0, KEYBOARD_NEUTRAL, GAMEPAD_NEUTRAL):
                    raise ValueError("Macro must end with a neutral END record")
            elif hold == 0:
                raise ValueError("Zero-duration intermediate step")
        if checksum.digest() != digest:
            raise ValueError("Macro checksum mismatch")
    return {"steps": count, "duration_ms": duration, "bytes": size,
            "sha256": file_hash.hexdigest()}


def iter_steps(path):
    """Runtime supplies validated immutable bytes; paths remain useful offline."""
    if isinstance(path, bytes):
        magic, count, _ = HEADER.unpack_from(path)
        if magic != MAGIC:
            raise ValueError("Invalid macro magic")
        for offset in range(HEADER.size, HEADER.size + count * STEP.size, STEP.size):
            yield STEP.unpack_from(path, offset)
        return
    with Path(path).open("rb") as stream:
        magic, count, _ = HEADER.unpack(stream.read(HEADER.size))
        if magic != MAGIC:
            raise ValueError("Invalid macro magic")
        for _ in range(count):
            yield STEP.unpack(stream.read(STEP.size))
