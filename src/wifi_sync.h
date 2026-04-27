#pragma once
// WiFi + SNTP time sync. Credentials (SSID/pass/tz) are pushed over BLE
// once via tools/push_wifi.py and persisted to NVS, so subsequent boots
// can sync time without Claude Desktop running — the device just needs
// USB power and to be in range of the known WiFi network.
//
// Non-blocking: kicks off the WiFi connect asynchronously, polled from
// the main loop. If creds are missing, WiFi is down, or NTP fails, the
// rest of the firmware (BLE bridge, character, clock-from-BLE) keeps
// working untouched. A fresh BLE {"time":...} push still takes over if
// you ever need to override.
//
// TZ is stored as a POSIX TZ environment variable so localtime_r gives
// local wall-clock components. Sign is POSIX-inverted: UTC+8 → "UTC-8".

#include <Arduino.h>
#include <WiFi.h>
#include <Preferences.h>
#include <sys/time.h>
#include <time.h>

// Build a POSIX TZ string for the given offset. POSIX inverts the sign:
// UTC+8 (China, tz_offset=28800) → "UTC-8".
inline void _wifiBuildTzStr(int32_t tz_offset_sec, char* buf, size_t n) {
  int total = -tz_offset_sec;
  int h = total / 3600;
  int m = (total % 3600);
  if (m < 0) m = -m;
  m /= 60;
  if (m == 0) snprintf(buf, n, "UTC%+d", h);
  else        snprintf(buf, n, "UTC%+d:%02d", h, m);
}

// Applied by data.h's {"time":...} handler. For the WiFi/NTP path we
// use configTzTime instead (which sets TZ _and_ starts SNTP in one call,
// avoiding configTime's side-effect of clobbering the TZ env var).
inline void _wifiApplyTz(int32_t tz_offset_sec) {
  char buf[16];
  _wifiBuildTzStr(tz_offset_sec, buf, sizeof(buf));
  setenv("TZ", buf, 1);
  tzset();
}

// State machine — kept simple, one step per loop() tick.
enum WifiSyncState : uint8_t {
  WSS_IDLE,        // no creds / disabled — do nothing
  WSS_CONNECTING,  // WiFi.begin() issued, waiting for link
  WSS_NTP_WAIT,    // WiFi up, configTime fired, polling for sync
  WSS_DONE,        // time synced, _rtcValid = true
  WSS_FAILED,      // give up until reboot or new creds
};

static WifiSyncState _wssState = WSS_IDLE;
static uint32_t      _wssStartMs = 0;
static uint32_t      _wssNextPollMs = 0;
static String        _wssSsid, _wssPass;
static int32_t       _wssTz = 0;
static bool          _wssHasCreds = false;

// Defined in data.h — flipping this true flips on the clock face.
extern bool _rtcValid;

static void _wssLoadNvs() {
  Preferences p;
  if (!p.begin("wifi", true)) { _wssHasCreds = false; return; }
  _wssSsid = p.getString("ssid", "");
  _wssPass = p.getString("pass", "");
  _wssTz   = p.getInt("tz", 0);
  p.end();
  _wssHasCreds = _wssSsid.length() > 0;
  if (_wssHasCreds) _wifiApplyTz(_wssTz);
}

static void _wssSaveNvs(const char* ssid, const char* pass, int32_t tz) {
  Preferences p;
  if (!p.begin("wifi", false)) return;
  p.putString("ssid", ssid);
  p.putString("pass", pass);
  p.putInt("tz", tz);
  p.end();
  _wssSsid = ssid; _wssPass = pass; _wssTz = tz;
  _wssHasCreds = _wssSsid.length() > 0;
  _wifiApplyTz(tz);
}

// Called from main setup() after BLE is up. Reads NVS, kicks off connect
// if creds exist. Safe to call before/after BLE init.
inline void wifiSyncInit() {
  _wssLoadNvs();
  if (!_wssHasCreds) { _wssState = WSS_IDLE; return; }
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(true);  // coexist with BLE radio
  WiFi.begin(_wssSsid.c_str(), _wssPass.c_str());
  _wssState = WSS_CONNECTING;
  _wssStartMs = millis();
  _wssNextPollMs = millis() + 500;
  Serial.printf("[wifi] connecting to %s\n", _wssSsid.c_str());
}

// Called from data.h when a {"wifi":{...}} payload arrives. Saves to
// NVS and (re)starts the connect state machine with the new creds.
inline void wifiSyncApplyCreds(const char* ssid, const char* pass, int32_t tz) {
  _wssSaveNvs(ssid, pass, tz);
  if (WiFi.status() == WL_CONNECTED) WiFi.disconnect(true);
  if (!_wssHasCreds) { _wssState = WSS_IDLE; return; }
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(true);
  WiFi.begin(_wssSsid.c_str(), _wssPass.c_str());
  _wssState = WSS_CONNECTING;
  _wssStartMs = millis();
  _wssNextPollMs = millis() + 500;
  Serial.printf("[wifi] new creds, connecting to %s\n", ssid);
}

// Called every main loop tick. Cheap — returns immediately unless there's
// a state transition due. 20s cap on connect, 10s cap on NTP sync.
inline void wifiSyncPoll() {
  uint32_t now = millis();
  if ((int32_t)(now - _wssNextPollMs) < 0) return;
  _wssNextPollMs = now + 500;

  switch (_wssState) {
    case WSS_IDLE:
    case WSS_DONE:
    case WSS_FAILED:
      return;

    case WSS_CONNECTING: {
      wl_status_t s = WiFi.status();
      if (s == WL_CONNECTED) {
        Serial.printf("[wifi] up, ip=%s\n", WiFi.localIP().toString().c_str());
        // Use configTzTime, NOT configTime — configTime(0, 0, ...)
        // clobbers TZ to "GMT0" and makes localtime_r return UTC.
        // configTzTime sets the POSIX TZ string + SNTP in one call.
        char tzbuf[16];
        _wifiBuildTzStr(_wssTz, tzbuf, sizeof(tzbuf));
        configTzTime(tzbuf, "ntp.aliyun.com", "cn.pool.ntp.org");
        Serial.printf("[wifi] ntp start, tz=%s\n", tzbuf);
        _wssState = WSS_NTP_WAIT;
        _wssStartMs = now;
        return;
      }
      if (now - _wssStartMs > 20000) {
        Serial.println("[wifi] connect timeout, giving up");
        WiFi.disconnect(true);
        _wssState = WSS_FAILED;
      }
      return;
    }

    case WSS_NTP_WAIT: {
      // time() < 2020-01-01 means clock is still at the epoch — SNTP
      // hasn't delivered yet. 1577836800 = 2020-01-01 UTC.
      time_t t = time(nullptr);
      if (t > 1577836800) {
        _rtcValid = true;
        _wssState = WSS_DONE;
        struct tm lt; localtime_r(&t, &lt);
        Serial.printf("[wifi] ntp synced: %04d-%02d-%02d %02d:%02d:%02d\n",
                      lt.tm_year + 1900, lt.tm_mon + 1, lt.tm_mday,
                      lt.tm_hour, lt.tm_min, lt.tm_sec);
        return;
      }
      if (now - _wssStartMs > 10000) {
        Serial.println("[wifi] ntp timeout");
        _wssState = WSS_FAILED;
      }
      return;
    }
  }
}

// For INFO page / debugging.
inline const char* wifiSyncStateName() {
  switch (_wssState) {
    case WSS_IDLE:       return "idle";
    case WSS_CONNECTING: return "connecting";
    case WSS_NTP_WAIT:   return "ntp-wait";
    case WSS_DONE:       return "synced";
    case WSS_FAILED:     return "failed";
  }
  return "?";
}
