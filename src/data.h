#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include <sys/time.h>     // settimeofday / struct timeval (software RTC)
#include "ble_bridge.h"
#include "wifi_sync.h"
#include "xfer.h"

struct TamaState {
  uint8_t  sessionsTotal;
  uint8_t  sessionsRunning;
  uint8_t  sessionsWaiting;
  bool     recentlyCompleted;
  uint32_t tokensToday;
  uint32_t lastUpdated;
  char     msg[24];
  bool     connected;
  char     lines[8][92];
  uint8_t  nLines;
  uint16_t lineGen;          // bumps when lines change — lets UI reset scroll
  char     promptId[40];     // pending permission request ID; empty = no prompt
  char     promptTool[20];
  char     promptHint[44];
};

// ---------------------------------------------------------------------------
// Three modes, checked in priority order:
//   demo   → auto-cycle fake scenarios every 8s, ignore live data
//   live   → JSON arrived in the last 10s over USB or BT
//   asleep → no data, all zeros, "No Claude connected"
// ---------------------------------------------------------------------------

static uint32_t _lastLiveMs = 0;
static uint32_t _lastBtByteMs = 0;   // hasClient() lies; track actual BT traffic
static uint32_t _lastDaemonMs = 0;   // heartbeat from CLI daemon
static uint32_t _lastUsbMs = 0;      // last USB data (hooks + heartbeat)
static char     _daemonOs[12] = "";   // "macOS", "Windows", "Linux"
static char     _daemonPort[20] = ""; // e.g. "/dev/cu.usbmodem1101"
static uint32_t _daemonConnectMs = 0; // first heartbeat timestamp (uptime origin)
static uint32_t _btConnectMs = 0;     // first BLE data timestamp (uptime origin)
static bool     _demoMode   = false;

// Accessors for main.cpp to read daemon metadata (static vars aren't shared
// across translation units).
inline uint32_t dataDaemonMs()    { return _lastDaemonMs; }
inline const char* dataDaemonOs() { return _daemonOs; }
inline const char* dataDaemonPort() { return _daemonPort; }
inline uint32_t dataBtByteMs()    { return _lastBtByteMs; }
inline uint32_t dataUsbMs()       { return _lastUsbMs; }
inline uint32_t dataDaemonConnectMs() { return _daemonConnectMs; }
inline uint32_t dataBtConnectMs()     { return _btConnectMs; }
static uint8_t  _demoIdx    = 0;
static uint32_t _demoNext   = 0;

struct _Fake { const char* n; uint8_t t,r,w; bool c; uint32_t tok; };
static const _Fake _FAKES[] = {
  {"asleep",0,0,0,false,0}, {"one idle",1,0,0,false,12000},
  {"busy",4,3,0,false,89000}, {"attention",2,1,1,false,45000},
  {"completed",1,0,0,true,142000},
};

inline void dataSetDemo(bool on) {
  _demoMode = on;
  if (on) { _demoIdx = 0; _demoNext = millis(); }
}
inline bool dataDemo() { return _demoMode; }

inline bool dataConnected() {
  return _lastLiveMs != 0 && (millis() - _lastLiveMs) <= 30000;
}

inline bool dataBtActive() {
  // Desktop's idle keepalive is ~10s; give it 1.5x headroom.
  return _lastBtByteMs != 0 && (millis() - _lastBtByteMs) <= 15000;
}

inline const char* dataScenarioName() {
  if (_demoMode) return _FAKES[_demoIdx].n;
  if (dataConnected()) return dataBtActive() ? "bt" : "usb";
  return "none";
}

// Active link info — picks the most recently active transport, prioritizing
// the daemon heartbeat (CLI signal) over raw data freshness. This prevents
// Desktop BLE heartbeats from masking an active USB daemon connection.
enum LinkTransport { LINK_NONE, LINK_USB, LINK_BLE, LINK_WIFI };

inline LinkTransport dataLinkTransport() {
  if (_demoMode) return LINK_NONE;
  // Daemon heartbeat is the authoritative CLI signal — if seen recently,
  // the transport from the heartbeat wins (with BLE override when BT data
  // is even fresher).
  if (_lastDaemonMs != 0 && (millis() - _lastDaemonMs) <= 30000) {
    // If BT data is fresher than USB, CLI is likely via BT (Win/BT daemon)
    bool btFresher = (_lastBtByteMs >= _lastUsbMs);
    return btFresher ? LINK_BLE : LINK_USB;
  }
  // No daemon heartbeat — pick whichever transport has fresher data.
  // Without daemon signal, this could be Desktop BLE or a freshly started
  // USB daemon whose first heartbeat hasn't arrived yet.
  bool btFresher = (_lastBtByteMs >= _lastUsbMs);
  return btFresher ? LINK_BLE : LINK_NONE;
}

inline const char* dataLinkInfo() {
  LinkTransport t = dataLinkTransport();
  if (t == LINK_BLE) {
    // Could be Desktop BLE or CLI via BLE — we can't distinguish from
    // data alone; Desktop doesn't send a daemon heartbeat.
    if (_lastDaemonMs != 0 && (millis() - _lastDaemonMs) <= 30000) {
      return "CLI via BT";
    }
    return "Desktop via BT";
  }
  if (t == LINK_USB) return "CLI via USB";
  if (t == LINK_WIFI) return "CLI via WiFi";
  return "none";
}

// Set true once the bridge sends a time sync — until then the RTC may
// hold whatever was on the coin cell (or 2000-01-01 if it lost power).
// Non-static so wifi_sync.h can flip it from the NTP path (single-TU
// header, still resolves in main.cpp).
bool _rtcValid = false;
inline bool dataRtcValid() { return _rtcValid; }

static void _applyJson(const char* line, TamaState* out) {
  JsonDocument doc;
  if (deserializeJson(doc, line)) return;
  if (xferCommand(doc)) { _lastLiveMs = millis(); return; }

  // {"daemon":1} heartbeat from CLI daemon — marks daemon as alive without
  // triggering any other state change. Includes "transport" to identify the
  // link medium (usb/bt/wifi).
  if (doc["daemon"].is<unsigned int>()) {
    uint32_t now = millis();
    // Track first heartbeat as connection start time.
    if (_daemonConnectMs == 0) _daemonConnectMs = now;
    _lastDaemonMs = now;
    // Track transport from heartbeat so we know which link the CLI daemon
    // is using. Default to USB for legacy daemons that don't send it.
    const char* tr = doc["transport"];
    if (tr && strcmp(tr, "bt") == 0) {
      if (_btConnectMs == 0) _btConnectMs = now;
      _lastBtByteMs = now;
    }
    else if (tr && strcmp(tr, "wifi") == 0) {} // WiFi tracked separately
    else _lastUsbMs = now;
    // Capture OS and port for display on TRANSPORT info page.
    const char* os = doc["os"];
    if (os) { strncpy(_daemonOs, os, sizeof(_daemonOs)-1); _daemonOs[sizeof(_daemonOs)-1]=0; }
    const char* port = doc["port"];
    if (port) { strncpy(_daemonPort, port, sizeof(_daemonPort)-1); _daemonPort[sizeof(_daemonPort)-1]=0; }
    return;
  }
  if (doc["reset"].is<unsigned int>()) {
    out->promptId[0] = 0; out->promptTool[0] = 0; out->promptHint[0] = 0;
    out->sessionsTotal = 0; out->sessionsRunning = 0; out->sessionsWaiting = 0;
    out->recentlyCompleted = false; out->lastUpdated = millis();
    strncpy(out->msg, doc["msg"] ? doc["msg"].as<const char*>() : "ready", sizeof(out->msg)-1);
    out->msg[sizeof(out->msg)-1] = 0;
    _lastLiveMs = millis();
    _daemonConnectMs = 0;  // daemon restart — reset uptime
    _btConnectMs = 0;
    return;
  }

  // Bridge sends {"time":[epoch_sec, tz_offset_sec]}. Store real UTC
  // via settimeofday, apply tz_offset as POSIX TZ env so localtime_r
  // returns local wall-clock. M5StickS3 has no BM8563 RTC; the ESP32
  // software RTC is the source of truth.
  JsonArray t = doc["time"];
  if (!t.isNull() && t.size() == 2) {
    time_t utc = (time_t)t[0].as<uint32_t>();
    int32_t tz = (int32_t)t[1];
    struct timeval tv = { .tv_sec = utc, .tv_usec = 0 };
    settimeofday(&tv, nullptr);
    _wifiApplyTz(tz);
    extern uint32_t _clkLastRead;
    _clkLastRead = 0;   // force re-read so _clkDt and _rtcValid agree
    _rtcValid = true;
    _lastLiveMs = millis();
    return;
  }

  // {"wifi":{"ssid":"...","pass":"...","tz":<sec>}} persists credentials
  // to NVS and kicks off a (re)connect. Subsequent boots sync via NTP
  // automatically — no Claude Desktop needed for the clock to work.
  JsonObject w = doc["wifi"];
  if (!w.isNull()) {
    const char* ssid = w["ssid"] | "";
    const char* pass = w["pass"] | "";
    int32_t tz       = w["tz"]   | 0;
    if (ssid[0]) {
      wifiSyncApplyCreds(ssid, pass, tz);
      _lastLiveMs = millis();
      return;
    }
  }

  out->sessionsTotal     = doc["total"]     | out->sessionsTotal;
  out->sessionsRunning   = doc["running"]   | out->sessionsRunning;
  out->sessionsWaiting   = doc["waiting"]   | out->sessionsWaiting;
  out->recentlyCompleted = doc["completed"] | false;
  uint32_t bridgeTokens = doc["tokens"] | 0;
  if (doc["tokens"].is<uint32_t>()) statsOnBridgeTokens(bridgeTokens);
  out->tokensToday = doc["tokens_today"] | out->tokensToday;
  const char* m = doc["msg"];
  if (m) { strncpy(out->msg, m, sizeof(out->msg)-1); out->msg[sizeof(out->msg)-1]=0; }
  JsonArray la = doc["entries"];
  if (!la.isNull()) {
    uint8_t n = 0;
    for (JsonVariant v : la) {
      if (n >= 8) break;
      const char* s = v.as<const char*>();
      strncpy(out->lines[n], s ? s : "", 91); out->lines[n][91]=0;
      n++;
    }
    if (n != out->nLines || (n > 0 && strcmp(out->lines[n-1], out->msg) != 0)) {
      out->lineGen++;
    }
    out->nLines = n;
  }
  JsonObject pr = doc["prompt"];
  if (!pr.isNull()) {
    const char* pid = pr["id"]; const char* pt = pr["tool"]; const char* ph = pr["hint"];
    strncpy(out->promptId,   pid ? pid : "", sizeof(out->promptId)-1);   out->promptId[sizeof(out->promptId)-1]=0;
    strncpy(out->promptTool, pt  ? pt  : "", sizeof(out->promptTool)-1); out->promptTool[sizeof(out->promptTool)-1]=0;
    strncpy(out->promptHint, ph  ? ph  : "", sizeof(out->promptHint)-1); out->promptHint[sizeof(out->promptHint)-1]=0;
  } else {
    out->promptId[0] = 0; out->promptTool[0] = 0; out->promptHint[0] = 0;
  }
  // {"promptResolving":"<id>"} — CLI resolved a prompt (approved/denied
  // in terminal or auto-approved). Match by ID so stale prompts disappear
  // from buddy immediately, without waiting for RESPONSE_TIMEOUT_MS.
  const char* resolving = doc["promptResolving"];
  if (resolving && strcmp(resolving, out->promptId) == 0) {
    out->promptId[0] = 0; out->promptTool[0] = 0; out->promptHint[0] = 0;
  }
  out->lastUpdated = millis();
  _lastLiveMs = millis();
}

template<size_t N>
struct _LineBuf {
  char buf[N];
  uint16_t len = 0;
  void feed(Stream& s, TamaState* out) {
    while (s.available()) {
      char c = s.read();
      if (c == '\n' || c == '\r') {
        if (len > 0) { buf[len]=0; if (buf[0]=='{') _applyJson(buf, out); len=0; }
      } else if (len < N-1) {
        buf[len++] = c;
      }
    }
  }
};

static _LineBuf<1024> _usbLine, _btLine;

inline void dataPoll(TamaState* out) {
  uint32_t now = millis();

  if (_demoMode) {
    if (now >= _demoNext) { _demoIdx = (_demoIdx + 1) % 5; _demoNext = now + 8000; }
    const _Fake& s = _FAKES[_demoIdx];
    out->sessionsTotal=s.t; out->sessionsRunning=s.r; out->sessionsWaiting=s.w;
    out->recentlyCompleted=s.c; out->tokensToday=s.tok; out->lastUpdated=now;
    out->connected = true;
    snprintf(out->msg, sizeof(out->msg), "demo: %s", s.n);
    return;
  }

  // Feed USB serial data into the line buffer — any parsed JSON line
  // (including daemon heartbeat) has already updated _lastUsbMs inside
  // _applyJson, so we just mark USB as active when Serial has data.
  if (Serial.available()) {
    _lastUsbMs = millis();
    _usbLine.feed(Serial, out);
  }
  // BLE ring buffer is drained manually since it's not a Stream.
  while (bleAvailable()) {
    int c = bleRead();
    if (c < 0) break;
    uint32_t now = millis();
    if (_btConnectMs == 0) _btConnectMs = now;
    _lastBtByteMs = now;
    if (c == '\n' || c == '\r') {
      if (_btLine.len > 0) {
        _btLine.buf[_btLine.len] = 0;
        if (_btLine.buf[0] == '{') _applyJson(_btLine.buf, out);
        _btLine.len = 0;
      }
    } else if (_btLine.len < sizeof(_btLine.buf) - 1) {
      _btLine.buf[_btLine.len++] = (char)c;
    }
  }

  out->connected = dataConnected();
  if (!out->connected) {
    // CLI daemon heartbeat keeps the link alive even when hooks aren't firing
    // (e.g. idle between tool calls). Keep session state from last hook.
    if (_lastDaemonMs != 0 && (millis() - _lastDaemonMs) <= 30000) {
      strncpy(out->msg, dataLinkInfo(), sizeof(out->msg)-1);
      out->msg[sizeof(out->msg)-1]=0;
    } else {
      out->sessionsTotal=0; out->sessionsRunning=0; out->sessionsWaiting=0;
      out->recentlyCompleted=false; out->lastUpdated=now;
      strncpy(out->msg, "No Claude connected", sizeof(out->msg)-1);
      out->msg[sizeof(out->msg)-1]=0;
    }
  }
}
