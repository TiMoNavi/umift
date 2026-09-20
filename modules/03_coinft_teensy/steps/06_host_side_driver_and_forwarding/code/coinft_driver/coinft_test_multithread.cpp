/***************************************************************************************************
 * File:        coinft_test_multithread.cpp
 * Author:      Hojung Choi
 * Email:       hjchoi92@stanford.edu
 * Institution: Stanford University
 * Date:        2024-11-19
 * Description: This script shows an example of how might the coinft class be used to acquire data.
 *              It demonstrates the multi-threading capabilities. Note that with longer sleep time in 
 *              the main loop, the more data you acquire.
 *
 * Usage:       ./coinft_interface <RunDurationInSeconds>
 ***************************************************************************************************/

#include <chrono>
#include <iomanip>  // For std::setprecision
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include "coinft/coin_ft.h"  // Include the CoinFT class

// Simulate the robot_control function with variable delay
void robot_control(int& delay_ms, int& increment, const int min_delay,
                   const int max_delay) {
  auto start_time = std::chrono::steady_clock::now();

  // Simulate the delay
  std::this_thread::sleep_for(std::chrono::milliseconds(delay_ms));

  // Adjust the delay for next iteration
  delay_ms += increment;
  if (delay_ms >= max_delay || delay_ms <= min_delay) {
    increment = -increment;  // Reverse the direction of change
  }

  auto end_time = std::chrono::steady_clock::now();
  double actual_delay =
      std::chrono::duration<double, std::milli>(end_time - start_time).count();

  std::cout << "Robot control delay requested: " << delay_ms << " ms, "
            << "actual delay: " << actual_delay
            << " ms on thread ID: " << std::this_thread::get_id() << std::endl;
}

int main(int argc, char* argv[]) {
  if (argc < 2) {
    std::cerr << "Usage: ./coinft_test <RunDurationInSeconds>" << std::endl;
    return 1;
  }

  double run_duration = std::stod(argv[1]);  // Convert duration to double

  try {
    CoinFT::CoinFTConfig config;
    config.port = "/dev/tty.usbmodem2102";
    config.baud_rate = 115200;
    config.left_calibration_file =
        "modules/03_coinft_teensy/assets/"
        "calibration/production/coinft_2606602_0019/"
        "coinft_2606602_0019_MLP.onnx";
    config.right_calibration_file =
        "modules/03_coinft_teensy/assets/"
        "calibration/production/coinft_2606602_0012/"
        "coinft_2606602_0012_MLP.onnx";
    config.left_normalization_file =
        "modules/03_coinft_teensy/assets/"
        "calibration/production/coinft_2606602_0019/"
        "coinft_2606602_0019_norm.json";
    config.right_normalization_file =
        "modules/03_coinft_teensy/assets/"
        "calibration/production/coinft_2606602_0012/"
        "coinft_2606602_0012_norm.json";
    config.WrenchSafety << 20.0, 20.0, 50.0, 1.0, 1.0, 1.0;
    config.PoseSensorToolLeft << 0.166, -0.081, 0.08, 0.707107, 0.0,
        -0.707107, 0.0;
    config.PoseSensorToolRight << 0.166, 0.081, 0.08, 0.0, -0.707107, 0.0,
        0.707107;

    CoinFT sensor;
    RUT::Timer timer;
    sensor.init(timer.tic(), config);

    // Variables for robot_control()
    int delay_ms = 50;   // Start at 50 ms
    int increment = 10;  // Increment by 10 ms
    const int min_delay = 50;
    const int max_delay = 100;
    auto last_check_time = std::chrono::steady_clock::now();
    int samples_since_last = 0;

    // Start the timer
    auto start_time = std::chrono::steady_clock::now();
    RUT::VectorXd wrench = RUT::VectorXd::Zero(12);

    // Main control loop
    while (std::chrono::duration<double>(std::chrono::steady_clock::now() -
                                         start_time)
               .count() < run_duration) {
      // Perform the robot control operation with variable delay
      robot_control(delay_ms, increment, min_delay, max_delay);
      if (sensor.is_data_ready()) {
        sensor.getWrenchSensor(wrench, 2);
        samples_since_last++;
      }

      auto current_time = std::chrono::steady_clock::now();

      // Calculate the time elapsed since last check in seconds
      double time_elapsed =
          std::chrono::duration<double>(current_time - last_check_time).count();

      // Calculate the sensor reading rate (readings per second)
      double reading_rate = 0.0;
      if (time_elapsed > 0.0) {
        reading_rate = samples_since_last / time_elapsed;
      }

      // Output the results
      std::cout
          << "[" << std::fixed << std::setprecision(3)
          << std::chrono::duration<double>(current_time - start_time).count()
          << " s] "
          << "Robot control delay: " << delay_ms << " ms, "
          << "Readings since last check: " << samples_since_last << ", "
          << "Time elapsed: " << time_elapsed << " s, "
          << "Reading rate: " << reading_rate << " readings/s" << std::endl;

      samples_since_last = 0;
      last_check_time = current_time;
    }
  } catch (const std::exception& e) {
    std::cerr << "An error occurred: " << e.what() << std::endl;
  }
  return 0;
}
