#ifndef COINFTBUS_H
#define COINFTBUS_H

// #include <cpu_provider_factory.h>
#include <onnxruntime_cxx_api.h>
#include <Eigen/Dense>
#include <boost/asio.hpp>
#include <csignal>
#include <map>
#include <mutex>
#include <nlohmann/json.hpp>
#include <string>
#include <thread>
#include <vector>

struct CoinFTSensor {
  int address;
  bool tareInProgress = true;
  std::string input_name;
  std::string output_name;
  std::unique_ptr<Ort::Session>
      session;  // ONNX Runtime session for the neural network
  int tareSampleCount = 0;
  int tareSampleTarget = 1000;
  std::vector<Eigen::VectorXd> tareSamples;
  Eigen::VectorXd tareOffset;
  std::vector<double> latestData;
  std::mutex mutex;

  /* --- normalisation constants -------------------------------- */
  Eigen::VectorXd mu_x;  // 12×1
  Eigen::VectorXd sd_x;  // 12×1
  Eigen::VectorXd mu_y;  // 6×1
  Eigen::VectorXd sd_y;  // 6×1

  // Move constructor
  CoinFTSensor(CoinFTSensor&& other) noexcept
      : address(other.address),
        tareInProgress(other.tareInProgress),
        input_name(std::move(other.input_name)),
        output_name(std::move(other.output_name)),
        session(std::move(other.session)),
        tareSampleCount(other.tareSampleCount),
        tareSampleTarget(other.tareSampleTarget),
        tareSamples(std::move(other.tareSamples)),
        tareOffset(std::move(other.tareOffset)),
        latestData(std::move(other.latestData)),
        mu_x(std::move(other.mu_x)),
        sd_x(std::move(other.sd_x)),
        mu_y(std::move(other.mu_y)),
        sd_y(std::move(other.sd_y)) {}

  // Move assignment operator
  CoinFTSensor& operator=(CoinFTSensor&& other) noexcept {
    if (this != &other) {
      address = other.address;
      tareInProgress = other.tareInProgress;
      input_name = std::move(other.input_name);
      output_name = std::move(other.output_name);
      session = std::move(other.session);
      tareSampleCount = other.tareSampleCount;
      tareSampleTarget = other.tareSampleTarget;
      tareSamples = std::move(other.tareSamples);
      tareOffset = std::move(other.tareOffset);
      latestData = std::move(other.latestData);
      mu_x = std::move(other.mu_x);
      sd_x = std::move(other.sd_x);
      mu_y = std::move(other.mu_y);
      sd_y = std::move(other.sd_y);
    }
    return *this;
  }

  // Disable copy constructor and copy assignment operator
  CoinFTSensor(const CoinFTSensor&) = delete;
  CoinFTSensor& operator=(const CoinFTSensor&) = delete;

  // Default constructor
  CoinFTSensor() = default;
};

class CoinFTBus {
 public:
  // Enum for command values (moved inside the class)
  enum Commands {
    IDLE = 0x69,  // 'i'
    // QUERY = 0x71,  // 'q'
    STREAM = 0x73  // 's'
  };

  // static constexpr uint8_t STX = 0x02; // Start of Transmission byte
  // static constexpr uint8_t ETX = 0x03; // End of Transmission byte

  // constants for UART communication
  static constexpr int UART_HEADER_LEN = 2;
  static constexpr int UART_METADATA_LEN = 8;  // sequence_id + teensy_time_us
  static constexpr int COINFT_CHANNELS = 12;
  static constexpr int BYTES_PER_SENSOR = COINFT_CHANNELS * 2;  // 24
  static constexpr int UART_PACKET_LEN =
      UART_HEADER_LEN + UART_METADATA_LEN + 2 * BYTES_PER_SENSOR;  // 58

  CoinFTBus(const std::string& port, unsigned int baud_rate,
            const std::vector<std::tuple<int, std::string, std::string>>&
                sensorConfigs);
  ~CoinFTBus();

  void startStreaming();
  void stopStreaming();
  void idle();
  void tareSensor(int address);
  std::vector<double> getLatestData(int address);

  static constexpr int LEFT = 8;
  static constexpr int RIGHT = 9;

  static void signalHandler(int signum);

 private:
  void initializeBus();
  void sendIdle();
  void sendChar(uint8_t cmd);
  std::vector<uint8_t> readData(size_t length);
  void dataAcquisitionLoop();

  std::map<int, CoinFTSensor> sensors;
  std::mutex sensors_mutex;
  boost::asio::io_service io;
  boost::asio::serial_port serial;
  std::atomic<bool> running;
  std::thread data_thread;
  // int packet_size;
  int num_raw_channels;
};

// List of active instances
extern std::vector<CoinFTBus*> activeInstances;
extern std::mutex instanceMutex;

#endif  // COINFTBUS_H
