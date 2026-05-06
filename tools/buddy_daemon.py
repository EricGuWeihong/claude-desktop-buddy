#!/usr/bin/env python3
"""Daemon that owns the M5Stick USB serial port exclusively.

Two responsibilities:
  1. Read approval responses from device → inject into Claude Code via tmux
  2. Forward hook data from FIFO → device serial

Hook scripts (buddy_send.py, push_time.py) write to ~/.claude/buddy_send_fifo
instead of the serial port directly. The daemon forwards everything from the
FIFO to the serial port.

tmux session auto-detection uses a three-strategy approach:
  1. Find the Claude Code process PID → match to tmux pane
  2. Session name contains "claude"
  3. Current working directory matches pane path

Usage:
    python3 buddy_daemon.py                    # auto-detect tmux session + pane
    python3 buddy_daemon.py -t claude -p 0     # explicit session/pane

Runs until Ctrl-C. Silently reconnects if the M5Stick is unplugged and
plugged back in.
"""
import argparse
import base64
import fcntl
import glob
import json
import os
import select
import subprocess
import sys
import threading
import time

# Load .env from project root (one level up from tools/).
_ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH, "r") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# Fix SSL cert path on macOS for Python 3.11+
import certifi
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

# Fix opus library path on macOS (Homebrew) — ctypes.util.find_library
# only searches /usr/lib, so it won't find Homebrew's opus. We must
# monkey-patch the module-level variable before importing the rest of
# opuslib, which triggers the import at package level.
import ctypes.util
_original_find_library = ctypes.util.find_library
def _patched_find_library(name):
    if name == 'opus':
        for p in ['/opt/homebrew/lib/libopus.dylib',
                  '/opt/homebrew/lib/libopus.0.dylib',
                  '/usr/local/lib/libopus.dylib']:
            if os.path.exists(p):
                return p
    return _original_find_library(name)
ctypes.util.find_library = _patched_find_library
os.environ.setdefault("DYLD_LIBRARY_PATH", "/opt/homebrew/lib")

import opuslib

# Single lock guarding ALL writes to the device transport. Without this,
# the heartbeat timer (main thread), FIFO forwarder (main thread), and the
# voice ACK writer (transcribe sub-thread) can interleave bytes mid-message,
# corrupting JSON lines on the device. That manifested as the buddy hanging
# in ANALYZING until the ack timeout because the corrupted ack failed to
# parse and voiceOnAck was never called.
_transport_write_lock = threading.Lock()


def ser_write(transport, data):
    """Thread-safe write to the active transport (USB or BLE)."""
    if transport is None:
        return
    with _transport_write_lock:
        transport.write(data)


# --- Transport auto-detection ------------------------------------------------

_BLE_CACHE_PATH = os.path.expanduser("~/.claude/buddy_ble_cache.json")


def _load_ble_cache():
    """Return cached BLE address string, or None."""
    try:
        with open(_BLE_CACHE_PATH) as f:
            return json.load(f).get("address")
    except Exception:
        return None


def _save_ble_cache(address):
    try:
        os.makedirs(os.path.dirname(_BLE_CACHE_PATH), exist_ok=True)
        with open(_BLE_CACHE_PATH, "w") as f:
            json.dump({"address": address}, f)
    except Exception:
        pass


def create_transport():
    """Auto-detect: USB first, then BLE (macOS only). Returns Transport or None."""
    # On non-macOS, USB only (existing behavior).
    if sys.platform != "darwin":
        from transport_usb import USBTransport
        t = USBTransport()
        if t.open():
            log("Using USB transport")
            return t
        return None

    # macOS: try USB first, then BLE.
    from transport_usb import find_serial_port
    if find_serial_port():
        from transport_usb import USBTransport
        t = USBTransport()
        if t.open():
            log("Using USB transport")
            return t
        log("USB port found but open failed, trying BLE")

    # Try BLE.
    log("Scanning for BLE device...")
    from transport_ble import BLETransport
    t = BLETransport(cached_address=_load_ble_cache())
    if t.open():
        _save_ble_cache(t.address)
        log(f"Using BLE transport ({t.address})")
        return t
    log("No BLE device found")
    return None
SERIAL_BAUD = 115200
FIFO_PATH = os.path.expanduser("~/.claude/buddy_send_fifo")
ASR_BACKEND = os.environ.get("BUDDY_ASR_BACKEND", "qwen").lower()
QWEN_MODEL = os.environ.get("BUDDY_ASR_QWEN_MODEL", "paraformer-realtime-v2")
QWEN_LANGUAGE = os.environ.get("BUDDY_ASR_LANGUAGE", "zh")
QWEN_LANGUAGE_HINTS = os.environ.get("BUDDY_ASR_LANGUAGE_HINTS", "zh,en").split(",")


def find_tmux_session():
    """Auto-detect the tmux session running Claude Code.

    Strategy 1: Find the Claude Code process PID, match it to a tmux pane
       by walking up the process tree. This is the most reliable — it
       actually verifies which pane is running Claude, not just trusting
       a saved identifier.
    Strategy 2: Read pane_id saved by the hook as a fallback.
    Strategy 3: Session name contains "claude".
    Strategy 4: Most recently active session.
    """
    # Get all panes across all sessions.
    # Use colon as separator; we parse with a fixed field count since
    # pane_current_path may contain colons.
    try:
        out = subprocess.check_output(
            ["tmux", "list-panes", "-a", "-F",
             "#{session_name}\t#{window_index}\t#{pane_id}\t#{pane_pid}\t#{pane_current_command}"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        return None, None

    if not out:
        return None, None

    panes = []
    for line in out.split("\n"):
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        panes.append({
            "session": parts[0],
            "window": parts[1],
            "pane_id": parts[2],
            "pane_pid": parts[3],
            "command": parts[4] if len(parts) > 4 else "",
        })

    # Strategy 1: Find Claude Code process, match to pane via process tree.
    claude_pid = _find_claude_pid()
    if claude_pid is not None:
        for p in panes:
            if _pid_in_tmux_tree(str(claude_pid), p["pane_pid"]):
                log(f"Claude Code PID {claude_pid} found in pane {p['pane_id']} "
                    f"({p['session']}, cmd={p['command']})")
                return p["session"], p["pane_id"]

    # Strategy 2: Read the pane_id saved by the hook script. Verify it's
    # still valid by checking the pane exists.
    session, pane_id = _load_tmux_target()
    if pane_id and pane_id.startswith("%"):
        try:
            subprocess.check_output(
                ["tmux", "list-panes", "-t", pane_id],
                stderr=subprocess.DEVNULL,
            )
            log(f"Using pane_id from hook: {pane_id} (session: {session})")
            return session, pane_id
        except subprocess.CalledProcessError:
            pass  # pane gone, fall through

    # Strategy 3: Session name contains "claude".
    sessions_seen = set()
    for p in panes:
        if "claude" in p["session"].lower() and p["session"] not in sessions_seen:
            sessions_seen.add(p["session"])
            return p["session"], p["pane_id"]

    # Strategy 4: Use the first available pane.
    if panes:
        return panes[0]["session"], panes[0]["pane_id"]

    return None, None


def _load_tmux_target():
    """Read the tmux session/pane_id recorded by buddy_send.py.

    Used as fallback when process-based detection can't find Claude Code
    (e.g. session is idle and Claude process has exited).
    """
    target_path = os.path.expanduser("~/.claude/buddy_tmux_target.json")
    try:
        with open(target_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("session"), data.get("pane_id")
    except Exception:
        return None, None


def _find_claude_pid():
    """Find the PID of a running Claude Code process.

    Claude Code runs as `claude` (node binary). We look for processes whose
    command contains "claude" but excludes our own Python process and common
    false positives like "clam".
    """
    try:
        out = subprocess.check_output(
            ["ps", "aux"], text=True, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None

    my_pid = os.getpid()
    my_ppid = os.getppid()

    best_pid = None
    for line in out.split("\n"):
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        pid = parts[1]
        cmd = parts[10]
        # Skip ourselves and parent.
        try:
            if int(pid) in (my_pid, my_ppid):
                continue
        except ValueError:
            continue
        # Claude Code binary shows as "claude" in the command.
        if "claude" in cmd.lower() and "claude-desktop" not in cmd.lower():
            # Keep the first match; prefer shorter command lines (more likely
            # the CLI binary vs. long node invocations).
            if best_pid is None or len(cmd) < len(parts[10]):
                best_pid = int(pid)

    return best_pid


def _pid_in_tmux_tree(target_pid, pane_pid):
    """Check if target_pid is a descendant of pane_pid in the process tree.

    Walks up the parent chain of target_pid to see if it reaches pane_pid.
    """
    current = target_pid
    visited = set()
    while current and current not in visited:
        if current == pane_pid:
            return True
        visited.add(current)
        try:
            out = subprocess.check_output(
                ["ps", "-o", "ppid=", "-p", current],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
            current = out.strip()
        except subprocess.CalledProcessError:
            return False
    return False


def send_to_tmux(pane_target, key):
    """Inject a key into the tmux pane identified by pane_id or target."""
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_target, key, "Enter"],
            check=True, timeout=5,
        )
        return True
    except Exception as e:
        log(f"  tmux send failed ({pane_target}): {e}")
        return False


def ensure_fifo():
    """Create FIFO if it doesn't exist."""
    os.makedirs(os.path.dirname(FIFO_PATH), exist_ok=True)
    if os.path.exists(FIFO_PATH):
        os.remove(FIFO_PATH)
    os.mkfifo(FIFO_PATH)


def kill_existing_daemon():
    """Kill any existing buddy_daemon processes to avoid serial port conflicts."""
    my_pid = os.getpid()
    my_ppid = os.getppid()
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "buddy_daemon.py"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        return  # no matches
    for pid_str in out.split():
        if not pid_str:
            continue
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid == my_pid or pid == my_ppid:
            continue
        try:
            os.kill(pid, 9)  # SIGKILL — force kill, no graceful shutdown
            log(f"Killed existing daemon PID {pid}")
        except OSError:
            pass


def reset_hook_state():
    """Clear cached active_prompt from buddy_send_state.json so stale
    prompts don't reappear after daemon restart."""
    state_path = os.path.expanduser("~/.claude/buddy_send_state.json")
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        state["active_prompt"] = None
        with open(state_path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(state_path + ".tmp", state_path)
        log("Cleared stale hook state")
    except Exception:
        pass  # file may not exist or be corrupt


_LOG_FILE = os.path.expanduser("~/.claude/buddy_voice.log")
_DRAGONITE_SYNC_PATH = os.path.expanduser("~/.claude/dragonite_state.json")

def log(msg):
    ts = time.strftime("%H:%M:%S")
    line = f"[buddy_daemon] {ts} {msg}"
    print(line, flush=True)
    try:
        with open(_LOG_FILE, "a") as _lf:
            _lf.write(line + "\n")
    except Exception:
        pass


def _write_dragonite_sync(prompt_id, source):
    """Write to dragonite_state.json so the Swift desktop app knows
    this prompt was handled by ESP32. Prevents duplicate approval UIs."""
    try:
        os.makedirs(os.path.dirname(_DRAGONITE_SYNC_PATH), exist_ok=True)
        state = {
            "last_approver": source,
            "last_prompt_id": prompt_id,
            "last_timestamp": time.time(),
        }
        tmp = _DRAGONITE_SYNC_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, _DRAGONITE_SYNC_PATH)
    except Exception:
        pass  # non-critical — desktop app just won't know about this approval


# --- Voice push-to-talk -----------------------------------------------------

class VoiceSession:
    """Reassembles audio_begin/chunk/end JSON commands into a WAV file,
    transcribes it, pastes the result into the active window, and tracks
    the pasted character count so a subsequent voice_cancel can delete it.
    """

    def __init__(self, transport):
        self.transport = transport
        self.reset()
        self.last_pasted_len = 0
        self._qwen_conv = None  # persistent Qwen WebSocket connection

    def reset(self):
        self.active = False
        self.chunks = []         # list[(idx, bytes)]
        self.codec = "opus_b64"  # default from firmware
        self.sample_rate = 16000
        self.bits = 16
        self.channels = 1
        self._asr_running = False
        self._opus_decoder = None

    def begin(self, msg):
        self.reset()
        self.active = True
        self.sample_rate = int(msg.get("sr", 16000))
        self.bits = int(msg.get("bits", 16))
        self.channels = int(msg.get("ch", 1))
        self.codec = msg.get("codec", "opus_b64")
        self._transcript = None

        if ASR_BACKEND != "qwen":
            self.pcm_buffer = []
            self.pcm_bytes = 0
            log(f"voice: begin sr={self.sample_rate} codec={self.codec} (buffer mode)")
            return

        self.pcm_buffer = []
        self.pcm_bytes = 0

        if not self._qwen_conv:
            self._qwen_connect()

        self._qwen_transcript = None
        self._qwen_partial = None
        self._qwen_ready = True
        log(f"voice: begin sr={self.sample_rate} codec={self.codec} (streaming)")

    def chunk(self, msg):
        if not self.active:
            return
        idx = int(msg.get("i", -1))
        try:
            data = base64.b64decode(msg.get("data", ""))
        except Exception as e:
            log(f"voice: bad b64 chunk {idx}: {e}")
            return

        codec = msg.get("codec", self.codec)

        if codec == "opus_b64":
            try:
                if self._opus_decoder is None:
                    self._opus_decoder = opuslib.Decoder(self.sample_rate, self.channels)
                frame_samples = int(self.sample_rate * 0.02)
                pcm_frame = self._opus_decoder.decode(bytes(data), frame_samples, decode_fec=False)
                # Stream to Qwen immediately.
                if ASR_BACKEND == "qwen" and self._qwen_ready:
                    self._qwen_conv.append_audio(base64.b64encode(pcm_frame).decode('utf-8'))
                self.pcm_buffer.append(pcm_frame)
                self.pcm_bytes += len(pcm_frame)
            except Exception as e:
                log(f"voice: opus decode error chunk {idx}: {e}")
        else:
            # pcm_b64 fallback
            self.pcm_buffer.append(data)
            self.pcm_bytes += len(data)
            if ASR_BACKEND == "qwen" and self._qwen_ready:
                self._qwen_conv.append_audio(msg.get("data", ""))

        self.chunks.append((idx, data, codec))

    def end(self, msg):
        if not self.active:
            return

        log(f"voice: end {self.pcm_bytes} bytes PCM")

        # Signal Qwen that audio is complete and start transcribing.
        if ASR_BACKEND == "qwen" and self._qwen_ready:
            self._qwen_conv.commit()

        # Mark ASR running and reset session-level state BEFORE spawning the
        # waiter — otherwise reset() would clobber `_asr_running` back to False
        # and break the cancel guard. Keep `_qwen_transcript` / `_qwen_ready`
        # untouched so the waiter can read them.
        self.active = False
        self.chunks = []
        self._opus_decoder = None
        self._asr_running = True

        t = threading.Thread(target=self._transcribe_async, daemon=True)
        t.start()

    def _transcribe_async(self):
        """Wait for streaming Qwen transcription result."""
        t0 = time.time()

        if ASR_BACKEND == "qwen" and self._qwen_ready:
            for attempt in range(100):  # 10 s max
                if self._qwen_transcript is not None:
                    break
                if attempt == 50:  # log at 5s mark
                    log(f"voice: still waiting for transcript ({attempt * 0.1:.0f}s)")
                time.sleep(0.1)
            text = (self._qwen_transcript or "").strip()
            if not text and self._qwen_transcript is None:
                log("voice: transcript never set (no callback received)")
            elif not text:
                log("voice: transcript was empty string")
        else:
            log("voice: ASR backend not supported in streaming mode")
            self._asr_running = False
            self._ack(False, err="unsupported_backend")
            return

        elapsed = (time.time() - t0) * 1000
        log(f"voice: ASR done in {elapsed:.0f}ms, transcript_set={self._qwen_transcript is not None}")

        if not text:
            log("voice: empty transcript")
            self._asr_running = False
            self._ack(False, err="empty")
            return
        log(f"voice: text=\"{text}\"")

        self._asr_running = False
        self._ack(True, text=text)
        self._paste(text)

    def cancel_pasted(self):
        if self._asr_running:
            log("voice: cancel — ASR still running, ignoring")
            return
        n = self.last_pasted_len
        self.last_pasted_len = 0
        if n <= 0:
            log("voice: cancel — nothing tracked")
            return
        log(f"voice: cancel — sending {n} backspaces")
        script = (f'tell application "System Events" to repeat {n} times\n'
                  f'  key code 51\nend repeat')
        try:
            subprocess.run(["osascript", "-e", script], check=False, timeout=10)
        except Exception as e:
            log(f"voice: cancel osascript failed: {e}")

    def press_return(self):
        self.last_pasted_len = 0  # commit — no longer cancellable
        try:
            subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to key code 36'],
                check=False, timeout=5,
            )
        except Exception as e:
            log(f"voice: press_return failed: {e}")

    def _qwen_connect(self):
        """Open a persistent Qwen WebSocket connection for ASR."""
        api_key = os.environ.get("DASHSCOPE_API_KEY")
        if not api_key:
            raise RuntimeError("DASHSCOPE_API_KEY not set")
        try:
            import dashscope  # type: ignore
            from dashscope.audio.qwen_omni import (  # type: ignore
                OmniRealtimeConversation,
                OmniRealtimeCallback,
                AudioFormat,
                MultiModality,
            )
            from dashscope.audio.qwen_omni.omni_realtime import (  # type: ignore
                TranscriptionParams,
            )
        except ImportError:
            raise RuntimeError("dashscope not installed (pip install dashscope)")
        dashscope.api_key = api_key

        self._multi_modality_text = MultiModality.TEXT

        # Only the `.completed` event is authoritative — `.text` events stream
        # in *during* recording with partial fragments, and writing those into
        # `_qwen_transcript` was racing the `_transcribe_async` waiter, which
        # would latch onto a partial and skip the real result. Keep the
        # partials in `_qwen_partial` for diagnostics only.
        vs = self
        class _QwenCB(OmniRealtimeCallback):
            def on_event(self_cb, response):
                event_type = response.get('type', '')
                if event_type == 'conversation.item.input_audio_transcription.completed':
                    transcript = response.get('transcript', '')
                    log(f"voice: qwen final: \"{transcript}\"")
                    vs._qwen_transcript = transcript
                elif event_type == 'conversation.item.input_audio_transcription.text':
                    text = response.get('text', '')
                    if text:
                        vs._qwen_partial = text
                elif event_type == 'error':
                    log(f"voice: qwen error: {json.dumps(response, ensure_ascii=False)[:200]}")
                else:
                    log(f"voice: qwen event: {event_type}")

        cb = _QwenCB()
        self._qwen_conv = OmniRealtimeConversation(
            model='qwen3-asr-flash-realtime',
            callback=cb,
        )
        self._qwen_conv.connect()

        self._qwen_conv.update_session(
            output_modalities=[MultiModality.TEXT],
            input_audio_format=AudioFormat.PCM_16000HZ_MONO_16BIT,
            enable_input_audio_transcription=True,
            input_audio_transcription_model='qwen3-asr-flash-realtime',
            enable_turn_detection=False,
            transcription_params=TranscriptionParams(
                language=QWEN_LANGUAGE,
                sample_rate=16000,
            ),
        )
        # SDK bug: transcription_params wipes model from input_audio_transcription.
        if 'input_audio_transcription' in self._qwen_conv.config:
            self._qwen_conv.config['input_audio_transcription']['model'] = \
                'qwen3-asr-flash-realtime'
        log(f"voice: qwen connected (config={json.dumps(self._qwen_conv.config, default=str, ensure_ascii=False)[:200]})")
        self._qwen_ready = True

    def _qwen_disconnect(self):
        if self._qwen_conv:
            try:
                self._qwen_conv.close()
                log("voice: qwen websocket closed")
            except Exception:
                pass
            self._qwen_conv = None

    def _paste(self, text):
        self.last_pasted_len = len(text)
        try:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False, timeout=5)
            subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to keystroke "v" using command down'],
                check=False, timeout=5,
            )
        except Exception as e:
            log(f"voice: paste failed: {e}")

    def _ack(self, ok, text="", err=""):
        msg = {"ack": "voice", "ok": bool(ok)}
        if text:
            msg["text"] = text
        if err:
            msg["err"] = err
        try:
            # ensure_ascii=False keeps Chinese as compact UTF-8 (~3 bytes/char)
            # instead of \uXXXX escapes (~6 bytes), shrinking long-transcript
            # acks from ~600 bytes to ~250 bytes — small enough to fit in the
            # device's USB CDC RX ringbuffer comfortably.
            ser_write(self.transport, (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            log(f"voice: ack sent ok={ok}" + (f' text="{text}"' if text else "") + (f" err={err}" if err else ""))
        except Exception as e:
            log(f"voice: ack write failed: {e}")


def main():
    parser = argparse.ArgumentParser(description="M5Stick ↔ Claude Code bridge")
    parser.add_argument("-t", "--tmux_session", help="tmux session name (auto-detect if omitted)")
    parser.add_argument("-p", "--tmux_pane", default="0", help="tmux pane index (default: 0)")
    args = parser.parse_args()

    session = args.tmux_session
    pane = args.tmux_pane
    pane_target = None  # set below, used directly as tmux -t target (e.g. %1)

    if not session:
        session, auto_pane = find_tmux_session()
        if session:
            pane_target = auto_pane or f"{session}:{pane}"
            log(f"Auto-detected Claude in pane {pane_target}")
        else:
            log("No tmux session found. Specify with -t <session>.")
            sys.exit(1)
    else:
        pane_target = f"{session}:{pane}"

    log(f"Listening for M5Stick approval responses → tmux send to {pane_target}")
    log(f"voice: ASR backend = {ASR_BACKEND}")

    # --- FIFO for hook scripts → daemon ---
    # Kill any existing daemon instances first
    kill_existing_daemon()

    ensure_fifo()
    # Open for reading (non-blocking) + writing (dummy to prevent EOF)
    fifo_fd = os.open(FIFO_PATH, os.O_RDONLY | os.O_NONBLOCK)
    fifo_write_fd = os.open(FIFO_PATH, os.O_WRONLY | os.O_NONBLOCK)
    log(f"FIFO ready: {FIFO_PATH}")

    # --- Clear stale hook state so old prompts don't reappear ---
    reset_hook_state()

    # --- Signal handler for clean shutdown ---
    import signal

    def _shutdown(signum, frame):
        log("daemon shutting down...")
        if voice:
            voice._qwen_disconnect()
        if transport:
            transport.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # --- Reset device state so buddy starts from a clean slate ---
    transport = None
    last_transport_type = None  # "usb" | "ble" | None
    last_heartbeat = 0
    voice = None     # VoiceSession; rebound when transport is (re)opened
    line_buf = b""   # persists across select cycles — JSON lines (e.g. audio
                     # chunks) routinely span multiple read() calls.

    while True:
        if transport is None:
            transport = create_transport()
            if transport is None:
                time.sleep(2)
                continue

            transport_type = "usb" if hasattr(transport, "_port") else "ble"
            if transport_type != last_transport_type:
                log(f"Transport connected: {transport_type} via {transport.port_name}")

            voice = VoiceSession(transport)
            line_buf = b""
            last_transport_type = transport_type

            # Open persistent Qwen WebSocket now so first voice skips connect.
            if ASR_BACKEND == "qwen":
                try:
                    voice._qwen_connect()
                except Exception as e:
                    log(f"voice: qwen pre-connect failed ({e}), will retry on first use")

            # Reset device state on startup — clear stale prompts,
            # sessions, etc. so the buddy starts from a clean slate.
            time.sleep(2)
            # Drain any stale data from the input buffer first
            while transport.read(512):
                pass
            ser_write(transport, b'{"reset":1,"prompt":{"id":"","tool":"","hint":""},"msg":"ready","total":0,"running":0,"waiting":0}\n')

        if transport is None:
            time.sleep(2)
            continue

        # Send a heartbeat every 10s so firmware knows CLI daemon is alive.
        # This keeps dataConnected() true during idle periods when no hooks fire.
        now = time.time()
        if now - last_heartbeat >= 10:
            try:
                plat = sys.platform
                os_name = "macOS" if plat == "darwin" else ("Windows" if plat == "win32" else "Linux")
                ser_write(transport, json.dumps({
                    "daemon": 1,
                    "transport": "bt" if last_transport_type == "ble" else "usb",
                    "os": os_name,
                    "port": transport.port_name,
                }).encode() + b"\n")
                last_heartbeat = now
            except Exception:
                pass

        # Poll both transport and FIFO with select
        transport_fd = transport.fileno()

        # Detect BLE disconnect (callback flag) — must check before the
        # transport_fd < 0 guard, otherwise fileno() == -1 traps us in
        # an infinite sleep(0.1) loop that never reaches reconnect logic.
        # Actively probe via check_connected() to catch stale connections
        # that the callback may have missed.
        if last_transport_type == "ble" and not transport.check_connected():
            log("BLE connection lost, reconnecting...")
            try:
                transport.close()
            except Exception:
                pass
            transport = None
            last_transport_type = None
            voice = None
            continue

        if transport_fd < 0:
            time.sleep(0.1)
            continue
        fds = [transport_fd, fifo_fd]
        try:
            readable, _, _ = select.select(fds, [], [], 0.1)
        except Exception:
            continue

        for fd in readable:
            if fd == transport_fd:
                # Read from device — look for approval responses
                try:
                    data = transport.read(512)
                    if not data:
                        continue
                    # Drain any pending bytes
                    while True:
                        try:
                            more = transport.read(512)
                            if not more:
                                break
                            data += more
                        except Exception:
                            break
                    line_buf += data
                    # Keep last fragment (no trailing newline) in line_buf;
                    # parse the rest. Long base64 audio chunks span many
                    # 512-byte reads, so we cannot drop partials.
                    if len(line_buf) > 1 << 20:   # 1 MB sanity cap
                        log("line buffer overflow — dropping")
                        line_buf = b""
                    *complete, line_buf = line_buf.split(b"\n")
                    for raw in complete:
                        line_str = raw.decode("utf-8", errors="replace").strip()
                        if not line_str:
                            continue
                        # Forward device debug/diagnostic lines to our log
                        if not line_str.startswith("{"):
                            if line_str.startswith("[voice]"):
                                log(line_str)
                            continue
                        try:
                            msg = json.loads(line_str)
                        except json.JSONDecodeError:
                            continue
                        approval = msg.get("approval")
                        if approval == "yes":
                            req_id = msg.get("id", "?")
                            log(f"APPROVE from device (id={req_id[:12]}...) → sending 'Y'")
                            _write_dragonite_sync(req_id, "esp32")
                            send_to_tmux(pane_target, "Y")
                            continue
                        if approval == "no":
                            req_id = msg.get("id", "?")
                            log(f"DENY from device (id={req_id[:12]}...) → sending 'N'")
                            _write_dragonite_sync(req_id, "esp32")
                            send_to_tmux(pane_target, "N")
                            continue
                        cmd = msg.get("cmd")
                        if cmd in ("audio_begin", "audio_chunk", "audio_end") and voice is None:
                            log(f"voice: {cmd} DROPPED — voice session is None")
                        if cmd == "audio_begin" and voice is not None:
                            log(f"voice: begin sr={msg.get('sr')} codec={msg.get('codec')}")
                            voice.begin(msg)
                        elif cmd == "audio_chunk" and voice is not None:
                            voice.chunk(msg)
                        elif cmd == "audio_end" and voice is not None:
                            log(f"voice: end {voice.pcm_bytes} bytes PCM")
                            voice.end(msg)
                        elif cmd == "voice_enter" and voice is not None:
                            log("voice: ENTER from device")
                            voice.press_return()
                        elif cmd == "voice_cancel" and voice is not None:
                            log("voice: CANCEL from device")
                            voice.cancel_pasted()
                except Exception as e:
                    err_str = str(e)
                    # ESP32 USB-Serial often returns "device reports readiness
                    # to read but returned no data" — a select() false positive.
                    # Don't close/reopen the port; just skip this poll cycle.
                    if "readiness to read" in err_str or "returned no data" in err_str:
                        continue
                    log(f"Transport read error: {e}")
                    try:
                        transport.close()
                    except Exception:
                        pass
                    transport = None
                    last_transport_type = None

            elif fd == fifo_fd:
                # Read from hook scripts → forward to device
                try:
                    data = os.read(fifo_fd, 4096)
                    if data:
                        ser_write(transport, data)
                except Exception:
                    pass


if __name__ == "__main__":
    main()
