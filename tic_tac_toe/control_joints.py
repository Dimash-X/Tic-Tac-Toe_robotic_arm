#!/usr/bin/env python3
"""Direct joint keyboard control, live by default.

No FK/IK; startup moves to the configured joint pose.
"""
import argparse
import select
import signal
import time

from support import config
from support.keyboard_input import detect_keyboard
from support.servo_bus import ServoBus, calibrated_joints, NAMES
from support.direct_joints import JointMotion, DirectSession, KEYS


def print_manual(dry_run, keyboard):
    mode = (
        'DRY RUN - simulation only; the real arm will not move'
        if dry_run else
        'LIVE - the arm moves to its starting pose automatically'
    )

    print(f"""
ROBOT ARM - KEYBOARD CONTROLS
{mode}
Keyboard: {keyboard}
Starting joint angles (degrees): {config.STARTING_POSE_DEGREES}

  KEY                 ACTION
  Left / Right        Rotate the base (+ / -)
  W / S               Move the shoulder joint (+ / -)
  Up / Down           Move the elbow joint (+ / -)
  A / D               Rotate the wrist joint (motor 4, + / -)
  J / L               Open / close the gripper
  Space               Stop and hold (also cancels startup)
  E                   Retry after a warning/fault
  Q                   Hold and quit

HOW TO USE
  Wait for the startup movement to finish before using direction keys.
  Tap a direction once to keep moving toward that joint's travel limit.
  Press Space to stop. Tap a direction again to resume.

NOTES
  Each direction key controls one joint, not the tool's straight-line motion.
  (+ / -) means increasing / decreasing the configured joint angle.
  Stop and hold does not mean motor power is switched off.

Warnings, if any, appear below.
""", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Do not open the servo bus',
    )
    parser.add_argument(
        '--keyboard',
        help='Override automatic Linux keyboard detection',
    )
    parser.add_argument('--port', default=config.PORT)
    parser.add_argument('--baudrate', type=int, default=config.BAUDRATE)
    args = parser.parse_args()

    from evdev import InputDevice, ecodes, list_devices

    path = args.keyboard or detect_keyboard(
        InputDevice, ecodes, list_devices
    )

    settings = config.settings(direct=True)
    settings.update(
        port=args.port,
        baudrate=args.baudrate,
        sync_read=False,
    )

    # Encoder mapping and limits only; no robot model.
    maps = calibrated_joints(settings)

    codes = {
        getattr(ecodes, 'KEY_' + key.upper()): key
        for key in (*KEYS, 'e', 'q', 'space')
    }
    modifiers = {
        ecodes.KEY_LEFTCTRL,
        ecodes.KEY_RIGHTCTRL,
        ecodes.KEY_LEFTALT,
        ecodes.KEY_RIGHTALT,
        ecodes.KEY_LEFTMETA,
        ecodes.KEY_RIGHTMETA,
    }

    quit_requested = False

    def stop_signal(*_):
        nonlocal quit_requested
        quit_requested = True

    signal.signal(signal.SIGINT, stop_signal)
    signal.signal(signal.SIGTERM, stop_signal)

    bus = None
    device = None
    session = None
    grabbed = False
    last_warning = None

    def warn(message):
        nonlocal last_warning
        if message != last_warning:
            print('WARNING:', message, flush=True)
            last_warning = message

    try:
        device = InputDevice(path)
        if ecodes.KEY_W not in device.capabilities().get(ecodes.EV_KEY, []):
            raise ValueError('Selected device is not a keyboard')

        device.grab()
        grabbed = True

        if not args.dry_run:
            bus = ServoBus(settings, maps)

        pose = (
            bus.read_degrees(check_mode=True)
            if bus else
            [config.REFERENCE_DEGREES[n] for n in NAMES]
        )

        motion = JointMotion(
            maps,
            pose,
            config.DIRECT_JOINT_SPEED_DEG_S,
        )
        session = DirectSession(
            motion,
            bus,
            period=config.CONTROL_PERIOD_SECONDS,
            tracking_error=config.MAX_TRACKING_ERROR_DEG,
            timeout=config.LOOP_TIMEOUT_SECONDS,
            stall_seconds=config.DIRECT_STALL_SECONDS,
            progress_degrees=config.DIRECT_PROGRESS_DEGREES,
            on_warning=warn,
        )

        print_manual(args.dry_run, path)
        session.home(config.STARTING_POSE_DEGREES)

        next_tick = time.monotonic()

        while not quit_requested:
            readable, _, _ = select.select(
                [device.fd],
                [],
                [],
                max(0, next_tick - time.monotonic()),
            )

            if readable:
                for event in device.read():
                    if (
                        event.type == ecodes.EV_SYN
                        and event.code == ecodes.SYN_DROPPED
                    ):
                        session.pause('Keyboard events lost')
                        continue

                    if event.type != ecodes.EV_KEY or event.value != 1:
                        continue

                    if event.code in modifiers:
                        session.pause()

                    key = codes.get(event.code)

                    if key == 'q':
                        quit_requested = True
                        break

                    if key:
                        session.key(key)

                    if not session.faulted:
                        last_warning = None

            if quit_requested:
                break

            now = time.monotonic()
            if now < next_tick:
                continue

            active = set(device.active_keys())
            if session.ready and (
                ecodes.KEY_SPACE in active or active & modifiers
            ):
                session.pause()

            session.tick()

            if not session.faulted:
                last_warning = None

            next_tick = max(
                now + session.period,
                time.monotonic(),
            )

    finally:
        if session:
            session.pause()

        if bus:
            bus.close()

        if device:
            if grabbed:
                try:
                    device.ungrab()
                except OSError:
                    pass
            device.close()


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, ImportError) as ex:
        raise SystemExit(str(ex))
