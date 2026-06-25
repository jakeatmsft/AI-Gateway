from __future__ import annotations

import io
import base64
import asyncio
import contextlib
import os
import threading
import warnings
import wave
from typing import Any, Callable, Awaitable

import numpy as np
import pyaudio
import sounddevice as sd

with warnings.catch_warnings():
    warnings.filterwarnings(
        "error",
        message="Couldn't find ffmpeg or avconv - defaulting to ffmpeg, but may not work",
        category=RuntimeWarning,
    )
    try:
        from pydub import AudioSegment as _AudioSegment  # type: ignore
    except (ImportError, RuntimeWarning):  # pragma: no cover - import-time fallback
        _AudioSegment = None

AudioSegment: Any | None = _AudioSegment

from openai.resources.realtime.realtime import AsyncRealtimeConnection

CHUNK_LENGTH_S = 0.05  # 100ms
SAMPLE_RATE = 24000
FORMAT = pyaudio.paInt16
CHANNELS = 1

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

_DEVICE_ENV_VARS = ("MIC_DEVICE", "INPUT_DEVICE", "AUDIO_INPUT_DEVICE")


def _parse_device_identifier(value: str) -> int | str:
    value = value.strip()
    try:
        return int(value)
    except ValueError:
        return value


def _match_device_by_name(name: str, devices: list[dict[str, Any]]) -> int | None:
    lowered = name.lower()
    for idx, info in enumerate(devices):
        if info.get("max_input_channels", 0) > 0 and lowered in info.get("name", "").lower():
            return idx
    return None


def resolve_input_device(devices: list[dict[str, Any]] | None = None) -> tuple[int | str | None, list[str]]:
    devices = list(devices or sd.query_devices())
    messages: list[str] = []

    override: str | None = None
    for env_name in _DEVICE_ENV_VARS:
        value = os.getenv(env_name)
        if value:
            override = value
            messages.append(f"Environment override: {env_name}={value}")
            break

    selected: int | str | None = None

    if override is not None:
        parsed = _parse_device_identifier(override)
        if isinstance(parsed, int):
            if 0 <= parsed < len(devices) and devices[parsed].get("max_input_channels", 0) > 0:
                selected = parsed
            else:
                messages.append(
                    "Requested input device index is invalid or has no input channels; falling back to auto selection."
                )
        else:
            match = _match_device_by_name(parsed, devices)
            if match is not None:
                selected = match
                messages.append(f"Matched input device by name '{parsed}'.")
            else:
                messages.append(
                    f"No input device contains '{parsed}' in its name; falling back to auto selection."
                )

    if selected is None:
        default_devices = sd.default.device  # type: ignore[attr-defined]
        default_input = default_devices[0] if isinstance(default_devices, (list, tuple)) else default_devices
        if isinstance(default_input, int) and default_input >= 0:
            if devices[default_input].get("max_input_channels", 0) > 0:
                selected = default_input
                messages.append(f"Using sounddevice default input index {default_input}.")

    if selected is None:
        for idx, info in enumerate(devices):
            if info.get("max_input_channels", 0) > 0:
                selected = idx
                messages.append(f"Auto-selected first input-capable device index {idx}.")
                break

    if selected is None:
        messages.append(
            "No suitable audio input device found. Set MIC_DEVICE to the desired index or name and restart."
        )

    return selected, messages

def _convert_wav_bytes(audio_bytes: bytes) -> bytes:
    """Fallback WAV-only decoder used when pydub/ffmpeg is unavailable."""
    with contextlib.closing(wave.open(io.BytesIO(audio_bytes))) as wav_file:
        sample_rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        frame_count = wav_file.getnframes()
        raw_audio = wav_file.readframes(frame_count)

    if sample_width == 1:
        data = np.frombuffer(raw_audio, dtype=np.uint8)
        data = ((data.astype(np.int16) - 128) << 8).astype(np.int16)
    elif sample_width == 2:
        data = np.frombuffer(raw_audio, dtype=np.int16)
    else:
        raise RuntimeError("Unsupported WAV sample width; install ffmpeg for broader format support.")

    if channels > 1:
        data = data.reshape(-1, channels).astype(np.float32).mean(axis=1)
    else:
        data = data.astype(np.float32)

    if len(data) == 0:
        return b""

    if sample_rate != SAMPLE_RATE:
        duration = len(data) / float(sample_rate)
        target_length = max(1, int(round(duration * SAMPLE_RATE)))
        source_positions = np.linspace(0, len(data) - 1, num=len(data), dtype=np.float64)
        target_positions = np.linspace(0, len(data) - 1, num=target_length, dtype=np.float64)
        data = np.interp(target_positions, source_positions, data)

    data = np.clip(data, np.iinfo(np.int16).min, np.iinfo(np.int16).max).astype(np.int16)
    return data.tobytes()


def audio_to_pcm16_base64(audio_bytes: bytes) -> bytes:
    if AudioSegment is not None:
        try:
            audio = AudioSegment.from_file(io.BytesIO(audio_bytes))
        except Exception:
            pass
        else:
            pcm_audio = (
                audio.set_frame_rate(SAMPLE_RATE)
                .set_channels(CHANNELS)
                .set_sample_width(2)
                .raw_data
            )
            return pcm_audio

    try:
        return _convert_wav_bytes(audio_bytes)
    except wave.Error as exc:
        raise RuntimeError(
            "Unable to decode audio without ffmpeg; install ffmpeg for additional format support."
        ) from exc


class AudioPlayerAsync:
    def __init__(self):
        self.queue = []
        self.lock = threading.Lock()
        self.stream = sd.OutputStream(
            callback=self.callback,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=np.int16,
            blocksize=int(CHUNK_LENGTH_S * SAMPLE_RATE),
        )
        self.playing = False
        self._frame_count = 0

    def callback(self, outdata, frames, time, status):  # noqa
        with self.lock:
            data = np.empty(0, dtype=np.int16)

            # get next item from queue if there is still space in the buffer
            while len(data) < frames and len(self.queue) > 0:
                item = self.queue.pop(0)
                frames_needed = frames - len(data)
                data = np.concatenate((data, item[:frames_needed]))
                if len(item) > frames_needed:
                    self.queue.insert(0, item[frames_needed:])

            self._frame_count += len(data)

            # fill the rest of the frames with zeros if there is no more data
            if len(data) < frames:
                data = np.concatenate((data, np.zeros(frames - len(data), dtype=np.int16)))

        outdata[:] = data.reshape(-1, 1)

    def reset_frame_count(self):
        self._frame_count = 0

    def get_frame_count(self):
        return self._frame_count

    def add_data(self, data: bytes):
        with self.lock:
            # bytes is pcm16 single channel audio data, convert to numpy array
            np_data = np.frombuffer(data, dtype=np.int16)
            self.queue.append(np_data)
            if not self.playing:
                self.start()

    def start(self):
        self.playing = True
        self.stream.start()

    def stop(self):
        self.playing = False
        self.stream.stop()
        with self.lock:
            self.queue = []

    def terminate(self):
        self.stream.close()


async def send_audio_worker_sounddevice(
    connection: AsyncRealtimeConnection,
    should_send: Callable[[], bool] | None = None,
    start_send: Callable[[], Awaitable[None]] | None = None,
    device: int | str | None = None,
):
    sent_audio = False

    device_info = list(sd.query_devices())
    selection_messages: list[str] = ["Detected audio devices:"]
    for idx, info in enumerate(device_info):
        selection_messages.append(
            f"[{idx}] {info.get('name', 'Unknown')} (inputs={info.get('max_input_channels', 0)}, "
            f"outputs={info.get('max_output_channels', 0)})"
        )

    selected_device = device
    if selected_device is None:
        selected_device, resolved_messages = resolve_input_device(device_info)
        selection_messages.extend(resolved_messages)
    else:
        selection_messages.append(f"Using explicitly provided input device: {selected_device}")

    for message in selection_messages:
        print(f"[audio] {message}")

    read_size = int(SAMPLE_RATE * 0.02)

    stream_kwargs: dict[str, Any] = dict(
        channels=CHANNELS,
        samplerate=SAMPLE_RATE,
        dtype="int16",
    )
    if selected_device is not None:
        stream_kwargs["device"] = selected_device

    stream = sd.InputStream(**stream_kwargs)
    stream.start()

    silence_frames = 0
    silence_notified = False

    try:
        while True:
            if stream.read_available < read_size:
                await asyncio.sleep(0)
                continue

            data, _ = stream.read(read_size)

            # Detect prolonged silence to alert the user if no audio is captured.
            peak = float(np.abs(data).max()) if data.size else 0.0
            if peak < 750:
                silence_frames += 1
            else:
                if silence_notified:
                    print("[audio] Detected microphone activity again.")
                    silence_notified = False
                silence_frames = 0

            if not silence_notified and silence_frames >= 200:
                print(
                    "[audio] Microphone input appears silent. Check input levels or set MIC_DEVICE env var to the desired device."
                )
                silence_notified = True

            if should_send() if should_send else True:
                if not sent_audio and start_send:
                    await start_send()
                await connection.send(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(data.tobytes()).decode("utf-8"),
                    }
                )
                sent_audio = True

            elif sent_audio:
                print("Done, triggering inference")
                await connection.send({"type": "input_audio_buffer.commit"})
                await connection.send({"type": "response.create", "response": {}})
                sent_audio = False

            await asyncio.sleep(0)

    except KeyboardInterrupt:
        pass
    finally:
        stream.stop()
        stream.close()
