#include "coinft/CoinFTBus.h"
#include <unistd.h>
#include <boost/asio.hpp>
#include <boost/asio/serial_port.hpp>
#include <chrono>
#include <csignal>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <thread>
#include <vector>

// Global ONNX Runtime environment (only one per process is recommended)
static Ort::Env ort_env(ORT_LOGGING_LEVEL_WARNING, "CoinFTBus");
Ort::AllocatorWithDefaultOptions allocator;

std::vector<CoinFTBus*> activeInstances;
std::mutex instanceMutex;

//----------------------------------------------------------------
// Constructor
//----------------------------------------------------------------
CoinFTBus::CoinFTBus(
    const std::string& port, unsigned int baud_rate,
    const std::vector<std::tuple<int, std::string, std::string>>& sensorConfigs)
    : serial(io), running(false), num_raw_channels(0) {
  {
    std::lock_guard<std::mutex> lock(instanceMutex);
    activeInstances.push_back(this);
  }

  std::signal(SIGINT, signalHandler);
  std::signal(SIGTERM, signalHandler);
  std::signal(SIGTSTP, signalHandler);

  // **Close lingering serial connections first**
  if (serial.is_open()) {
    std::cout << "[DEBUG] Closing previous serial connection..." << std::endl;
    serial.close();
    std::this_thread::sleep_for(
        std::chrono::milliseconds(10));  // Allow OS time to release port
  }

  // Open the serial port
  serial.open(port);
  serial.set_option(boost::asio::serial_port_base::baud_rate(baud_rate));
  serial.set_option(boost::asio::serial_port_base::character_size(8));
  serial.set_option(boost::asio::serial_port_base::parity(
      boost::asio::serial_port_base::parity::none));
  serial.set_option(boost::asio::serial_port_base::stop_bits(
      boost::asio::serial_port_base::stop_bits::one));
  serial.set_option(boost::asio::serial_port_base::flow_control(
      boost::asio::serial_port_base::flow_control::none));

  std::cout << "Opened serial port: " << port << std::endl;

  // Initialize the bus
  initializeBus();

  // Load sensor configurations
  for (const auto& config : sensorConfigs) {
    int address;
    std::string model_file, json_path;
    std::tie(address, model_file, json_path) = config;

    CoinFTSensor sensor;
    sensor.address = address;
    sensor.tareInProgress = true;
    sensor.tareSampleCount = 0;
    sensor.tareSampleTarget = 1000;
    sensor.tareOffset = Eigen::VectorXd::Zero(num_raw_channels);
    sensor.latestData = std::vector<double>(6, 0.0);

    /* ---------- load μ/σ JSON ------------------ */
    std::ifstream jf(json_path);
    if (!jf)
      throw std::runtime_error("Cannot open " + json_path);
    nlohmann::json j;
    jf >> j;

    auto vec12 = [](const nlohmann::json& a) {
      Eigen::VectorXd v(12);
      for (int i = 0; i < 12; ++i)
        v(i) = a.at(i).get<double>();
      return v;
    };
    auto vec6 = [](const nlohmann::json& a) {
      Eigen::VectorXd v(6);
      for (int i = 0; i < 6; ++i)
        v(i) = a.at(i).get<double>();
      return v;
    };

    sensor.mu_x = vec12(j["mu_x"]);
    sensor.sd_x = vec12(j["sd_x"]);
    sensor.mu_y = vec6(j["mu_y"]);
    sensor.sd_y = vec6(j["sd_y"]);
    /* ------------------------------------------- */

    // Create ONNX session options. TODO: what does this do, line by line?
    Ort::SessionOptions session_options;
    session_options.SetIntraOpNumThreads(1);
    session_options.SetGraphOptimizationLevel(
        GraphOptimizationLevel::ORT_ENABLE_EXTENDED);

    // Create ONNX session
    sensor.session = std::make_unique<Ort::Session>(ort_env, model_file.c_str(),
                                                    session_options);

    // Retrieve input and output node names
    sensor.input_name =
        sensor.session->GetInputNameAllocated(0, allocator).get();
    sensor.output_name =
        sensor.session->GetOutputNameAllocated(0, allocator).get();

    // Store the sensor configuration
    {
      std::lock_guard<std::mutex> lock(sensors_mutex);
      sensors[address] = std::move(sensor);
    }

    std::cout << "Configured sensor at address " << address
              << " with ONNX model: " << model_file << std::endl;
  }

  // Start streaming
  startStreaming();

  // Wait until all sensors are tared
  std::cout << "Waiting for sensors to tare..." << std::endl;
  bool allTared = false;
  while (!allTared) {
    allTared = true;
    {
      std::lock_guard<std::mutex> lock(sensors_mutex);
      for (auto& kv : sensors) {
        std::lock_guard<std::mutex> sensor_lock(kv.second.mutex);
        if (kv.second.tareInProgress) {
          allTared = false;
          break;
        }
      }
    }
    if (!allTared) {
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
  }
  std::cout << "All sensors are tared." << std::endl;
}

//----------------------------------------------------------------
// Destructor
//----------------------------------------------------------------
CoinFTBus::~CoinFTBus() {
  stopStreaming();
  if (serial.is_open()) {
    sendIdle();
    serial.close();
  }

  // Remove instance from activeInstances
  {
    std::lock_guard<std::mutex> lock(instanceMutex);
    activeInstances.erase(
        std::remove(activeInstances.begin(), activeInstances.end(), this),
        activeInstances.end());
  }
}

//----------------------------------------------------------------
// Initialize Bus
//----------------------------------------------------------------

void CoinFTBus::initializeBus() {
  std::cout << "[DEBUG] Attempting to reset device to IDLE mode..."
            << std::endl;

  // Send IDLE multiple times to ensure sensor resets from an unknown state
  for (int i = 0; i < 3; i++) {
    sendChar(IDLE);
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
  }

// Clear any unread data in the serial buffer
#if defined(__unix__) || defined(__APPLE__)
  serial.cancel();
  std::cout << "[DEBUG] Flushing serial buffer..." << std::endl;
  int fd = serial.native_handle();
  tcflush(fd, TCIFLUSH);
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
#endif

  // std::cout << "[DEBUG] Sending QUERY command to get packet size..." << std::endl;
  // sendChar(QUERY);
  // std::this_thread::sleep_for(std::chrono::milliseconds(20));

  // Read response
  // std::vector<uint8_t> data = readData(1);
  // if (data.empty()) {
  //     throw std::runtime_error("[ERROR] Failed to read packet size from sensor bus.");
  // }

  // packet_size = static_cast<int>(data[0]) - 1;
  // num_raw_channels = (packet_size - 1) / 2;
  num_raw_channels =
      CoinFTBus::COINFT_CHANNELS;  // fixed 12 channels per sensor
  // std::cout << "[DEBUG] Packet size: " << packet_size
  //           << ", Number of raw channels: " << num_raw_channels << std::endl;
}

//----------------------------------------------------------------
// Start Streaming
//----------------------------------------------------------------
void CoinFTBus::startStreaming() {
  if (running.load())
    return;

  sendChar(STREAM);
  std::this_thread::sleep_for(std::chrono::milliseconds(10));
  running.store(true);
  data_thread = std::thread(&CoinFTBus::dataAcquisitionLoop, this);
  std::cout << "Started streaming on sensor bus." << std::endl;
}

//----------------------------------------------------------------
// Stop Streaming
//----------------------------------------------------------------
/*void CoinFTBus::stopStreaming() {
    running.store(false);
    sendIdle();  // Ensure sensors are set to idle
    serial.cancel();  // Cancel any ongoing read operations

    if (data_thread.joinable()) {
        std::cerr << "[DEBUG] Waiting for data thread to stop..." << std::endl;
        data_thread.join();  // Wait for the thread to finish
        std::cerr << "[DEBUG] Data thread successfully stopped." << std::endl;
    }

    std::cout << "Stopped streaming on sensor bus." << std::endl;
}*/
void CoinFTBus::stopStreaming() {
  if (!running.exchange(false))
    return;  // Prevent multiple calls

  std::cerr << "[DEBUG] Stopping data acquisition thread..." << std::endl;

  sendIdle();       // Ensure sensors are set to idle
  serial.cancel();  // Cancel ongoing read operations

  // Give the thread some time to exit gracefully
  if (data_thread.joinable()) {
    std::thread killer([&]() {
      std::this_thread::sleep_for(std::chrono::milliseconds(500));

      // If still joinable, force detach
      if (data_thread.joinable()) {
        std::cerr << "[WARNING] Data thread did not stop in time, forcefully "
                     "detaching!"
                  << std::endl;
        data_thread.detach();
      }
    });

    data_thread.join();
    killer.detach();
  }

  io.stop();   // Ensure io_service stops processing events
  io.reset();  // Reset for potential reuse

  std::cout << "[DEBUG] Data thread successfully stopped." << std::endl;
  std::cout << "Stopped streaming on sensor bus." << std::endl;
}

//----------------------------------------------------------------
// Idle
//----------------------------------------------------------------
void CoinFTBus::idle() {
  sendChar(IDLE);
  std::cout << "Sensor bus set to idle mode." << std::endl;
}

//----------------------------------------------------------------
// Tare Sensor
//----------------------------------------------------------------
void CoinFTBus::tareSensor(int address) {
  std::lock_guard<std::mutex> lock(sensors_mutex);
  auto it = sensors.find(address);
  if (it != sensors.end()) {
    std::lock_guard<std::mutex> sensor_lock(it->second.mutex);
    it->second.tareInProgress = true;
    it->second.tareSampleCount = 0;
    it->second.tareSamples.clear();
    it->second.tareOffset = Eigen::VectorXd::Zero(num_raw_channels);
    std::cout << "Taring initiated for sensor at address: " << address
              << std::endl;
  } else {
    std::cerr << "Sensor with address " << address << " not found."
              << std::endl;
  }
}

//----------------------------------------------------------------
// Get Latest Data
//----------------------------------------------------------------
std::vector<double> CoinFTBus::getLatestData(int address) {
  std::lock_guard<std::mutex> lock(sensors_mutex);
  auto it = sensors.find(address);
  if (it != sensors.end()) {
    std::lock_guard<std::mutex> sensor_lock(it->second.mutex);
    return it->second.latestData;
  } else {
    std::cerr << "Sensor with address " << address << " not found."
              << std::endl;
    return std::vector<double>();
  }
}

//----------------------------------------------------------------
// Send Idle Command
//----------------------------------------------------------------
void CoinFTBus::sendIdle() {
  sendChar(IDLE);
}

//----------------------------------------------------------------
// Send Character Command
//----------------------------------------------------------------
void CoinFTBus::sendChar(uint8_t cmd) {
  boost::asio::write(serial, boost::asio::buffer(&cmd, 1));
}

//----------------------------------------------------------------
// Read Data from Sensor Bus
//----------------------------------------------------------------

std::vector<uint8_t> CoinFTBus::readData(size_t length) {
  std::vector<uint8_t> buffer(length);
  boost::system::error_code ec;

  // Ensure io_service is reset before running async operations
  io.restart();

  boost::asio::deadline_timer timer(io, boost::posix_time::milliseconds(500));

  bool timeout_flag = false;
  bool read_complete = false;

  //std::cout << "[DEBUG] Starting async read of " << length << " bytes..." << std::endl;

  // Setup timeout handling
  timer.async_wait([&](const boost::system::error_code& error) {
    if (!error && !read_complete) {  // Timer expired
      timeout_flag = true;
      std::cerr << "[ERROR] Serial read timeout! Cancelling operation."
                << std::endl;
      serial.cancel();
    }
  });

  // Start async read operation
  boost::asio::async_read(serial, boost::asio::buffer(buffer, length),
                          [&](const boost::system::error_code& error,
                              std::size_t bytes_transferred) {
                            ec = error;
                            read_complete = true;
                            timer.cancel();  // Stop the timeout timer

                            if (error) {
                              std::cerr << "[ERROR] Serial read failed: "
                                        << error.message() << std::endl;
                            } else {
                              //std::cout << "[DEBUG] Successfully read " << bytes_transferred << " bytes." << std::endl;
                            }
                          });

  // Process IO events until read completes or timeout occurs
  while (!read_complete && !timeout_flag) {
    io.run_one();
  }

  // If a timeout occurred, return empty buffer
  if (timeout_flag) {
    std::cerr << "[ERROR] Read operation timed out. Returning empty buffer."
              << std::endl;
    return {};
  }

  // Handle errors
  if (ec && ec != boost::asio::error::operation_aborted) {
    std::cerr << "[ERROR] Serial read error: " << ec.message() << std::endl;
    return {};
  }

  return buffer;
}

//----------------------------------------------------------------
// Data Acquisition Loop  (UART: 2×CoinFT per packet, 0x00 0x00 header)
//----------------------------------------------------------------
void CoinFTBus::dataAcquisitionLoop() {
  // UART packet:
  // [0x00,0x00] + u32 sequence_id + u32 teensy_time_us
  // + 12×u16 (LEFT) + 12×u16 (RIGHT)  ==> total 2 + 8 + 24 + 24 = 58 bytes
  const int header_len = 2;
  const int metadata_len = 8;
  const int bytes_per_sensor = num_raw_channels * 2;         // expect 12→24
  const int payload_offset = header_len + metadata_len;
  const int packet_len = payload_offset + 2 * bytes_per_sensor;  // expect 58

  auto processSensor = [&](CoinFTSensor& sensor,
                           const Eigen::VectorXd& rawInput, int sensorKey) {
    std::lock_guard<std::mutex> sensor_lock(sensor.mutex);

    // Tare collection
    if (sensor.tareInProgress) {
      sensor.tareSamples.push_back(rawInput);
      sensor.tareSampleCount++;

      if (sensor.tareSampleCount >= sensor.tareSampleTarget) {
        // Match Python: discard first 5 samples if available
        const int discard = std::min(5, sensor.tareSampleCount);
        const int kept = sensor.tareSampleCount - discard;

        if (kept <= 0) {
          // Fallback: no valid samples, keep taring
          return;
        }

        Eigen::VectorXd sum = Eigen::VectorXd::Zero(num_raw_channels);
        for (int i = discard; i < sensor.tareSampleCount; ++i) {
          sum += sensor.tareSamples[i];
        }
        sensor.tareOffset = sum / static_cast<double>(kept);

        sensor.tareSamples.clear();
        sensor.tareSampleCount = 0;
        sensor.tareInProgress = false;
        std::cout << "Taring completed for sensor key " << sensorKey
                  << std::endl;
      }
      return;  // Skip further processing until taring is complete
    }

    // Normalize → ONNX → de-normalize
    Eigen::VectorXd adjusted = rawInput - sensor.tareOffset;  // 12
    Eigen::VectorXd normIn =
        (adjusted - sensor.mu_x).cwiseQuotient(sensor.sd_x);  // 12

    std::vector<float> input_tensor_values(num_raw_channels);
    for (int i = 0; i < num_raw_channels; ++i)
      input_tensor_values[i] = static_cast<float>(normIn(i));
    std::array<int64_t, 2> input_shape = {1, num_raw_channels};

    Ort::MemoryInfo memory_info =
        Ort::MemoryInfo::CreateCpu(OrtArenaAllocator, OrtMemTypeDefault);
    Ort::Value input_tensor = Ort::Value::CreateTensor<float>(
        memory_info, input_tensor_values.data(), input_tensor_values.size(),
        input_shape.data(), input_shape.size());

    std::vector<const char*> input_names = {sensor.input_name.c_str()};
    std::vector<const char*> output_names = {sensor.output_name.c_str()};

    auto output_tensors =
        sensor.session->Run(Ort::RunOptions{nullptr}, input_names.data(),
                            &input_tensor, 1, output_names.data(), 1);

    float* yhat = output_tensors[0].GetTensorMutableData<float>();  // 6
    Eigen::VectorXd y_n(6);
    for (int i = 0; i < 6; ++i)
      y_n(i) = static_cast<double>(yhat[i]);

    // physical units: y = y_n * sd_y + mu_y
    Eigen::VectorXd y_phys =
        y_n.cwiseProduct(sensor.sd_y).array() + sensor.mu_y.array();

    std::vector<double> ft(6);
    for (int i = 0; i < 6; ++i)
      ft[i] = y_phys(i);
    sensor.latestData = std::move(ft);
  };

  while (running.load()) {
    try {
      // Read a full UART packet
      std::vector<uint8_t> packet = readData(packet_len);
      if (packet.size() != static_cast<size_t>(packet_len)) {
        // Timeout or short read
        continue;
      }

      // Check 2-byte header (0x00, 0x00) as in Python
      if (packet[0] != 0x00 || packet[1] != 0x00) {
        // Try to resync by scanning for the 2-byte header within this buffer
        bool found = false;
        for (size_t i = 1; i + 1 < packet.size(); ++i) {
          if (packet[i] == 0x00 && packet[i + 1] == 0x00) {
            // We found a header inside; read the remaining tail to complete a full packet
            const size_t already =
                packet.size() - i;  // bytes from header we already have
            std::vector<uint8_t> tail = readData(packet_len - already);
            if (tail.size() != static_cast<size_t>(packet_len - already)) {
              break;
            }
            std::vector<uint8_t> fixed(packet.begin() + i, packet.end());
            fixed.insert(fixed.end(), tail.begin(), tail.end());
            packet.swap(fixed);
            found = true;
            break;
          }
        }
        if (!found)
          continue;  // try again next loop
      }

      // Parse LEFT sensor (first 12×u16 after header + metadata)
      Eigen::VectorXd rawLeft(num_raw_channels);
      for (int i = 0; i < num_raw_channels; ++i) {
        const int off = payload_offset + 2 * i;
        uint16_t v = static_cast<uint16_t>(packet[off] + 256 * packet[off + 1]);
        rawLeft(i) = static_cast<double>(v);
      }

      // Parse RIGHT sensor (next 12×u16)
      Eigen::VectorXd rawRight(num_raw_channels);
      for (int i = 0; i < num_raw_channels; ++i) {
        const int off = payload_offset + bytes_per_sensor + 2 * i;
        uint16_t v = static_cast<uint16_t>(packet[off] + 256 * packet[off + 1]);
        rawRight(i) = static_cast<double>(v);
      }

      // Process both sensors in one go
      {
        std::lock_guard<std::mutex> lock(sensors_mutex);

        auto itL = sensors.find(LEFT);
        auto itR = sensors.find(RIGHT);
        if (itL == sensors.end() || itR == sensors.end()) {
          std::cerr << "[WARN] LEFT/RIGHT sensor keys not found in sensors map."
                    << std::endl;
          continue;
        }
        processSensor(itL->second, rawLeft, LEFT);
        processSensor(itR->second, rawRight, RIGHT);
      }

    } catch (const std::exception& e) {
      std::cerr << "Exception in data acquisition loop: " << e.what()
                << std::endl;
      running.store(false);
    }
  }
}

//----------------------------------------------------------------
// Signal Handler
//----------------------------------------------------------------
void CoinFTBus::signalHandler(int signum) {
  if (signum == SIGINT || signum == SIGTERM) {
    std::cout << "\n[DEBUG] Signal (" << signum
              << ") received, shutting down all active instances..."
              << std::endl;
    {
      std::lock_guard<std::mutex> lock(instanceMutex);
      for (auto* instance : activeInstances) {
        if (instance) {
          instance->stopStreaming();
          instance->idle();
        }
      }
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
    std::exit(signum);
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
  } else if (signum == SIGTSTP) {  // Handle `Ctrl+Z` separately
    std::cout
        << "\n[DEBUG] Signal (SIGTSTP) received, suspending all instances..."
        << std::endl;

    {
      std::lock_guard<std::mutex> lock(instanceMutex);
      for (auto* instance : activeInstances) {
        if (instance) {
          instance->stopStreaming();
          instance->idle();
        }
      }
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(500));

    // Send SIGSTOP to suspend process
    kill(getpid(), SIGSTOP);
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
  }
}
