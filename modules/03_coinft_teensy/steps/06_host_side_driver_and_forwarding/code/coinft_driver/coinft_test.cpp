/***************************************************************************************************
 * File:        coinft_interface.cpp
 * Author:      Hojung Choi
 * Email:       hjchoi92@stanford.edu
 * Institution: Stanford University
 * Date:        2024-11-19
 * Description: This script shows an example of how might the coinft class be used to acquire data.
 *
 * Usage:       ./coinft_interface <RunDurationInSeconds>
 ***************************************************************************************************/

#include <chrono>
#include <iomanip>  // For std::setprecision
#include <iostream>
#include <string>
#include <thread>
#include <vector>
#include "RobotUtilities/spatial_utilities.h"
#include "RobotUtilities/timer_linux.h"
#include "coinft/coin_ft.h"  // Include the CoinFT class

int main(int argc, char* argv[]) {
  if (argc < 2) {
    std::cerr << "Usage: ./coinft_interface <RunDurationInSeconds>"
              << std::endl;
    return 1;
  }

  double run_duration_ms =
      1000. * std::stod(argv[1]);  // Convert duration to double

  RUT::Timer timer;
  RUT::TimePoint time0 = timer.tic();

  CoinFT::CoinFTConfig config;
  config.port = "/dev/ttyACM0";
  config.baud_rate = 115200;
  config.left_calibration_file =
      "/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/assets/"
      "calibration/production/coinft_2606602_0008/"
      "coinft_2606602_0008_MLP.onnx";
  config.right_calibration_file =
      "/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/assets/"
      "calibration/production/coinft_2606602_0019/"
      "coinft_2606602_0019_MLP.onnx";
  config.left_normalization_file =
      "/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/assets/"
      "calibration/production/coinft_2606602_0008/"
      "coinft_2606602_0008_norm.json";
  config.right_normalization_file =
      "/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/assets/"
      "calibration/production/coinft_2606602_0019/"
      "coinft_2606602_0019_norm.json";
  config.WrenchSafety << 20.0, 20.0, 50.0, 1.0, 1.0, 1.0;
  config.PoseSensorToolLeft << 0.166, -0.081, 0.08, 0.707107, 0.0,
      -0.707107, 0.0;
  config.PoseSensorToolRight << 0.166, 0.081, 0.08, 0.0, -0.707107, 0.0,
      0.707107;

  try {
    // Provide the serial port, baud rate, and calibration matrix file name
    std::cout << "Creating CoinFT object..." << std::endl;
    CoinFT sensor;
    sensor.init(time0, config);
    std::cout << "CoinFT object created." << std::endl;

    // Main loop
    RUT::VectorXd wrench = RUT::VectorXd::Zero(12);
    while (timer.toc_ms() < run_duration_ms) {
      if (!sensor.is_data_ready()) {
        std::cout << "Waiting for data..." << std::endl;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        continue;
      }

      sensor.getWrenchSensor(wrench, 2);
      std::cout << "Time: " << timer.toc_ms() << " ms, Wrench: " << wrench
                << std::endl;

      // Sleep for a short duration
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }

  } catch (const std::exception& e) {
    std::cerr << "An error occurred: " << e.what() << std::endl;
  }

  return 0;
}
