// Minimal CoinFTBus example using the current production CoinFT mapping.

#include "CoinFTBus.h"

#include <chrono>
#include <iostream>
#include <string>
#include <thread>
#include <tuple>
#include <vector>

namespace {

constexpr const char* kPort = "/dev/tty.usbmodem101";
constexpr unsigned int kBaudRate = 115200;

constexpr const char* kLeftModel =
    "/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/assets/"
    "calibration/production/coinft_2606602_0008/"
    "coinft_2606602_0008_MLP.onnx";
constexpr const char* kLeftNorm =
    "/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/assets/"
    "calibration/production/coinft_2606602_0008/"
    "coinft_2606602_0008_norm.json";
constexpr const char* kRightModel =
    "/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/assets/"
    "calibration/production/coinft_2606602_0019/"
    "coinft_2606602_0019_MLP.onnx";
constexpr const char* kRightNorm =
    "/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/assets/"
    "calibration/production/coinft_2606602_0019/"
    "coinft_2606602_0019_norm.json";

}  // namespace

int main() {
  // left  = higher numbered active Teensy Serial/pins = coinft_2606602_0019
  // right = lower numbered active Teensy Serial/pins  = coinft_2606602_0008
  const std::vector<std::tuple<int, std::string, std::string>> sensorConfigs = {
      {CoinFTBus::LEFT, kLeftModel, kLeftNorm},
      {CoinFTBus::RIGHT, kRightModel, kRightNorm},
  };

  try {
    CoinFTBus bus(kPort, kBaudRate, sensorConfigs);
    std::cout << "CoinFTBus initialized and streaming." << std::endl;

    int sampleCounter = 0;
    while (true) {
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
      sampleCounter++;

      if (sampleCounter % 50 != 0) {
        continue;
      }

      std::vector<double> dataLeft = bus.getLatestData(CoinFTBus::LEFT);
      std::vector<double> dataRight = bus.getLatestData(CoinFTBus::RIGHT);

      std::cout << "Sample " << sampleCounter << ":\n  LEFT sensor FT:  ";
      for (double v : dataLeft) {
        std::cout << v << " ";
      }
      std::cout << "\n  RIGHT sensor FT: ";
      for (double v : dataRight) {
        std::cout << v << " ";
      }
      std::cout << "\n---------------------\n";
    }
  } catch (const std::exception& e) {
    std::cerr << "Exception in main: " << e.what() << std::endl;
    return 1;
  }

  return 0;
}
