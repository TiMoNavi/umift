#!/usr/bin/env python3
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_ROOT = REPO_ROOT / "modules" / "02_iphone_gripper"
IOS_APP_ROOT = REPO_ROOT / "modules" / "02_iphone_gripper" / "ios_app" / "legacy" / "UMIFTiPhoneCaptureMVP"
PROJECT_PATH = IOS_APP_ROOT / "UMIFTiPhoneCaptureMVP.xcodeproj"
DEFAULT_DEVICE = "6805CFA8-2AA2-5509-9D00-D96FF37FA6CC"
DEFAULT_XCODE_DESTINATION_ID = "00008130-000E2DA10141001C"
DEFAULT_BUNDLE_ID = "com.local.umift.capture.mvp"
DEFAULT_DEVELOPER_DIR = "/Applications/Xcode.app/Contents/Developer"
DEFAULT_DERIVED_DATA = MODULE_ROOT / ".deriveddata" / "iphone_capture"
PULL_SCRIPT = REPO_ROOT / "modules" / "02_iphone_gripper" / "tools" / "pull_latest_iphone_demo.py"


def run(cmd, env=None):
    print("+", " ".join(str(x) for x in cmd))
    try:
        subprocess.run(cmd, check=True, env=env)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc


def build_app(args, env):
    derived_data = Path(args.derived_data).resolve()
    derived_data.mkdir(parents=True, exist_ok=True)
    cmd = [
        "xcodebuild",
        "-project", str(PROJECT_PATH),
        "-scheme", "UMIFTiPhoneCaptureMVP",
        "-destination", f"id={args.xcode_destination_id}",
        "-configuration", "Debug",
        "-derivedDataPath", str(derived_data),
        "-allowProvisioningUpdates",
        "build",
    ]
    run(cmd, env=env)
    app_path = derived_data / "Build" / "Products" / "Debug-iphoneos" / "UMIFTiPhoneCaptureMVP.app"
    if not app_path.exists():
        raise SystemExit(f"Expected built app at {app_path}, but it does not exist.")
    return app_path


def install_app(args, env, app_path):
    cmd = [
        "xcrun", "devicectl", "device", "install", "app",
        "--device", args.device,
        str(app_path),
    ]
    run(cmd, env=env)


def launch_app(args, env):
    cmd = [
        "xcrun", "devicectl", "device", "process", "launch",
        "--device", args.device,
        "--terminate-existing",
    ]
    if args.console:
        cmd.append("--console")
    cmd.append(args.bundle_id)
    cmd.extend([
        "--session-name", args.session_name,
        "--side", args.side,
        "--type", args.recording_type,
        "--auto-calibrate",
        "--auto-start",
        "--auto-stop-after", str(args.duration_s),
    ])
    if args.exit_after_save:
        cmd.append("--exit-after-save")
    try:
        run(cmd, env=env)
    except SystemExit as exc:
        print("Launch failed.")
        print("Most common cause on a physical iPhone: the device is still considered locked by iOS.")
        print("Unlock the phone once, keep it on the home screen or app foreground, then rerun with:")
        print(f"  python3 {Path(__file__).resolve()} --session-name {args.session_name} --duration-s {args.duration_s} --skip-build --skip-install --pull-after --pull-settle-s {args.pull_settle_s} --exit-after-save")
        raise exc


def pull_demo(args, env):
    cmd = [
        sys.executable,
        str(PULL_SCRIPT),
        "--device", args.device,
        "--bundle-id", args.bundle_id,
        "--developer-dir", args.developer_dir,
    ]
    if args.pull_destination:
        cmd.extend(["--destination", str(Path(args.pull_destination).resolve())])
    run(cmd, env=env)


def main():
    parser = argparse.ArgumentParser(description="Legacy MVP helper. Current app is ios_app/UMIFTiPhoneCaptureCore.")
    parser.add_argument(
        "--allow-legacy-mvp",
        action="store_true",
        help="Required to run this archived UMIFTiPhoneCaptureMVP workflow.",
    )
    parser.add_argument("--session-name", required=True)
    parser.add_argument("--side", choices=["left", "right"], default="right")
    parser.add_argument("--recording-type", choices=["demonstration", "grippercalibration", "qrcalibration"], default="demonstration")
    parser.add_argument("--duration-s", type=float, default=15.0)
    parser.add_argument("--device", default=DEFAULT_DEVICE)
    parser.add_argument("--xcode-destination-id", default=DEFAULT_XCODE_DESTINATION_ID)
    parser.add_argument("--bundle-id", default=DEFAULT_BUNDLE_ID)
    parser.add_argument("--developer-dir", default=DEFAULT_DEVELOPER_DIR)
    parser.add_argument("--derived-data", default=str(DEFAULT_DERIVED_DATA))
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--skip-launch", action="store_true")
    parser.add_argument("--pull-after", action="store_true")
    parser.add_argument("--pull-settle-s", type=float, default=8.0)
    parser.add_argument("--pull-destination")
    parser.add_argument("--console", action="store_true", help="Attach launch to the remote app console. This blocks until the app exits.")
    parser.add_argument("--exit-after-save", action="store_true", help="Ask the app to exit after saving. Intended for Debug builds only.")
    args = parser.parse_args()

    if not args.allow_legacy_mvp:
        raise SystemExit(
            "This script targets archived ios_app/legacy/UMIFTiPhoneCaptureMVP. "
            "Use ios_app/UMIFTiPhoneCaptureCore for current work, or rerun with "
            "--allow-legacy-mvp if you intentionally need the old MVP flow."
        )

    if not args.skip_launch:
        print("NOTE: Launch automation will attempt calibration from the current live pose before auto-starting.")
        print("      Place the iPhone flat in landscape with its long edge facing the intended +X direction.")

    env = os.environ.copy()
    env["DEVELOPER_DIR"] = args.developer_dir

    app_path = Path(args.derived_data).resolve() / "Build" / "Products" / "Debug-iphoneos" / "UMIFTiPhoneCaptureMVP.app"
    if not args.skip_build:
        app_path = build_app(args, env)
    elif not app_path.exists():
        raise SystemExit(f"--skip-build was given, but no built app exists at {app_path}")

    if not args.skip_install:
        install_app(args, env, app_path)

    if not args.skip_launch:
        launch_app(args, env)

    if args.pull_after:
        if args.console and not args.exit_after_save:
            raise SystemExit("--pull-after cannot be combined with --console unless --exit-after-save is enabled.")
        wait_s = max(0.0, args.duration_s + args.pull_settle_s)
        print(f"Waiting {wait_s:.1f}s before pulling saved demo...")
        time.sleep(wait_s)
        pull_demo(args, env)


if __name__ == "__main__":
    main()
