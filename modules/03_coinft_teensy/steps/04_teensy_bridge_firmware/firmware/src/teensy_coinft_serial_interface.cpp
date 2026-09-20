#include <Arduino.h>

// Teensy -> CoinFT bridge.
//
// All candidate hardware UARTs are opened at 1 Mbps. At runtime, any UART
// that receives valid CoinFT frames is attached automatically. If two or more
// CoinFTs are live, the highest numbered Serial port is exported as left and
// the next highest as right.

static constexpr uint32_t COINFT_BAUD = 1000000;
static constexpr uint32_t HOST_BAUD = 115200;
static constexpr uint32_t STARTUP_DETECTION_WINDOW_US = 100000;
static constexpr uint32_t SENSOR_ACTIVE_TIMEOUT_US = 200000;
static constexpr uint8_t COINFT_FRAME_BODY_LEN = 24;
static constexpr uint8_t VALID_FRAMES_TO_CONNECT = 2;

IntervalTimer myTimer;

struct CoinFTPort {
  HardwareSerialIMXRT *serial;
  const char *name;
  uint8_t rank;
  byte data[COINFT_FRAME_BODY_LEN];
  volatile bool newData;
  volatile bool seen;
  volatile uint32_t lastDataUs;
  uint8_t validFrameCount;
};

CoinFTPort ports[] = {
  {&Serial1, "Serial1", 1, {0}, false, false, 0, 0},
  {&Serial2, "Serial2", 2, {0}, false, false, 0, 0},
  {&Serial3, "Serial3", 3, {0}, false, false, 0, 0},
  {&Serial4, "Serial4", 4, {0}, false, false, 0, 0},
  {&Serial5, "Serial5", 5, {0}, false, false, 0, 0},
  {&Serial6, "Serial6", 6, {0}, false, false, 0, 0},
  {&Serial7, "Serial7", 7, {0}, false, false, 0, 0},
  #if defined(ARDUINO_TEENSY41)
  {&Serial8, "Serial8", 8, {0}, false, false, 0, 0},
  #endif
};

static constexpr size_t PORT_COUNT = sizeof(ports) / sizeof(ports[0]);

volatile uint32_t packetSequence = 0;
volatile bool simulationMode = false;
volatile uint32_t streamStartUs = 0;

uint16_t simulatedRawValue(uint8_t sensorIndex, uint8_t channel, uint32_t sample);
bool sensorRecentlyActive(const CoinFTPort &port, uint32_t nowUs);
void beginCoinFTPorts();
void resetCoinFTState();
void writeCommandToCoinFTPorts(char command);
void readCoinFTPort(CoinFTPort &port);
uint32_t writePacketPrefix();
void writeUint16LE(uint16_t value);
void writeUint32LE(uint32_t value);
void writeSimulatedSensorPayload(uint8_t sensorIndex, uint32_t sample);
void writeSimulatedPacket();
void writeDualSensorPacket(const byte left[COINFT_FRAME_BODY_LEN], const byte right[COINFT_FRAME_BODY_LEN]);
void transmitData();
void runDiagnostics();

void setup() {
  beginCoinFTPorts();
  Serial.begin(HOST_BAUD);
}

void loop() {
  if (Serial.available()) {
    const char readChar = Serial.read();

    if (readChar == 'i') {
      writeCommandToCoinFTPorts('i');
      myTimer.end();
      simulationMode = false;
      resetCoinFTState();
    } else if (readChar == 's') {
      myTimer.end();
      simulationMode = false;
      resetCoinFTState();
      streamStartUs = micros();
      writeCommandToCoinFTPorts('s');
      myTimer.begin(transmitData, 1000);
    } else if (readChar == 'm') {
      myTimer.end();
      simulationMode = true;
      resetCoinFTState();
      streamStartUs = micros();
      myTimer.begin(transmitData, 1000);
    } else if (readChar == 't') {
      writeCommandToCoinFTPorts('t');
    } else if (readChar == 'd') {
      runDiagnostics();
    }
  }

  for (size_t i = 0; i < PORT_COUNT; i++) {
    readCoinFTPort(ports[i]);
  }
}

bool sensorRecentlyActive(const CoinFTPort &port, uint32_t nowUs) {
  return port.seen && static_cast<uint32_t>(nowUs - port.lastDataUs) < SENSOR_ACTIVE_TIMEOUT_US;
}

void beginCoinFTPorts() {
  for (size_t i = 0; i < PORT_COUNT; i++) {
    ports[i].serial->begin(COINFT_BAUD);
  }
}

void resetCoinFTState() {
  packetSequence = 0;
  for (size_t i = 0; i < PORT_COUNT; i++) {
    ports[i].newData = false;
    ports[i].seen = false;
    ports[i].lastDataUs = 0;
    ports[i].validFrameCount = 0;
  }
}

void writeCommandToCoinFTPorts(char command) {
  for (size_t i = 0; i < PORT_COUNT; i++) {
    ports[i].serial->write(command);
  }
}

void readCoinFTPort(CoinFTPort &port) {
  while (port.serial->available() > COINFT_FRAME_BODY_LEN + 1) {
    if (port.serial->read() != 0x02) {
      continue;
    }

    byte body[COINFT_FRAME_BODY_LEN] = {0};
    port.serial->readBytes(body, COINFT_FRAME_BODY_LEN);

    bool hasEndByte = false;
    const uint32_t waitStartUs = micros();
    while (static_cast<uint32_t>(micros() - waitStartUs) < 1000) {
      if (port.serial->available() <= 0) {
        continue;
      }
      if (port.serial->read() == 0x03) {
        hasEndByte = true;
        break;
      }
    }
    if (!hasEndByte) {
      continue;
    }

    memcpy(port.data, body, COINFT_FRAME_BODY_LEN);
    if (port.validFrameCount < VALID_FRAMES_TO_CONNECT) {
      port.validFrameCount++;
    }
    if (port.validFrameCount >= VALID_FRAMES_TO_CONNECT) {
      port.lastDataUs = micros();
      port.seen = true;
      port.newData = true;
    }
  }
}

uint16_t simulatedRawValue(uint8_t sensorIndex, uint8_t channel, uint32_t sample) {
  const int32_t baseline = 30000 + static_cast<int32_t>(sensorIndex) * 650 + static_cast<int32_t>(channel) * 85;
  const uint32_t phase = (sample * (channel + 3) + sensorIndex * 157 + channel * 61) & 0x3FF;
  const int32_t triangle = (phase < 512) ? static_cast<int32_t>(phase) : static_cast<int32_t>(1023 - phase);
  const int32_t centered = triangle - 255;
  const int32_t pulse = (((sample / 250) + channel + sensorIndex) % 16 == 0) ? 900 : 0;
  int32_t value = baseline + centered * 3 + pulse;

  if (value < 0) {
    value = 0;
  } else if (value > 65535) {
    value = 65535;
  }

  return static_cast<uint16_t>(value);
}

void writeUint16LE(uint16_t value) {
  Serial.write(static_cast<uint8_t>(value & 0xFF));
  Serial.write(static_cast<uint8_t>((value >> 8) & 0xFF));
}

void writeUint32LE(uint32_t value) {
  Serial.write(static_cast<uint8_t>(value & 0xFF));
  Serial.write(static_cast<uint8_t>((value >> 8) & 0xFF));
  Serial.write(static_cast<uint8_t>((value >> 16) & 0xFF));
  Serial.write(static_cast<uint8_t>((value >> 24) & 0xFF));
}

uint32_t writePacketPrefix() {
  const uint32_t sequence = packetSequence++;
  Serial.write(0x00);
  Serial.write(0x00);
  writeUint32LE(sequence);
  writeUint32LE(static_cast<uint32_t>(micros()));
  return sequence;
}

void writeSimulatedSensorPayload(uint8_t sensorIndex, uint32_t sample) {
  for (uint8_t channel = 0; channel < 12; channel++) {
    writeUint16LE(simulatedRawValue(sensorIndex, channel, sample));
  }
}

void writeSimulatedPacket() {
  const uint32_t sequence = writePacketPrefix();
  writeSimulatedSensorPayload(0, sequence);
  writeSimulatedSensorPayload(1, sequence);
}

void writeDualSensorPacket(const byte left[COINFT_FRAME_BODY_LEN], const byte right[COINFT_FRAME_BODY_LEN]) {
  writePacketPrefix();
  Serial.write(left, COINFT_FRAME_BODY_LEN);
  Serial.write(right, COINFT_FRAME_BODY_LEN);
}

void transmitData() {
  if (simulationMode) {
    writeSimulatedPacket();
    return;
  }

  const uint32_t nowUs = static_cast<uint32_t>(micros());
  const bool startupDetecting = static_cast<uint32_t>(nowUs - streamStartUs) < STARTUP_DETECTION_WINDOW_US;
  int leftIndex = -1;
  int rightIndex = -1;

  for (size_t i = 0; i < PORT_COUNT; i++) {
    const bool active = sensorRecentlyActive(ports[i], nowUs);
    if (!active) {
      ports[i].newData = false;
      continue;
    }
    if (leftIndex < 0 || ports[i].rank > ports[leftIndex].rank) {
      rightIndex = leftIndex;
      leftIndex = static_cast<int>(i);
    } else if (rightIndex < 0 || ports[i].rank > ports[rightIndex].rank) {
      rightIndex = static_cast<int>(i);
    }
  }

  if (leftIndex >= 0 && rightIndex >= 0) {
    CoinFTPort &left = ports[leftIndex];
    CoinFTPort &right = ports[rightIndex];
    if (left.newData && right.newData) {
      writeDualSensorPacket(left.data, right.data);
      left.newData = false;
      right.newData = false;
    }
    return;
  }

  if (leftIndex >= 0) {
    if (startupDetecting) {
      return;
    }
    CoinFTPort &single = ports[leftIndex];
    if (single.newData) {
      Serial.write(single.data, COINFT_FRAME_BODY_LEN);
      single.newData = false;
    }
    return;
  }

  if (!startupDetecting) {
    writeSimulatedPacket();
  }
}

void runDiagnostics() {
  myTimer.end();
  simulationMode = false;
  resetCoinFTState();

  Serial.println("coinft_diag begin");
  writeCommandToCoinFTPorts('i');
  delay(100);
  writeCommandToCoinFTPorts('s');

  const uint32_t startUs = micros();
  while (static_cast<uint32_t>(micros() - startUs) < 500000) {
    for (size_t i = 0; i < PORT_COUNT; i++) {
      readCoinFTPort(ports[i]);
    }
  }
  writeCommandToCoinFTPorts('i');

  int leftIndex = -1;
  int rightIndex = -1;
  const uint32_t nowUs = micros();
  for (size_t i = 0; i < PORT_COUNT; i++) {
    const bool active = sensorRecentlyActive(ports[i], nowUs);
    Serial.print(ports[i].name);
    Serial.print(" rank=");
    Serial.print(ports[i].rank);
    Serial.print(" valid_frames=");
    Serial.print(ports[i].validFrameCount);
    Serial.print(" active=");
    Serial.println(active ? "yes" : "no");

    if (!active) {
      continue;
    }
    if (leftIndex < 0 || ports[i].rank > ports[leftIndex].rank) {
      rightIndex = leftIndex;
      leftIndex = static_cast<int>(i);
    } else if (rightIndex < 0 || ports[i].rank > ports[rightIndex].rank) {
      rightIndex = static_cast<int>(i);
    }
  }

  Serial.print("left=");
  Serial.println(leftIndex >= 0 ? ports[leftIndex].name : "none");
  Serial.print("right=");
  Serial.println(rightIndex >= 0 ? ports[rightIndex].name : "none");
  Serial.println("coinft_diag end");
}
