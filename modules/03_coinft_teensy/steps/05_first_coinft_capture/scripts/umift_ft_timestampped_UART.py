#!/usr/bin/env python3
"""
Filename: umift_ft_timestamped_UART.py
Author:   Hojung Choi (rewritten)
Date:     2025/10/03

Description:
    Timestamped FT data capture from TWO CoinFT sensors over a single UART stream.
    Packet format (per read):
        [0x00, 0x00] + u32 sequence_id + u32 teensy_time_us
        + 12*u16 (CoinFT1) + 12*u16 (CoinFT2)  => total 58 bytes
    Each sensor is calibrated via its own ONNX model + normalization constants.

    Output columns (per-sensor CSV):
      [Timestamp, Fx, Fy, Fz, Mx, My, Mz, C1, C2, ..., C12]
"""

import argparse
import time
import struct
import numpy as np
import os
import datetime
import matplotlib.pyplot as plt
import ntplib
import onnxruntime as ort
import copy
import serial
import json

# =========================
# Fixed constants
# =========================
NUM_RAW_CHANNELS = 12
NUM_DATA_ENTRY = 1 + 6 + NUM_RAW_CHANNELS   # timestamp + 6 FT + 12 raw

# =========================
# User-configurable
# =========================
SAMPLING_RATE       = 360              # for buffer sizing only (approx)
INITIAL_SAMPLES     = 1000              # samples for offset (tare)
IGNORED_SAMPLES     = 10               # ignore first N before computing offset

# UART
BAUD_RATE  = 115200
READ_TIMEOUT = 0.1

# Packet format for TWO CoinFTs
HEADER_BYTES  = b"\x00\x00"
HEADER_LEN    = 2
COINFT_CH     = 12
COINFT_BYTES  = COINFT_CH * 2                      # uint16 * 12
METADATA_LEN  = 8                                  # sequence_id + teensy_time_us
PACKET_LEN    = HEADER_LEN + METADATA_LEN + COINFT_BYTES * 2

FILE_NAME_ANNOTATIONS = ['LF', 'RF']  # filenames: Left Finger, Right Finger

# Plotting / debug
debug_wrench_every = 150
PLOT_EVERY = 50
TIME_HISTORY = 10.0  # seconds for live plot window


# =========================
# Helpers
# =========================
def get_ntp_time():
    """Return NTP time if available, else local time."""
    try:
        client = ntplib.NTPClient()
        response = client.request('pool.ntp.org')
        return response.tx_time
    except Exception as e:
        print("Failed to get NTP time:", e)
        return time.time()

def load_norms(norm_paths):
    """Load normalization constants from JSON files."""
    out = []
    for path in norm_paths:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Norm file not found: {path}")

        with open(path, 'r') as f:
            data = json.load(f)
            
        out.append({
            'mu_x':  np.array(data['mu_x'], dtype=np.float32),
            'sd_x':  np.array(data['sd_x'], dtype=np.float32),
            'mu_y':  np.array(data['mu_y'], dtype=np.float32),
            'sd_y':  np.array(data['sd_y'], dtype=np.float32)
        })
    return out

def start_stream(ser, simulate=False):
    """Toggle to idle then start streaming."""
    ser.write(b'i')
    time.sleep(0.2)
    ser.reset_input_buffer()
    ser.write(b'm' if simulate else b's')
    time.sleep(0.05)

def read_exact(ser, nbytes):
    """Read exactly nbytes (or return None on timeout/short)."""
    buf = bytearray()
    t0 = time.time()
    while len(buf) < nbytes:
        chunk = ser.read(nbytes - len(buf))
        if chunk:
            buf.extend(chunk)
        else:
            if (time.time() - t0) > READ_TIMEOUT:
                return None
    return bytes(buf)

def read_packet(ser):
    """
    Read one full packet:
      [0x00, 0x00] + u32 sequence_id + u32 teensy_time_us + 12*u16 + 12*u16
    Returns (coinft1_raw (12,), coinft2_raw (12,), sequence_id, teensy_time_us),
    or None on failure.
    """
    # Find header (simple resync)
    head = read_exact(ser, HEADER_LEN)
    if head is None:
        return None
    if head != HEADER_BYTES:
        # Try to resync by sliding until we see header
        # (Simple approach: keep reading until we hit header, up to some bytes)
        # You can make this smarter if needed.
        ser.reset_input_buffer()
        return None

    body = read_exact(ser, METADATA_LEN + COINFT_BYTES * 2)
    if body is None or len(body) != METADATA_LEN + COINFT_BYTES * 2:
        return None

    sequence_id, teensy_time_us = struct.unpack('<II', body[:METADATA_LEN])

    # Unpack two blocks of 12*uint16 little-endian
    vals = struct.unpack('<' + 'H'* (COINFT_CH*2), body[METADATA_LEN:])
    data1 = np.array(vals[:COINFT_CH], dtype=np.float64)
    data2 = np.array(vals[COINFT_CH:], dtype=np.float64)
    return data1, data2, sequence_id, teensy_time_us

# =========================
# Main
# =========================
def mainLoopTwoSensors(args):
    savingFileName = args.filename
    run_duration = args.duration
    result_dir = args.result_dir
    os.makedirs(result_dir, exist_ok=True)

    # ---- ONNX sessions & norms ----
    if len(args.models) != 2 or len(args.norms) != 2:
        raise ValueError("This UART reader expects exactly TWO CoinFT sensors.")
    sessions = [ort.InferenceSession(p) for p in args.models]
    norms = load_norms(args.norms)

    # ---- Serial ----
    print(f"Opening UART: {args.port} @ {BAUD_RATE}")
    ser = serial.Serial(args.port, BAUD_RATE, timeout=READ_TIMEOUT)
    start_stream(ser, simulate=args.simulate)
    print("Started CoinFT streaming.")

    # ---- Offsets (tare) ----
    print("Collecting offsets...")
    offset_lists = [[], []]  # for sensor1, sensor2
    count_offset = 0
    while count_offset < INITIAL_SAMPLES:
        pkt = read_packet(ser)
        if pkt is None:
            continue
        d1, d2, _, _ = pkt
        offset_lists[0].append(d1)
        offset_lists[1].append(d2)
        count_offset += 1

    offsets = []
    for i in range(2):
        arr = np.vstack(offset_lists[i])
        # Ignore the first IGNORED_SAMPLES before averaging
        offsets.append(np.mean(arr[IGNORED_SAMPLES:], axis=0))

    # ---- Buffers ----
    max_samples = int((SAMPLING_RATE + 10) * (run_duration + 5))
    data_bufs = [np.zeros((max_samples, NUM_DATA_ENTRY), dtype=float) for _ in range(2)]
    counts    = [0, 0]
    ma_buffers = [[], []]

    # ---- Time base ----
    start_ntp_time   = get_ntp_time()
    start_local_time = time.time()
    print("Beginning acquisition...")

    # ---- Live plot setup (optional) ----
    if args.live_plot:
        plt.ion()
        figs, force_axes, moment_axes, force_lines, moment_lines = [], [], [], [], []
        time_data = [[], []]
        fx_data   = [[], []]
        fy_data   = [[], []]
        fz_data   = [[], []]
        mx_data   = [[], []]
        my_data   = [[], []]
        mz_data   = [[], []]
        plotted_counts = [0, 0]
        for i in range(2):
            fig, (ax_f, ax_m) = plt.subplots(2, 1, figsize=(8, 6))
            fig.suptitle(f"Sensor {i} ({FILE_NAME_ANNOTATIONS[i]})")
            lf, = ax_f.plot([], [], label='Fx')
            ly, = ax_f.plot([], [], label='Fy')
            lz, = ax_f.plot([], [], label='Fz')
            ax_f.legend(); ax_f.set_ylabel("Force [N]"); ax_f.grid()
            lmx, = ax_m.plot([], [], label='Mx')
            lmy_,= ax_m.plot([], [], label='My')
            lmz_,= ax_m.plot([], [], label='Mz')
            ax_m.legend(); ax_m.set_ylabel("Moment [Nm]"); ax_m.set_xlabel("Time [s]"); ax_m.grid()
            figs.append(fig); force_axes.append(ax_f); moment_axes.append(ax_m)
            force_lines.append((lf, ly, lz)); moment_lines.append((lmx, lmy_, lmz_))

    # ---- Main loop ----
    try:
        while (time.time() - start_local_time) < run_duration:
            pkt = read_packet(ser)
            if pkt is None:
                continue
            raw1, raw2, sequence_id, teensy_time_us = pkt  # uint16 arrays as float64

            # Timestamp (NTP-synchronized)
            current_time = start_ntp_time + (time.time() - start_local_time)

            # For each sensor: offset, normalize, ONNX, denorm, moving average, store
            for idx, (raw, offset, sess, consts) in enumerate(
                [(raw1, offsets[0], sessions[0], norms[0]),
                 (raw2, offsets[1], sessions[1], norms[1])]
            ):
                if counts[idx] >= max_samples:
                    continue

                offset_data = raw - offset
                x_n = (offset_data.astype(np.float32) - consts['mu_x']) / consts['sd_x']
                x_n = x_n.reshape(1, 12)
                input_name = sess.get_inputs()[0].name
                ft_cal_n = sess.run(None, {input_name: x_n})[0].flatten()    # (6,)
                ft_cal   = (ft_cal_n * consts['sd_y'] + consts['mu_y']).flatten()

                # moving average
                ma_buffers[idx].append(ft_cal)
                if len(ma_buffers[idx]) > args.window:
                    ma_buffers[idx].pop(0)
                ft_avg = np.mean(ma_buffers[idx], axis=0)

                # store
                row = data_bufs[idx][counts[idx], :]
                row[0] = current_time
                row[1:7] = ft_avg
                row[7:19] = offset_data
                counts[idx] += 1

            # Debug wrench on combined forces in left CoinFT frame
            if args.debug_wrench and counts[1] > 0 and (counts[1] % debug_wrench_every == 0):
                # last samples
                left_f  = copy.deepcopy(data_bufs[0][counts[0]-1, 1:4])
                right_f = copy.deepcopy(data_bufs[1][counts[1]-1, 1:4])
                # reflect right -> left frame (y,z flipped)
                right_f[1] = -right_f[1]
                right_f[2] = -right_f[2]
                f_sum = left_f + right_f
                print(f"Left: {left_f}, Right(leftCFTframe): {right_f}, Sum: {f_sum}")
                print(f"Norm: {np.linalg.norm(f_sum)}")

            # Live plot (throttled)
            if args.live_plot:
                for i in range(2):
                    if counts[i] > 0:
                        if counts[i] % PLOT_EVERY == 0:
                            rel_t = current_time - start_ntp_time
                            # get last avg FT
                            last_ft = data_bufs[i][counts[i]-1, 1:7]
                            time_data[i].append(rel_t)
                            fx_data[i].append(last_ft[0]); fy_data[i].append(last_ft[1]); fz_data[i].append(last_ft[2])
                            mx_data[i].append(last_ft[3]); my_data[i].append(last_ft[4]); mz_data[i].append(last_ft[5])
                            # clip to TIME_HISTORY
                            while (time_data[i][-1] - time_data[i][0]) > TIME_HISTORY:
                                time_data[i].pop(0); fx_data[i].pop(0); fy_data[i].pop(0)
                                fz_data[i].pop(0);   mx_data[i].pop(0); my_data[i].pop(0); mz_data[i].pop(0)
                            # update lines
                            lf, ly, lz = force_lines[i]
                            lmx, lmy_, lmz_ = moment_lines[i]
                            lf.set_data(time_data[i], fx_data[i]); ly.set_data(time_data[i], fy_data[i]); lz.set_data(time_data[i], fz_data[i])
                            lmx.set_data(time_data[i], mx_data[i]); lmy_.set_data(time_data[i], my_data[i]); lmz_.set_data(time_data[i], mz_data[i])
                            force_axes[i].relim(); force_axes[i].autoscale_view()
                            moment_axes[i].relim(); moment_axes[i].autoscale_view()
                plt.pause(0.001)

    finally:
        # Stop stream, close serial
        print("Stopping acquisition.")
        try:
            ser.write(b'i')
            time.sleep(0.02)
        except Exception:
            pass
        ser.close()

    # ---- Trim & report ----
    trimmed = min(counts)
    for i in range(2):
        data_bufs[i] = data_bufs[i][:trimmed, :]
    print("Finished collecting data.")
    print(f"Sensor LF samples: {counts[0]}; Sensor RF samples: {counts[1]}")
    print(f"Trimmed to {trimmed} samples.")

    # ---- Static plots ----
    plt.ioff()
    if args.static_plot and trimmed > 0:
        for i in range(2):
            sensor_label = f"Sensor_{i}_{FILE_NAME_ANNOTATIONS[i]}"
            # Forces
            plt.figure()
            plt.plot(data_bufs[i][:, 1], label='Fx')
            plt.plot(data_bufs[i][:, 2], label='Fy')
            plt.plot(data_bufs[i][:, 3], label='Fz')
            plt.ylabel('Force [N]'); plt.xlabel('Sample')
            plt.title(f"{sensor_label} - Forces (static)")
            plt.grid(); plt.legend()
            # Moments
            plt.figure()
            plt.plot(data_bufs[i][:, 4], label='Mx')
            plt.plot(data_bufs[i][:, 5], label='My')
            plt.plot(data_bufs[i][:, 6], label='Mz')
            plt.ylabel('Moment [Nm]'); plt.xlabel('Sample')
            plt.title(f"{sensor_label} - Moments (static)")
            plt.grid(); plt.legend()
        plt.show()

    if not args.live_plot:
        # ---- Save CSVs (per-sensor) ----
        date_dir = datetime.datetime.now().strftime("%y%m%d")
        out_dir = os.path.join(result_dir, date_dir)
        os.makedirs(out_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%y%m%d_%H%M%S_")
        header_str = "Timestamp,Fx,Fy,Fz,Mx,My,Mz," + ",".join([f"C{i+1}" for i in range(NUM_RAW_CHANNELS)])

        for i in range(2):
            fname = f'UMIFT_data_{stamp}{savingFileName}_{FILE_NAME_ANNOTATIONS[i]}.csv'
            fpath = os.path.join(out_dir, fname)
            np.savetxt(fpath, data_bufs[i], delimiter=",", header=header_str, comments='')
            print(f"Saved {FILE_NAME_ANNOTATIONS[i]} to: {fpath}")

        print("Data Collection and Saving Complete!.")
    else:
        print("Disable live plot to save temporally consistent data.")

# =========================
# CLI
# =========================
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="CoinFT UART Data Collection")
    
    # Positional arguments (keeping original flow)
    parser.add_argument("filename", type=str, help="Filename for saving CSV")
    parser.add_argument("duration", type=float, help="Run duration in seconds")
    parser.add_argument("result_dir", type=str, help="Output directory")

    # Configurable knobs (Defaults set here)
    parser.add_argument("--port", type=str, default="/dev/tty.usbmodem179386601", help="Serial port")
    parser.add_argument("--window", type=int, default=10, help="Moving average window")
    parser.add_argument("--models", nargs='+', default=[], help="List of model paths")
    parser.add_argument("--norms", nargs='+', default=[], help="List of norm constant paths")
    parser.add_argument("--simulate", action="store_true", help="Use Teensy-generated mock packets")
    
    # Boolean flags (store_true means providing the flag sets it to True)
    parser.add_argument("--static_plot", action="store_true", help="Enable static plot at end")
    parser.add_argument("--live_plot", action="store_true", help="Enable live plotting")
    parser.add_argument("--debug_wrench", action="store_true", help="Enable wrench debug prints")

    args = parser.parse_args()
    mainLoopTwoSensors(args)
