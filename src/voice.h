// Push-to-talk voice input. State machine + PSRAM audio buffer + chunked
// Opus encoding for fast serial/BLE transfer (~16kbps, 15x smaller than PCM).
// Owned by main.cpp's loop; transitions are driven by physical buttons there.
// Host-side ack feeds back via voiceOnAck().
#pragma once
#include <Arduino.h>
#include <stdint.h>
#include <stddef.h>

enum VoiceMode : uint8_t {
  VOICE_IDLE = 0,
  VOICE_LISTENING,    // mic is active, samples accumulating in recBuf
  VOICE_ANALYZING,    // mic released; encoding PCM→Opus, streaming to host
  VOICE_REVIEW,       // host ack'd; awaiting A (Enter) or B (Cancel)
  VOICE_ERROR_FLASH,  // brief "Voice failed" overlay before returning to IDLE
};

extern VoiceMode voiceMode;
extern uint32_t  voiceModeEnteredMs;
extern uint32_t  voiceErrorUntilMs;
extern char      voiceLastText[40];   // truncated transcript for HUD

void   voiceInit();
bool   voiceStartListening();      // speaker→mic; clear buffer
void   voiceRestartListening();    // discard buffer, stay in LISTENING (with beep handled inline)
void   voiceFinishListening();     // mic→speaker; transition to ANALYZING
void   voiceCancel();              // any state → IDLE (releases mic if held)
void   voiceTick();                // call every loop iteration
size_t voiceSamples();
uint32_t voiceListenMs();          // ms recorded so far (for HUD)

// Pause/resume mic so caller can briefly use the speaker (e.g. for a beep).
// No-op outside VOICE_LISTENING. The ~140 ms gap drops a sliver of audio,
// acceptable only at restart-discard time when the buffer is being thrown
// away anyway.
void   voiceMicPause();
void   voiceMicResume();

// Called by data.h when {"ack":"voice", ...} arrives.
void   voiceOnAck(bool ok, const char* text);
