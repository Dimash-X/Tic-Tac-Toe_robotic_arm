#!/usr/bin/env python3
"""Local Pi keyboard control.

Live by default; --dry-run prevents serial access.
Tap a direction to keep moving until stopped or blocked.
"""

import argparse
import json
import select
import signal
import time

from support import config
from support.kinematics import Robot
from support.cartesian_endpoints import (
    CartesianSession,
    CartesianTargets,
    NoReachableTarget,
)
from support.keyboard_input import detect_keyboard
from support.servo_bus import ServoBus, calibrated_joints


def print_manual(live, keyboard):
    mode = (
        'LIVE - automatic home and wrist levelling'
        if live else
        'DRY RUN - no motor connection'
    )
    shoulder_range = config.settings(direct=True)[
        'servos'
    ]['shoulder']['safe_degrees']

    print(f"""
ROBOT ARM - CONTINUOUS CARTESIAN CONTROLS
{mode}
Program: {__file__}
Keyboard: {keyboard}
Shoulder range: {shoulder_range} deg
Startup pose: {config.STARTING_POSE_DEGREES}, then level the wrist

  KEY                 ACTION
  W / S               Move the tool up / down
  Up / Down           Move the tool forward / backward in the yaw plane
  Left / Right        Rotate the base (existing directions preserved)
  A / D               Rotate the wrist directly (motor 4, + / -)
  J / L               Open / close the gripper
  Space               Stop and hold (also cancels startup/planning)
  E                   Retry after a warning/fault
  Q                   Hold and quit

HOW TO USE
  Wait for startup to finish.
  Tap a direction once to keep moving.
  Tap another direction to change direction.
  Press Space to stop and hold.
  After stopping, tap a direction to move again.
  After a fault, press E, then choose a direction.

MOVEMENT
  Motion is planned in successive segments.
  Planning segment: {config.CARTESIAN_TARGET_DISTANCE_M * 100:g} cm or {config.CARTESIAN_TARGET_ANGLE_DEGREES:g} degrees.
  Motion stops when no further safe, reachable target can be planned.
  Small pauses may occur between segments.
  Stop and hold does not switch off motor power.

Normal operation is quiet; warnings appear below.
""", flush=True)


def run_keys(args):
    from evdev import InputDevice, ecodes, list_devices

    if args.list_keyboards:
        for path in list_devices():
            dev = InputDevice(path)
            try:
                if ecodes.KEY_W in dev.capabilities().get(
                    ecodes.EV_KEY, []
                ):
                    print(path, dev.name)
            finally:
                dev.close()
        return

    path = args.keyboard or detect_keyboard(
        InputDevice,
        ecodes,
        list_devices,
    )

    settings = config.settings(direct=True)
    settings.update(
        port=args.port,
        baudrate=args.baudrate,
        sync_read=False,
    )

    maps = calibrated_joints(settings)
    robot = Robot()

    codes = {
        getattr(ecodes, 'KEY_' + key.upper()): key
        for key in (
            'w', 's',
            'up', 'down',
            'left', 'right',
            'a', 'd',
            'j', 'l',
            'space', 'e', 'q',
        )
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
    last_warning = None

    def stop_signal(*_):
        nonlocal quit_requested
        quit_requested = True

    def warn(message):
        nonlocal last_warning
        if message != last_warning:
            print('WARNING:', message, flush=True)
            last_warning = message

    signal.signal(signal.SIGINT, stop_signal)
    signal.signal(signal.SIGTERM, stop_signal)

    bus = None
    device = None
    session = None
    grabbed = False

    try:
        device = InputDevice(path)

        if ecodes.KEY_W not in device.capabilities().get(
            ecodes.EV_KEY, []
        ):
            raise ValueError('Selected device is not a keyboard')

        device.grab()
        grabbed = True

        if args.live:
            bus = ServoBus(settings, maps)

        session = CartesianSession(
            robot,
            maps,
            bus,
            home=config.STARTING_POSE_DEGREES,
            distance=config.CARTESIAN_TARGET_DISTANCE_M,
            angle_degrees=config.CARTESIAN_TARGET_ANGLE_DEGREES,
            speed=config.DIRECT_JOINT_SPEED_DEG_S,
            period=config.CONTROL_PERIOD_SECONDS,
            tracking_error=config.MAX_TRACKING_ERROR_DEG,
            timeout=config.LOOP_TIMEOUT_SECONDS,
            stall_seconds=config.DIRECT_STALL_SECONDS,
            progress_degrees=config.DIRECT_PROGRESS_DEGREES,
            on_warning=warn,
        )

        print_manual(args.live, path)
        session.start()
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

                    # Only physical key-down events; ignore autorepeat.
                    if event.type != ecodes.EV_KEY or event.value != 1:
                        continue

                    if event.code in modifiers:
                        session.pause()
                        continue

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

            # Also check physical state in case a press event was missed.
            # Keep Space effective during startup and planning as well.
            if ecodes.KEY_SPACE in active or active & modifiers:
                session.pause()
            else:
                session.tick()

            if not session.faulted:
                last_warning = None

            next_tick = max(
                now + session.period,
                time.monotonic(),
            )

    finally:
        # Attempt all cleanup steps even if an earlier one fails.
        try:
            if session:
                session.close()
        finally:
            try:
                if bus:
                    bus.close()
            finally:
                if device:
                    try:
                        if grabbed:
                            try:
                                device.ungrab()
                            except OSError:
                                pass
                    finally:
                        device.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        '--live',
        dest='live',
        action='store_true',
        default=None,
        help='Live mode (already the default)',
    )
    mode.add_argument(
        '--dry-run',
        dest='live',
        action='store_false',
        help='Keyboard control without accessing motors',
    )

    parser.add_argument(
        '--keyboard',
        help='Override the automatically detected Linux keyboard device',
    )
    parser.add_argument(
        '--port',
        default=config.PORT,
        help='Serial device (default: config.PORT)',
    )
    parser.add_argument(
        '--baudrate',
        type=int,
        default=config.BAUDRATE,
    )
    parser.add_argument(
        '--list-keyboards',
        action='store_true',
    )
    parser.add_argument(
        '--demo',
        action='store_true',
        help='Finite offline test; no keyboard/serial access',
    )

    args = parser.parse_args(argv)

    if args.demo and args.live is True:
        parser.error('--demo cannot be combined with --live')

    args.live = (
        not args.demo
        if args.live is None else
        args.live
    )

    return args


def main():
    args = parse_args()

    if args.demo:
        robot = Robot()
        targets = CartesianTargets(
            robot,
            calibrated_joints(config.settings(direct=True)),
            config.CARTESIAN_TARGET_DISTANCE_M,
            config.CARTESIAN_TARGET_ANGLE_DEGREES,
        )

        pose = targets.level(config.STARTING_POSE_DEGREES)
        endpoints = {}

        for key in (
            'w', 's',
            'up', 'down',
            'left', 'right',
            'a', 'd',
            'j', 'l',
        ):
            try:
                endpoints[key] = targets.plan(key, pose).tolist()
            except NoReachableTarget as ex:
                endpoints[key] = str(ex)

        print(json.dumps({
            'mode': 'dry-run',
            'degrees': pose.tolist(),
            'endpoints': endpoints,
        }))

    else:
        run_keys(args)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, ImportError) as ex:
        raise SystemExit(str(ex))
