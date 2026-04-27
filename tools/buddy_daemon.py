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
import fcntl
import glob
import json
import os
import select
import subprocess
import sys
import time

SERIAL_BAUD = 115200
FIFO_PATH = os.path.expanduser("~/.claude/buddy_send_fifo")


def find_serial_port():
    """Return the first /dev/cu.usbmodem* path, or None."""
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    return ports[0] if ports else None


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
    import signal
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
            continue  # don't kill ourselves or our parent
        try:
            os.kill(pid, signal.SIGTERM)
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


def log(msg):
    print(f"[buddy_daemon] {msg}", flush=True)


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

    # --- Reset device state so buddy starts from a clean slate ---
    ser = None
    last_port = None
    ser_fd = None
    last_heartbeat = 0

    while True:
        port = find_serial_port()
        if port != last_port:
            if port:
                log(f"Serial port detected: {port}")
            else:
                if last_port:
                    log("Serial port lost, waiting for reconnect...")
                last_port = port

        if port and (ser is None or last_port != port):
            try:
                import serial
                ser = serial.Serial(port, SERIAL_BAUD, timeout=0)
                ser_fd = ser.fileno()
                log(f"Connected to {port}")
                last_port = port
                # Reset device state on startup — clear stale prompts,
                # sessions, etc. so the buddy starts from a clean slate.
                time.sleep(2)
                # Drain any stale data from the serial buffer first
                ser.reset_input_buffer()
                ser.write(b'{"reset":1,"prompt":{"id":"","tool":"","hint":""},"msg":"ready","total":0,"running":0,"waiting":0}\n')
            except ImportError:
                log("ERROR: pyserial not installed. Run: pip3 install pyserial")
                sys.exit(1)
            except Exception as e:
                log(f"Open failed: {e}, retrying in 5s...")
                ser = None
                ser_fd = None
                last_port = None
                time.sleep(5)
                continue

        if ser is None:
            time.sleep(2)
            continue

        # Send a heartbeat every 10s so firmware knows CLI daemon is alive.
        # This keeps dataConnected() true during idle periods when no hooks fire.
        now = time.time()
        if now - last_heartbeat >= 10:
            try:
                ser.write(b'{"daemon":1}\n')
                last_heartbeat = now
            except Exception:
                pass

        # Poll both serial and FIFO with select
        fds = [ser_fd, fifo_fd]
        try:
            readable, _, _ = select.select(fds, [], [], 0.1)
        except Exception:
            continue

        for fd in readable:
            if fd == ser_fd:
                # Read from device — look for approval responses
                try:
                    data = ser.read(512)
                    if not data:
                        continue
                    # Drain any pending bytes
                    while True:
                        try:
                            more = ser.read(512)
                            if not more:
                                break
                            data += more
                        except Exception:
                            break
                    lines = data.decode("utf-8", errors="replace").split("\n")
                    for line_str in lines:
                        line_str = line_str.strip()
                        if not line_str or not line_str.startswith("{"):
                            continue
                        try:
                            msg = json.loads(line_str)
                        except json.JSONDecodeError:
                            continue
                        approval = msg.get("approval")
                        if approval == "yes":
                            req_id = msg.get("id", "?")
                            log(f"APPROVE from device (id={req_id[:12]}...) → sending 'Y'")
                            send_to_tmux(pane_target, "Y")
                        elif approval == "no":
                            req_id = msg.get("id", "?")
                            log(f"DENY from device (id={req_id[:12]}...) → sending 'N'")
                            send_to_tmux(pane_target, "N")
                except Exception as e:
                    err_str = str(e)
                    # ESP32 USB-Serial often returns "device reports readiness
                    # to read but returned no data" — a select() false positive.
                    # Don't close/reopen the port; just skip this poll cycle.
                    if "readiness to read" in err_str or "returned no data" in err_str:
                        continue
                    log(f"Serial read error: {e}")
                    try:
                        ser.close()
                    except Exception:
                        pass
                    ser = None
                    ser_fd = None
                    last_port = None

            elif fd == fifo_fd:
                # Read from hook scripts → forward to device
                try:
                    data = os.read(fifo_fd, 4096)
                    if data:
                        ser.write(data)
                except Exception:
                    pass


if __name__ == "__main__":
    main()
