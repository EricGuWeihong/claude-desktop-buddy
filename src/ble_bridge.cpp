#include "ble_bridge.h"
#include <NimBLEDevice.h>
#include <NimBLEServer.h>
#include <NimBLEUtils.h>
#include <Arduino.h>
#include <string.h>

// Nordic UART Service UUIDs
#define NUS_SERVICE_UUID "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
#define NUS_RX_UUID      "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
#define NUS_TX_UUID      "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

static const size_t RX_CAP = 2048;
static uint8_t  rxBuf[RX_CAP];
static volatile size_t rxHead = 0;
static volatile size_t rxTail = 0;

static NimBLEServer*         server = nullptr;
static NimBLECharacteristic* txChar = nullptr;
static NimBLECharacteristic* rxChar = nullptr;
static volatile bool         connected = false;
static volatile bool         secure = false;
static volatile uint32_t     passkey = 0;
static volatile uint16_t     mtu = 23;
static NimBLEAdvertising*    adv = nullptr;

// Non-blocking BLE transmit queue. Voice and control data are queued here
// and drained incrementally each loop iteration — no delay() blocking, so
// the main loop stays responsive for mic polling and incoming data.
static const size_t TX_Q_CAP = 32768;  // 32 KB — holds ~30 s of voice burst at MTU=515
static uint8_t  txQ[TX_Q_CAP];
static volatile size_t txQHead = 0;    // write pointer (producer)
static volatile size_t txQTail = 0;    // read pointer (consumer)

static size_t txQUsed() {
  return (txQHead + TX_Q_CAP - txQTail) % TX_Q_CAP;
}
static size_t txQFree() {
  return TX_Q_CAP - 1 - txQUsed();     // 1 byte sentinel
}
static void rxPush(const uint8_t* p, size_t n) {
  for (size_t i = 0; i < n; i++) {
    size_t next = (rxHead + 1) % RX_CAP;
    if (next == rxTail) return;
    rxBuf[rxHead] = p[i];
    rxHead = next;
  }
}

class RxCallbacks : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* c) override {
    std::string v = c->getValue();
    if (!v.empty()) rxPush((const uint8_t*)v.data(), v.size());
  }
};

class ServerCallbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer* s, ble_gap_conn_desc* desc) override {
    connected = true;
    Serial.printf("[ble] connected (handle=%u enc=%d)\n",
                  desc->conn_handle, desc->sec_state.encrypted);
    // Force pairing immediately. NimBLE's auto-created CCCD has no
    // encryption requirement, so subscribing to TX won't trigger pairing.
    // Starting security here ensures the passkey prompt appears right away.
    // For bonded reconnections this encrypts via stored LTK (no passkey).
    NimBLEDevice::startSecurity(desc->conn_handle);
  }
  void onDisconnect(NimBLEServer* s) override {
    connected = false;
    secure = false;
    passkey = 0;
    mtu = 23;
    txQHead = 0; txQTail = 0;
    Serial.println("[ble] disconnected");
    adv->start();
  }
  void onMTUChange(uint16_t newMtu, ble_gap_conn_desc* desc) override {
    mtu = newMtu;
    Serial.printf("[ble] mtu=%u\n", mtu);
  }
  // Called by NimBLE for BLE_SM_IOACT_DISP (passkey display) and
  // BLE_SM_IOACT_INPUT. For DISPLAY_ONLY we generate and show a passkey.
  uint32_t onPassKeyRequest() override {
    passkey = 100000 + (esp_random() % 900000);
    Serial.printf("[ble] passkey %06lu\n", (unsigned long)passkey);
    return passkey;
  }
  // Numeric comparison — show the PIN and auto-accept.
  bool onConfirmPIN(uint32_t pin) override {
    passkey = pin;
    Serial.printf("[ble] confirm pin %06lu\n", (unsigned long)pin);
    return true;
  }
  void onAuthenticationComplete(ble_gap_conn_desc* desc) override {
    passkey = 0;
    secure = desc->sec_state.encrypted;
    Serial.printf("[ble] auth %s\n", secure ? "ok" : "FAIL");
    if (!secure && server) {
      server->disconnect(desc->conn_handle, BLE_ERR_REM_USER_CONN_TERM);
    }
  }
};

void bleInit(const char* deviceName) {
  NimBLEDevice::init(deviceName);
  NimBLEDevice::setMTU(517);

  // LE Secure Connections with passkey display.
  // Security callbacks are handled in ServerCallbacks (onPassKeyRequest,
  // onConfirmPIN, onAuthenticationComplete) — NimBLE 1.4.x routes the
  // DISP action through server callbacks, not NimBLESecurityCallbacks.
  NimBLEDevice::setSecurityIOCap(BLE_HS_IO_DISPLAY_ONLY);
  NimBLEDevice::setSecurityAuth(true, true, true); // MITM, bond, SC

  server = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());

  NimBLEService* svc = server->createService(NUS_SERVICE_UUID);

  txChar = svc->createCharacteristic(
    NUS_TX_UUID,
    NIMBLE_PROPERTY::NOTIFY | NIMBLE_PROPERTY::READ_ENC
  );

  rxChar = svc->createCharacteristic(
    NUS_RX_UUID,
    NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR | NIMBLE_PROPERTY::WRITE_ENC
  );
  rxChar->setCallbacks(new RxCallbacks());

  svc->start();

  adv = server->getAdvertising();
  adv->addServiceUUID(NUS_SERVICE_UUID);
  adv->setScanResponse(true);
  adv->setMinPreferred(0x06);
  adv->setMaxPreferred(0x12);
  adv->start();
  Serial.printf("[ble] advertising as '%s'\n", deviceName);
}

bool bleConnected() { return connected; }
bool bleSecure()    { return secure; }
uint32_t blePasskey() { return passkey; }

void bleClearBonds() {
  NimBLEDevice::deleteAllBonds();
  Serial.println("[ble] cleared bonds");
}

size_t bleAvailable() {
  return (rxHead + RX_CAP - rxTail) % RX_CAP;
}

int bleRead() {
  if (rxHead == rxTail) return -1;
  int b = rxBuf[rxTail];
  rxTail = (rxTail + 1) % RX_CAP;
  return b;
}

size_t bleWrite(const uint8_t* data, size_t len) {
  if (!connected || !txChar || len == 0) return 0;
  size_t written = 0;
  while (written < len) {
    size_t free = txQFree();
    if (free == 0) break;
    size_t n = len - written;
    if (n > free) n = free;
    size_t seg1 = TX_Q_CAP - txQHead;
    if (seg1 >= n) {
      memcpy(txQ + txQHead, data + written, n);
    } else {
      memcpy(txQ + txQHead, data + written, seg1);
      memcpy(txQ, data + written + seg1, n - seg1);
    }
    txQHead = (txQHead + n) % TX_Q_CAP;
    written += n;
  }
  return written;
}

// Drain the BLE transmit queue. Call every loop iteration — sends as many
// chunks as the BLE stack can absorb in one go, so we don't fall behind
// when voice produces bursts of data.
void bleTick() {
  if (!connected || txQHead == txQTail) return;
  uint32_t now = millis();
  size_t chunk = mtu > 3 ? mtu - 3 : 20;
  if (chunk > 250) chunk = 250;

  // Send as many chunks as possible in one tick. The ESP32 NimBLE stack
  // queues notifications internally; we just push them through and rely
  // on the stack's own flow control.
  for (int i = 0; i < 8; i++) {
    size_t avail = (txQHead + TX_Q_CAP - txQTail) % TX_Q_CAP;
    if (avail == 0) return;
    size_t n = avail < chunk ? avail : chunk;
    // Contiguous segment (handle wrap at buffer end).
    size_t seg1 = TX_Q_CAP - txQTail;
    if (seg1 >= n) {
      txChar->notify(txQ + txQTail, n);
    } else {
      txChar->notify(txQ + txQTail, seg1);
      txChar->notify(txQ, n - seg1);
    }
    txQTail = (txQTail + n) % TX_Q_CAP;
  }
}
