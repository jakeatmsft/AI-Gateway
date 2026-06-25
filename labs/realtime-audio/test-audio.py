#!/usr/bin/env uv run
####################################################################
# Sample TUI app with a push to talk interface to the Realtime API #
# If you have `uv` installed and the `OPENAI_API_KEY`              #
# environment variable set, you can run this example with just     #
#                                                                  #
# `./examples/realtime/push_to_talk_app.py`                        #
#                                                                  #
# On Mac, you'll also need `brew install portaudio ffmpeg`           #
####################################################################
#
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "textual",
#     "numpy",
#     "pyaudio",
#     "pydub",
#     "sounddevice",
#     "openai[realtime]",
# ]
#
# [tool.uv.sources]
# openai = { path = "../../", editable = true }
# ///
from __future__ import annotations

import base64
import asyncio
import numpy as np
from typing import Any, cast
from typing_extensions import override

from textual import events
from audio_util import CHANNELS, SAMPLE_RATE, AudioPlayerAsync, resolve_input_device
from textual.app import App, ComposeResult
from textual.widgets import Button, Static, RichLog
from textual.reactive import reactive
from textual.containers import Container

from openai import AsyncOpenAI
from openai.types.beta.realtime.session import Session
from openai.resources.realtime.realtime import AsyncRealtimeConnection
from dotenv import load_dotenv
import os
from openai import AsyncAzureOpenAI
load_dotenv()

apim_resource_gateway_url = os.getenv("APIM_RESOURCE_GATEWAY_URL")
inference_api_path = os.getenv("INFERENCE_API_PATH")
inference_api_version = os.getenv("INFERENCE_API_VERSION") or "2024-06-01"
api_key = os.getenv("API_KEY")

class SessionDisplay(Static):
    """A widget that shows the current session ID."""

    session_id = reactive("")

    @override
    def render(self) -> str:
        return f"Session ID: {self.session_id}" if self.session_id else "Connecting..."


class AudioStatusIndicator(Static):
    """A widget that shows the current audio recording status."""

    is_recording = reactive(False)

    @override
    def render(self) -> str:
        status = (
            "🔴 Recording… (Press K or Space to stop)"
            if self.is_recording
            else "⚪ Press K or Space to start recording (Q to quit)"
        )
        return status


class RealtimeApp(App[None]):
    CSS = """
        Screen {
            background: #1a1b26;  /* Dark blue-grey background */
        }

        Container {
            border: double rgb(91, 164, 91);
        }

        Horizontal {
            width: 100%;
        }

        #input-container {
            height: 5;  /* Explicit height for input container */
            margin: 1 1;
            padding: 1 2;
        }

        Input {
            width: 80%;
            height: 3;  /* Explicit height for input */
        }

        Button {
            width: 20%;
            height: 3;  /* Explicit height for button */
        }

        #bottom-pane {
            width: 100%;
            height: 82%;  /* Reduced to make room for session display */
            border: round rgb(205, 133, 63);
            content-align: center middle;
        }

        #status-indicator {
            height: 3;
            content-align: center middle;
            background: #2a2b36;
            border: solid rgb(91, 164, 91);
            margin: 1 1;
        }

        #session-display {
            height: 3;
            content-align: center middle;
            background: #2a2b36;
            border: solid rgb(91, 164, 91);
            margin: 1 1;
        }

        Static {
            color: white;
        }
    """

    client: AsyncOpenAI
    should_send_audio: asyncio.Event
    audio_player: AudioPlayerAsync
    last_audio_item_id: str | None
    connection: AsyncRealtimeConnection | None
    session: Session | None
    connected: asyncio.Event

    def __init__(self) -> None:
        super().__init__()
        self.connection = None
        self.session = None
        self.client =  AsyncAzureOpenAI(
            azure_endpoint=f"{apim_resource_gateway_url}/{inference_api_path}",
            api_key=api_key,
            api_version=inference_api_version)
        self.audio_player = AudioPlayerAsync()
        self.last_audio_item_id = None
        self.should_send_audio = asyncio.Event()
        self.connected = asyncio.Event()

    def _log(self, message: str, *, clear: bool = False) -> None:
        bottom_pane = self.query_one("#bottom-pane", RichLog)
        if clear:
            bottom_pane.clear()
        bottom_pane.write(message)

    @override
    def compose(self) -> ComposeResult:
        """Create child widgets for the app."""
        with Container():
            yield SessionDisplay(id="session-display")
            yield AudioStatusIndicator(id="status-indicator")
            yield Button("Start Recording", id="toggle-recording")
            yield RichLog(id="bottom-pane", wrap=True, highlight=True, markup=True)

    async def on_mount(self) -> None:
        self._log(
            "Press K, R, or Space (or click the button) to toggle the microphone. "
            "Set MIC_DEVICE to a device index or name if the wrong microphone is chosen.",
            clear=True,
        )
        self.run_worker(self.handle_realtime_connection())
        self.run_worker(self.send_mic_audio())

    async def handle_realtime_connection(self) -> None:
        async with self.client.realtime.connect(model="gpt-realtime") as conn:
            self.connection = conn
            self.connected.set()

            # note: this is the default and can be omitted
            # if you want to manually handle VAD yourself, then set `'turn_detection': None`
            await conn.session.update(
                session={
                    "audio": {
                        "input": {"turn_detection": {"type": "server_vad"}},
                    },
                    "model": "gpt-realtime",
                    "type": "realtime",
                }
            )

            acc_items: dict[str, Any] = {}

            async for event in conn:
                if event.type == "session.created":
                    self.session = event.session
                    session_display = self.query_one(SessionDisplay)
                    assert event.session.id is not None
                    session_display.session_id = event.session.id
                    continue

                if event.type == "session.updated":
                    self.session = event.session
                    continue

                if event.type == "response.output_audio.delta":
                    if event.item_id != self.last_audio_item_id:
                        self.audio_player.reset_frame_count()
                        self.last_audio_item_id = event.item_id

                    bytes_data = base64.b64decode(event.delta)
                    self.audio_player.add_data(bytes_data)
                    continue

                if event.type == "response.output_audio_transcript.delta":
                    try:
                        text = acc_items[event.item_id]
                    except KeyError:
                        acc_items[event.item_id] = event.delta
                    else:
                        acc_items[event.item_id] = text + event.delta

                    # Clear and update the entire content because RichLog otherwise treats each delta as a new line
                    bottom_pane = self.query_one("#bottom-pane", RichLog)
                    bottom_pane.clear()
                    bottom_pane.write(acc_items[event.item_id])
                    continue

    async def _get_connection(self) -> AsyncRealtimeConnection:
        await self.connected.wait()
        assert self.connection is not None
        return self.connection

    async def send_mic_audio(self) -> None:
        import sounddevice as sd  # type: ignore

        sent_audio = False
        device_info = list(sd.query_devices())
        selection_messages = ["Detected audio devices:"]
        for idx, info in enumerate(device_info):
            selection_messages.append(
                f"[{idx}] {info.get('name', 'Unknown')} (inputs={info.get('max_input_channels', 0)}, "
                f"outputs={info.get('max_output_channels', 0)})"
            )

        selected_device, resolved_messages = resolve_input_device(device_info)
        selection_messages.extend(resolved_messages)

        for message in selection_messages:
            print(f"[audio] {message}")

        if resolved_messages:
            self._log(resolved_messages[-1])

        read_size = int(SAMPLE_RATE * 0.02)

        stream_kwargs: dict[str, Any] = dict(
            channels=CHANNELS,
            samplerate=SAMPLE_RATE,
            dtype="int16",
        )
        if selected_device is not None:
            stream_kwargs["device"] = selected_device

        try:
            stream = sd.InputStream(**stream_kwargs)
        except Exception as exc:  # pragma: no cover - depends on hardware
            error_message = (
                "Unable to open an input stream. If you have multiple microphones, set MIC_DEVICE "
                "to the desired device index or name and restart the app."
            )
            print(f"[audio] {error_message}\n[audio] {exc}")
            self._log(error_message)
            return

        stream.start()

        status_indicator = self.query_one(AudioStatusIndicator)

        silence_frames = 0
        silence_notified = False

        try:
            while True:
                if stream.read_available < read_size:
                    await asyncio.sleep(0)
                    continue

                await self.should_send_audio.wait()
                status_indicator.is_recording = True

                data, _ = stream.read(read_size)
                data_bytes = data.tobytes()

                peak = float(np.abs(data).max()) if data.size else 0.0
                if peak < 750:
                    silence_frames += 1
                else:
                    if silence_notified:
                        print("[audio] Detected microphone activity again.")
                        self._log("Microphone input detected.")
                        silence_notified = False
                    silence_frames = 0

                if not silence_notified and silence_frames >= 200:
                    notice = (
                        "Microphone input appears silent. Check input levels or set MIC_DEVICE to select a device."
                    )
                    print(f"[audio] {notice}")
                    self._log(notice)
                    silence_notified = True

                connection = await self._get_connection()
                if not sent_audio:
                    asyncio.create_task(connection.send({"type": "response.cancel"}))
                    sent_audio = True

                await connection.input_audio_buffer.append(
                    audio=base64.b64encode(data_bytes).decode("utf-8")
                )

                await asyncio.sleep(0)
        except KeyboardInterrupt:
            pass
        finally:
            stream.stop()
            stream.close()

    async def on_key(self, event: events.Key) -> None:
        """Handle key press events."""
        key = event.key.lower()

        if key == "enter":
            button = self.query_one("#toggle-recording", Button)
            button.press()
            return

        if key == "q":
            self.exit()
            return

        if key in {"k", "space", "r", " "}:
            await self._toggle_recording()

    async def _toggle_recording(self) -> None:
        status_indicator = self.query_one(AudioStatusIndicator)
        button = self.query_one("#toggle-recording", Button)
        if status_indicator.is_recording:
            self.should_send_audio.clear()
            status_indicator.is_recording = False
            button.label = "Start Recording"

            if self.session and self.session.turn_detection is None:
                # The default in the API is that the model will automatically detect when the user has
                # stopped talking and then start responding itself.
                #
                # However if we're in manual `turn_detection` mode then we need to
                # manually tell the model to commit the audio buffer and start responding.
                conn = await self._get_connection()
                await conn.input_audio_buffer.commit()
                await conn.response.create()
        else:
            self.should_send_audio.set()
            status_indicator.is_recording = True
            button.label = "Stop Recording"

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "toggle-recording":
            await self._toggle_recording()


if __name__ == "__main__":
    app = RealtimeApp()
    app.run()
