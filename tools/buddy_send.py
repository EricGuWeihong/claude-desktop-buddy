#!/usr/bin/env python3
"""Forward Claude Code hook events to a USB-connected Hardware Buddy.

Wiring: in ~/.claude/settings.json, register this script as the command
for PreToolUse / PostToolUse / Notification / Stop hooks. Claude Code pipes
a JSON payload on stdin describing the event; this script:

  1. Reads the payload (includes `transcript_path` — the session's JSONL)
  2. Scans the transcript and sums `output_tokens` across all assistant
     messages, then derives today's running total via a midnight-resetting
     baseline. We send DAILY (not lifetime cumulative) so cross-source
     switches (this script vs. another bridge feeding the same firmware)
     can't spike-credit lifetime totals as a single delta.
  3. Overlays event-specific state (running / waiting / msg / prompt) so
     the pet shows attention during Notification, done at Stop, etc.
  4. Writes one JSON line to /dev/cu.usbmodem* — the firmware's usb serial
     reader (data.h::_usbLine.feed) picks it up indistinguishably from BLE.

Failures are swallowed silently. A hook that blocks or errors would stall
Claude Code itself, and that's a much worse outcome than a dark buddy.

Usage (from settings.json):
  {"hooks": {"PostToolUse": [{"hooks":[{"type":"command",
     "command":"python3 /Users/YOU/AI/claude-desktop-buddy/tools/buddy_send.py"
  }]}]}}
"""
import os
import sys
import json
import datetime
import subprocess

# Persistent state tracks tokens so the `tokens` field sent to firmware
# is today's running total (resets at local midnight). Firmware treats
# the midnight drop as a bridge restart (stats.h bridge-restart branch)
# and resyncs baseline without crediting — correct: no tokens were
# actually spent at midnight. Sending DAILY (not cumulative) also bounds
# cross-source switches: when this script and another bridge feed the
# same firmware, each machine's daily counter is small enough that a
# resync-on-drop keeps spurious credit bounded by per-day usage rather
# than lifetime totals.
# - per_transcript[path] = last output_tokens count we've added (first
#   sight latches without crediting historical content)
# - total_counted = monotonic sum of all deltas; today's slice =
#   total_counted - today_baseline
STATE_PATH = os.path.expanduser("~/.claude/buddy_send_state.json")


def _read_event():
    try:
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}


def _load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
        if not isinstance(s, dict):
            raise ValueError
        s.setdefault("total_counted", 0)
        s.setdefault("per_transcript", {})
        s.setdefault("active_prompt", None)
        s.setdefault("today_date", None)
        s.setdefault("today_baseline", 0)
        return s
    except Exception:
        return {
            "total_counted": 0, "per_transcript": {}, "active_prompt": None,
            "today_date": None, "today_baseline": 0,
        }


def _save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def _sum_output_tokens(transcript_path):
    """Sum output_tokens across every assistant message in the JSONL.

    Uses a cached seek offset so only *new* lines are parsed on each hook
    fire. This reduces a multi-MB transcript scan from ~1s to <50ms.
    The offset is stored in state["per_transcript_pos"] as (inode, offset).
    If the file was rotated (different inode), we reset to position 0.
    """
    if not transcript_path:
        return 0
    try:
        st = os.stat(transcript_path)
        inode = st.st_ino
        file_size = st.st_size
    except Exception:
        return 0

    # Read from the global state (populated by caller).
    global _hook_state_for_tokens
    pos_map = _hook_state_for_tokens.get("per_transcript_pos", {})
    entry = pos_map.get(transcript_path, {})
    saved_inode = entry.get("inode")
    offset = entry.get("offset", 0)

    # File rotated (new inode) → restart from beginning.
    if saved_inode is not None and saved_inode != inode:
        offset = 0

    # If file shrank (truncated/rotated), restart.
    if offset > file_size:
        offset = 0

    total = 0
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            for line in f:
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                usage = (m.get("message") or {}).get("usage") or {}
                total += int(usage.get("output_tokens") or 0)
            # Save the new offset (end of file after reading all new lines).
            new_offset = f.tell()
    except FileNotFoundError:
        return 0
    except Exception:
        return 0

    # Persist offset back into the state map.
    pos_map[transcript_path] = {"inode": inode, "offset": new_offset}
    _hook_state_for_tokens["per_transcript_pos"] = pos_map

    return total


# Global so _sum_output_tokens can access and update the mutable state dict.
_hook_state_for_tokens = None


def _update_total(state, transcript_path, transcript_total):
    """Advance total_counted by the new output_tokens on this transcript.

    First-sight latch: a transcript_path we've never seen gets its current
    total recorded as the baseline WITHOUT crediting. This prevents a
    wiped state file (or a brand-new transcript that already has historical
    content from a prior run) from spike-crediting the entire history as
    if it just happened.
    """
    per = state["per_transcript"]
    if transcript_path not in per:
        per[transcript_path] = transcript_total
        return state["total_counted"]
    prev = int(per[transcript_path])
    if transcript_total < prev:
        # Log shrank — re-baseline for this path but keep total monotonic.
        per[transcript_path] = transcript_total
        return state["total_counted"]
    delta = transcript_total - prev
    per[transcript_path] = transcript_total
    state["total_counted"] = int(state["total_counted"]) + delta
    return state["total_counted"]


def _roll_today(state):
    """Snapshot today's baseline on local-date change.

    `tokens_today` is derived as total_counted - today_baseline. We snapshot
    BEFORE applying the current call's delta, so the delta observed on the
    first hook of a new day correctly counts toward that day.
    """
    today = datetime.date.today().isoformat()
    if state.get("today_date") != today:
        state["today_baseline"] = int(state.get("total_counted", 0))
        state["today_date"] = today


def _last_tool_use(transcript_path):
    """Read the transcript and return the most recent tool_use block.

    Returns (tool_name, hint_str) where hint_str is a concise, display-ready
    summary of the tool input (e.g. the bash command, file path, etc.).

    Scans the full transcript (not incremental) because approval context is
    only useful for the *last* tool_use, not for earlier ones.
    """
    if not transcript_path:
        return None, None

    last_name = None
    last_input = None

    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    msg = json.loads(line)
                except Exception:
                    continue

                role = msg.get("message", {}).get("role")

                # Format A: tool_use content blocks.
                if role == "assistant":
                    content = (msg.get("message") or {}).get("content")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                last_name = block.get("name")
                                last_input = block.get("input")

                # Format B: openai-style tool_calls array.
                tool_calls = (msg.get("message") or {}).get("tool_calls")
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        last_name = tc.get("function", {}).get("name")
                        raw = tc.get("function", {}).get("arguments", "{}")
                        try:
                            last_input = json.loads(raw) if isinstance(raw, str) else raw
                        except Exception:
                            last_input = raw
    except Exception:
        pass

    if not last_name:
        return None, None

    # Build a concise hint from the tool input.
    hint = _tool_hint(last_name, last_input)
    return last_name, hint


def _tool_hint(tool_name, tool_input):
    """Create a short, display-friendly hint from a tool's input dict."""
    if not isinstance(tool_input, dict):
        s = str(tool_input or "")[:100]
        return s if len(s) <= 43 else s[:40] + "..."

    # Known keys that make good one-line hints.
    for key in ("command", "file_path", "path"):
        if key in tool_input:
            return str(tool_input[key])[:43]

    # Generic: first value or JSON for small dicts.
    first = next(iter(tool_input.values()), "")
    if isinstance(first, str) and len(first) <= 43:
        return first
    s = json.dumps(tool_input, separators=(",", ":"))
    return s if len(s) <= 43 else s[:40] + "..."


def _build_payload(evt, tokens_today, state, transcript_path=""):
    """Shape a firmware-compatible JSON based on the hook event.

    Only Notification, PreToolUse, and Stop hooks are relevant —
    auto-approved tools don't trigger Notification but do fire PreToolUse.

    `tokens` carries today's running total, same value as `tokens_today`.
    Firmware's statsOnBridgeTokens tracks delta-since-last-packet; midnight
    rollover drops the value to ~0 and hits the bridge-restart branch, which
    resyncs baseline without credit. No cross-day double counting.
    """
    event = evt.get("hook_event_name", "")
    payload = {"tokens": tokens_today, "tokens_today": tokens_today}

    # Notification fires when approval is needed → set active_prompt.
    # Stop fires when session ends → clear active_prompt.
    ap = state.get("active_prompt")

    if event == "Notification":
        # Notification hook doesn't include tool_name or tool_input — scan
        # the transcript to find the most recent tool_use block that triggered
        # this approval. Fall back to "claude-code" / "Claude Code" if not found.
        name, hint = _last_tool_use(transcript_path)
        if not name:
            name = "claude-code"
        if not hint:
            hint = "Claude Code"
        ap = {
            "id":   (evt.get("session_id", "cli") or "cli")[:39],
            "tool": name[:19],
            "hint": hint[:43],
        }
        state["active_prompt"] = ap
        payload.update({
            "running": 1, "waiting": 1,
            "msg": "needs input",
            "prompt": ap,
        })
    elif event == "PreToolUse":
        # Tool about to execute → previous prompt was resolved (either
        # approved in CLI or auto-approved). Clear it immediately so
        # buddy doesn't show a stale approval UI.
        if ap:
            state["active_prompt"] = None
            payload.update({
                "running": 1,
                "msg": "working",
                "promptResolving": ap["id"],
            })
        else:
            payload["running"] = 1
    elif event == "Stop":
        # Session ended. Clear active_prompt so approval UI disappears —
        # don't preserve it; the default JSON key omission already clears
        # promptId in the firmware.
        if ap:
            state["active_prompt"] = None
        payload.update({
            "running": 0, "waiting": 0,
            "completed": True,
            "msg": "done",
        })
    else:
        # Unknown event (SessionStart, UserPromptSubmit, etc.) — emit a
        # minimal update but preserve any active prompt.
        if ap:
            payload["prompt"] = ap

    return payload


def _discover_tmux_session():
    """If running inside a tmux pane, return (session_name, pane_index).

    Runs inside the Claude Code hook process, so this accurately identifies
    the tmux session where Claude is running — far more reliable than
    having the daemon guess via process matching later.
    """
    tmux = os.environ.get("TMUX", "")
    if not tmux:
        return None, None
    # TMUX env var format: /tmp/tmux-NNN/default,NNN,N
    # Capture pane_id (e.g. %0) — this is stable across pane splits/closes,
    # unlike pane_index which shifts. tmux send-keys -t %N always targets
    # the correct pane regardless of its current index position.
    try:
        out = subprocess.check_output(
            ["tmux", "display-message", "-p",
             "#{session_name}:#{window_index}:#{pane_index}:#{pane_id}"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        parts = out.split(":")
        if len(parts) == 4:
            session, window, index, pane_id = parts
            return session, pane_id
    except Exception:
        return None, None


def _save_tmux_target(session, pane_id):
    """Persist the detected tmux session/pane_id for the daemon to read.

    Uses pane_id (e.g. %1) which is stable across pane splits/closes,
    unlike pane_index which shifts.
    """
    if not session:
        return
    target_path = os.path.expanduser("~/.claude/buddy_tmux_target.json")
    try:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump({"session": session, "pane_id": pane_id, "pid": os.getpid()}, f)
    except Exception:
        pass


def _load_tmux_target():
    """Read previously saved tmux target from the hook script.

    This lets the daemon target the correct tmux session even if process
    matching fails — the hook script runs inside Claude's tmux pane and
    captures the session name accurately.
    """
    target_path = os.path.expanduser("~/.claude/buddy_tmux_target.json")
    try:
        with open(target_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("session"), data.get("pane", "0")
    except Exception:
        return None, None


def _send(payload):
    """Write one JSON line to the daemon FIFO.

    The daemon owns the USB serial port exclusively and forwards FIFO data
    to the device. Writing directly to the serial port causes a conflict
    ("device reports readiness to read but returns no data").

    If the daemon isn't running or the FIFO doesn't exist, silently no-op.
    """
    fifo = os.path.expanduser("~/.claude/buddy_send_fifo")
    if not os.path.exists(fifo):
        return
    line = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        # Non-blocking open so a stopped daemon won't hang the hook.
        # O_NONBLOCK on FIFO: open() succeeds immediately even with no
        # reader, but write() would fail ENXIO — which we catch and
        # swallow (dark buddy > stalled Claude Code).
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except (OSError, IOError):
        pass


def main():
    global _hook_state_for_tokens

    evt = _read_event()
    transcript = evt.get("transcript_path") or ""

    state = _load_state()
    _hook_state_for_tokens = state  # link for incremental token scanning
    transcript_total = _sum_output_tokens(transcript)
    # Roll today's baseline BEFORE adding this call's delta, so the delta
    # at the first hook after midnight is credited to the new day.
    _roll_today(state)
    total_counted = _update_total(state, transcript, transcript_total)
    tokens_today = max(0, total_counted - int(state.get("today_baseline", 0)))
    # _build_payload mutates state["active_prompt"]; save after.
    payload = _build_payload(evt, tokens_today, state, transcript)
    _save_state(state)

    # Record the tmux session we're running in — the daemon reads this
    # to know where to inject approval responses. Far more reliable than
    # the daemon's own process-based detection.
    session, pane = _discover_tmux_session()
    if session:
        _save_tmux_target(session, pane)

    _send(payload)


if __name__ == "__main__":
    main()
