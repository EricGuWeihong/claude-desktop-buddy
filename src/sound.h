// Sound effects for buddy device.
// Uses M5Unified's playWav() to stream WAV data from flash.
#pragma once
#include <M5Unified.h>
#include <Arduino.h>
#include "cry.h"

static bool _cryPlaying = false;
static bool _levelupPlaying = false;
static bool _notifyPlaying = false;

// Play the embedded happy cry WAV. Non-blocking.
static void playHappyCry() {
  if (!settings().sound) return;
  if (_cryPlaying) return;
  M5.Speaker.playWav(cry_happy, CRY_HAPPY_LEN);
  _cryPlaying = true;
}

// Play the embedded level-up WAV. Non-blocking.
static void playLevelUp() {
  if (!settings().sound) return;
  if (_levelupPlaying) return;
  M5.Speaker.playWav(cry_levelup, CRY_LEVELUP_LEN);
  _levelupPlaying = true;
}

// Play the embedded notification WAV. Non-blocking.
static void playNotify() {
  if (!settings().sound) return;
  if (_notifyPlaying) return;
  M5.Speaker.playWav(cry_notify, CRY_NOTIFY_LEN);
  _notifyPlaying = true;
}

// Call every loop tick to check if playback finished.
static void soundTick() {
  if (_cryPlaying && !M5.Speaker.isPlaying()) _cryPlaying = false;
  if (_levelupPlaying && !M5.Speaker.isPlaying()) _levelupPlaying = false;
  if (_notifyPlaying && !M5.Speaker.isPlaying()) _notifyPlaying = false;
}
