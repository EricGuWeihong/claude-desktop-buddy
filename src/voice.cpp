#include "voice.h"
#include "ble_bridge.h"
#include <M5Unified.h>
#include <opus.h>
#include <string.h>

// Voice streams audio over USB serial and BLE NUS. The daemon reads from
// whichever transport is active (USB on Windows/Linux, BLE on macOS when
// no USB cable). Control commands (voice_enter/cancel) still use sendCmd
// so the daemon picks them up over both transports.
static void voiceSend(const char* json) {
  Serial.println(json);
  if (bleConnected()) {
    size_t n = strlen(json);
    bleWrite((const uint8_t*)json, n);
    bleWrite((const uint8_t*)"\n", 1);
  }
}

// Provided by main.cpp.
extern void sendCmd(const char* json);

VoiceMode voiceMode = VOICE_IDLE;
uint32_t  voiceModeEnteredMs = 0;
uint32_t  voiceErrorUntilMs  = 0;
char      voiceLastText[40]  = {0};

// 16 kHz / 16-bit / mono. 30 s = 960 KB — needs PSRAM. If PSRAM isn't
// available on this board variant, we fall back to a smaller heap-resident
// buffer so the feature still works (just with a shorter cap).
static const uint32_t SAMPLE_RATE   = 16000;
static const size_t   recBufSamples_PSRAM = 16000 * 30;
static const size_t   recBufSamples_HEAP  = 16000 * 3;   // 96 KB (3 s cap on non-PSRAM boards)

// Opus encoder settings.
static const int      OPUS_BITRATE  = 16000;   // 16 kbps — good quality speech
static const int      OPUS_FRAME_SAMPLES = 320; // 20 ms at 16 kHz

// Mic recording is double-buffered through the M5Unified Mic queue (2 slots).
// Each slot captures MIC_CHUNK_SAMPLES (40 ms = 2 opus frames), giving
// 80 ms of total in-queue buffering. With only 40 ms (2× 20 ms) of buffer,
// any main-loop tick longer than 40 ms (display flush, opus_encode burst,
// USB serial backpressure) starves the mic and audio is silently lost —
// observed as ~40% capture loss with 20 ms slots. 40 ms slots leave enough
// headroom for typical 50–80 ms ticks while still streaming at low latency.
// voiceTick keeps the queue topped up so the mic captures continuously
// between main-loop iterations.
static const int      MIC_CHUNK_SAMPLES = OPUS_FRAME_SAMPLES * 2;  // 640 = 40 ms

static int16_t* recBuf      = nullptr;
static size_t   recBufSamples = 0;
static size_t   recCount    = 0;          // samples completed (in recBuf, valid)
static size_t   queuedCount = 0;          // samples queued to mic (in flight + completed)
static int      pendingSlots = 0;         // our running tally of in-flight Mic slots (0..2)
static uint32_t recStartMs  = 0;

// Opus encoder handle + output buffer (max Opus frame = 1275 bytes).
static OpusEncoder* opusEnc   = nullptr;
static uint8_t      opusOutBuf[1275];

// Streaming-send state — runs during LISTENING so chunks go out in real-time.
static size_t   encCursor     = 0;     // next unencoded PCM sample
static int      sendChunkIdx  = 0;     // chunk sequence number
static uint32_t sendCrc       = 0xFFFFFFFFu; // CRC over *decoded* PCM
static bool     beginSent     = false;
static bool     endSent       = false;

// ----- helpers ------------------------------------------------------------

static uint32_t crc32_update(uint32_t crc, const uint8_t* data, size_t n) {
  for (size_t i = 0; i < n; i++) {
    crc ^= data[i];
    for (int b = 0; b < 8; b++) crc = (crc >> 1) ^ (0xEDB88320u & (uint32_t)(-(int32_t)(crc & 1u)));
  }
  return crc;
}

static const char B64A[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
static size_t b64_encode(const uint8_t* in, size_t n, char* out) {
  size_t o = 0;
  for (size_t i = 0; i < n; i += 3) {
    uint32_t v = (uint32_t)in[i] << 16;
    if (i + 1 < n) v |= (uint32_t)in[i+1] << 8;
    if (i + 2 < n) v |= in[i+2];
    out[o++] = B64A[(v >> 18) & 0x3F];
    out[o++] = B64A[(v >> 12) & 0x3F];
    out[o++] = (i + 1 < n) ? B64A[(v >> 6) & 0x3F] : '=';
    out[o++] = (i + 2 < n) ? B64A[v & 0x3F] : '=';
  }
  out[o] = 0;
  return o;
}

static const char* _switchToMic() {
  M5.Speaker.end();
  return M5.Mic.begin() ? nullptr : "Mic failed";
}
static void _switchToSpeaker() {
  M5.Mic.end();
  M5.Speaker.begin();
}

static void _resetSend() {
  encCursor = 0; sendChunkIdx = 0; sendCrc = 0xFFFFFFFFu;
  beginSent = false; endSent = false;
}

// Encode and stream one opus frame from the recording buffer.
// Returns true if a frame was encoded and sent.
//
// `allowPartial` is set only in the ANALYZING drain path so the trailing
// (<320 sample) sliver gets one final padded frame. During LISTENING we
// MUST refuse partial frames — opus frame_size MUST cover real audio only,
// otherwise the encoded chunk is part real + part zeros, which stretches
// the decoded audio and breaks downstream ASR.
static bool _streamOneFrame(bool allowPartial = false) {
  int available = recCount - encCursor;
  if (available <= 0) return false;
  if (!allowPartial && available < OPUS_FRAME_SAMPLES) return false;

  int n = available;
  if (n > OPUS_FRAME_SAMPLES) n = OPUS_FRAME_SAMPLES;

  int encodedLen = 0;
  if (opusEnc) {
    int16_t frameBuf[OPUS_FRAME_SAMPLES];
    for (int i = 0; i < OPUS_FRAME_SAMPLES; i++)
      frameBuf[i] = (i < n) ? recBuf[encCursor + i] : 0;

    encodedLen = opus_encode(opusEnc, frameBuf, OPUS_FRAME_SAMPLES,
                             opusOutBuf, sizeof(opusOutBuf));
  }

  if (encodedLen <= 0) {
    // Fallback: raw PCM b64.
    int16_t frameBuf[OPUS_FRAME_SAMPLES];
    for (int i = 0; i < OPUS_FRAME_SAMPLES; i++)
      frameBuf[i] = (i < n) ? recBuf[encCursor + i] : 0;
    size_t bytes = n * sizeof(int16_t);
    sendCrc = crc32_update(sendCrc, (const uint8_t*)frameBuf, bytes);
    static char b64[880];
    b64_encode((const uint8_t*)frameBuf, bytes, b64);
    static char json[940];
    snprintf(json, sizeof(json),
             "{\"cmd\":\"audio_chunk\",\"i\":%d,\"codec\":\"pcm_b64\",\"data\":\"%s\"}",
             sendChunkIdx, b64);
    voiceSend(json);
  } else {
    sendCrc = crc32_update(sendCrc, (const uint8_t*)opusOutBuf, encodedLen);
    static char b64[1720];
    b64_encode(opusOutBuf, encodedLen, b64);
    static char json[1800];
    snprintf(json, sizeof(json),
             "{\"cmd\":\"audio_chunk\",\"i\":%d,\"codec\":\"opus_b64\",\"data\":\"%s\",\"len\":%d}",
             sendChunkIdx, b64, encodedLen);
    voiceSend(json);
  }

  encCursor += n;
  sendChunkIdx++;
  return true;
}

// ----- public API ---------------------------------------------------------

void voiceInit() {
  if (recBuf) return;
  recBuf = (int16_t*)ps_malloc(recBufSamples_PSRAM * sizeof(int16_t));
  if (recBuf) {
    recBufSamples = recBufSamples_PSRAM;
  } else {
    // No PSRAM — fall back to internal heap with a shorter cap.
    recBuf = (int16_t*)malloc(recBufSamples_HEAP * sizeof(int16_t));
    if (recBuf) recBufSamples = recBufSamples_HEAP;
  }

  // Create opus encoder (lazy, once).
  if (!opusEnc) {
    int err;
    opusEnc = opus_encoder_create(SAMPLE_RATE, 1, OPUS_APPLICATION_VOIP, &err);
    if (opusEnc && err == OPUS_OK) {
      opus_encoder_ctl(opusEnc, OPUS_SET_BITRATE(OPUS_BITRATE));
      Serial.printf("[voice] opus encoder ok, heap=%uKB\n", ESP.getFreeHeap() / 1024);
    } else {
      Serial.printf("[voice] opus encoder failed (err=%d), heap=%uKB, using pcm_b64\n", err, ESP.getFreeHeap() / 1024);
      opusEnc = nullptr;  // encode will be skipped
    }
  }
}

bool voiceStartListening() {
  voiceInit();
  if (!recBuf) return false;
  recCount = 0;
  queuedCount = 0;
  pendingSlots = 0;
  encCursor = 0;
  sendChunkIdx = 0;
  sendCrc = 0xFFFFFFFFu;
  beginSent = false;
  endSent = false;
  recStartMs = millis();
  if (_switchToMic()) {
    _switchToSpeaker();
    return false;
  }
  // Prime the mic's 2-slot queue so capture is gap-free from frame 0.
  for (int i = 0; i < 2 && queuedCount + MIC_CHUNK_SAMPLES <= recBufSamples; i++) {
    if (M5.Mic.record(recBuf + queuedCount, MIC_CHUNK_SAMPLES, SAMPLE_RATE)) {
      queuedCount += MIC_CHUNK_SAMPLES;
      pendingSlots++;
    }
  }

  // Send begin header immediately — daemon can prepare Qwen connection now.
  char hdr[160];
  snprintf(hdr, sizeof(hdr),
    "{\"cmd\":\"audio_begin\",\"sr\":%u,\"bits\":16,\"ch\":1,\"codec\":\"opus_b64\"}",
    (unsigned)SAMPLE_RATE);
  voiceSend(hdr);

  voiceMode = VOICE_LISTENING;
  voiceModeEnteredMs = millis();
  return true;
}

void voiceRestartListening() {
  // Drop any in-flight slots by waiting them out; cheaper than juggling.
  // (Mic was paused/resumed by caller around this call.)
  while (M5.Mic.isRecording()) { delay(1); }
  recCount = 0;
  queuedCount = 0;
  pendingSlots = 0;
  encCursor = 0;
  sendChunkIdx = 0;
  sendCrc = 0xFFFFFFFFu;
  // beginSent stays true (header already sent).
  recStartMs = millis();
  voiceModeEnteredMs = millis();
  // Re-prime the queue.
  for (int i = 0; i < 2 && queuedCount + MIC_CHUNK_SAMPLES <= recBufSamples; i++) {
    if (M5.Mic.record(recBuf + queuedCount, MIC_CHUNK_SAMPLES, SAMPLE_RATE)) {
      queuedCount += MIC_CHUNK_SAMPLES;
      pendingSlots++;
    }
  }
}

void voiceFinishListening() {
  Serial.printf("[voice] finish listening, %u samples, heap=%uKB\n", (unsigned)recCount, ESP.getFreeHeap() / 1024);
  voiceMode = VOICE_ANALYZING;
  voiceModeEnteredMs = millis();
}

void voiceCancel() {
  if (voiceMode == VOICE_LISTENING) _switchToSpeaker();
  _resetSend();
  recCount = 0;
  queuedCount = 0;
  pendingSlots = 0;
  voiceMode = VOICE_IDLE;
}

size_t voiceSamples() { return recCount; }

uint32_t voiceListenMs() {
  if (voiceMode != VOICE_LISTENING) return 0;
  return millis() - recStartMs;
}

void voiceMicPause() {
  if (voiceMode != VOICE_LISTENING) return;
  _switchToSpeaker();
}

void voiceMicResume() {
  if (voiceMode != VOICE_LISTENING) return;
  _switchToMic();
}

void voiceOnAck(bool ok, const char* text) {
  if (voiceMode != VOICE_ANALYZING) return;
  if (ok) {
    if (text) {
      strncpy(voiceLastText, text, sizeof(voiceLastText) - 1);
      voiceLastText[sizeof(voiceLastText) - 1] = 0;
    } else {
      voiceLastText[0] = 0;
    }
    voiceMode = VOICE_REVIEW;
    voiceModeEnteredMs = millis();
  } else {
    voiceMode = VOICE_ERROR_FLASH;
    voiceErrorUntilMs = millis() + 1500;
  }
  _resetSend();
}

// ----- tick ---------------------------------------------------------------

// Reconcile our queued slots against the mic's actual state. Whatever the
// mic has finished (queued - inflight) becomes valid samples in recBuf.
static void _harvestMicSlots() {
  int inflight = (int)M5.Mic.isRecording();
  int completed = pendingSlots - inflight;
  if (completed > 0) {
    recCount += (size_t)completed * MIC_CHUNK_SAMPLES;
    pendingSlots = inflight;
  }
}

void voiceTick() {
  uint32_t now = millis();

  if (voiceMode == VOICE_LISTENING) {
    // Harvest any slots the mic finished since last tick.
    _harvestMicSlots();
    // Top up the queue so the mic always has a slot to fill — this is what
    // keeps capture continuous. record() blocks ONLY when both slots are
    // busy; with our budget it returns immediately while a slot is free.
    while (pendingSlots < 2 && queuedCount + MIC_CHUNK_SAMPLES <= recBufSamples) {
      if (!M5.Mic.record(recBuf + queuedCount, MIC_CHUNK_SAMPLES, SAMPLE_RATE)) break;
      queuedCount += MIC_CHUNK_SAMPLES;
      pendingSlots++;
    }
    // Drain enough frames per tick to stay ahead of the mic. The mic
    // generates ~25 chunks/sec (each chunk = 2 opus frames = 50 frames/sec
    // of audio). If we only encode 1 frame per main-loop tick (~20 fps) we
    // accumulate backlog and ANALYZING ends up draining for many seconds —
    // ASR feels like batch mode. 4 per tick keeps us ahead while bounding
    // the per-tick cost.
    for (int i = 0; i < 4 && _streamOneFrame(); i++) {}

    // Abort if both transports are gone — no point recording to nowhere.
    if (!bleConnected() && !Serial.isConnected()) {
      voiceCancel();
      return;
    }

    if (queuedCount + MIC_CHUNK_SAMPLES > recBufSamples) {
      voiceFinishListening();   // 30 s cap (queue can't grow further)
    }
    return;
  }

  if (voiceMode == VOICE_ANALYZING) {
    // Wait for any in-flight mic slots to drain into recBuf first.
    _harvestMicSlots();
    if (pendingSlots > 0) return;
    // Drain ALL remaining buffered frames in one tick — the mic is idle
    // now, no risk of starving capture, and we want Qwen to receive the
    // tail audio asap so .completed fires right after commit().
    if (encCursor < recCount) {
      while (_streamOneFrame(/*allowPartial=*/true)) {}
      return;
    }

    // Send audio_end with CRC so daemon knows audio is complete.
    if (!endSent) {
      _switchToSpeaker();
      char tail[64];
      uint32_t crc = sendCrc ^ 0xFFFFFFFFu;
      snprintf(tail, sizeof(tail), "{\"cmd\":\"audio_end\",\"crc\":%u}", (unsigned)crc);
      voiceSend(tail);
      endSent = true;
      voiceModeEnteredMs = now;

      // If neither transport is available, daemon never receives audio_end
      // and Qwen times out after 180s. Abort immediately.
      if (!bleConnected() && !Serial.isConnected()) {
        voiceMode = VOICE_ERROR_FLASH;
        voiceErrorUntilMs = now + 1500;
        _resetSend();
      }
      return;
    }

    // Wait for ack — 20 s budget after audio_end. Most ACKs come back in
    // <1 s; the long ceiling absorbs occasional Qwen latency or daemon GC
    // jitter so successful ASR doesn't get clobbered into VOICE_ERROR_FLASH.
    if (now - voiceModeEnteredMs > 20000) {
      voiceMode = VOICE_ERROR_FLASH;
      voiceErrorUntilMs = now + 1500;
      _resetSend();
    }
    return;
  }

  if (voiceMode == VOICE_REVIEW) {
    if (now - voiceModeEnteredMs > 30000) voiceMode = VOICE_IDLE;
    return;
  }

  if (voiceMode == VOICE_ERROR_FLASH) {
    if ((int32_t)(now - voiceErrorUntilMs) >= 0) voiceMode = VOICE_IDLE;
    return;
  }
}
