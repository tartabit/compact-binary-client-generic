"""
Generic UDP client using compact-binary-protocol over raw UDP sockets.

Features (mirrors zencross client, without AT commands):
- Sends Power On (P+) on startup using values from YAML/CLI (IMEI, code, mcc/mnc/rat, software/modem versions)
- Sends Configuration (C) after P+
- Periodically sends Telemetry (T) with simulated sensors and location
- Optional Motion events (M+/M-) if configured
- Handles inbound server commands on UDP socket: C (request), W (write), U+ (update request)
- Simulated update handling: sends U- "started" then final "success"/"failed" per YAML updateDuration/updateFailureRate

Configuration precedence: CLI > YAML > defaults.
Two sample YAMLs are available in sample_configs/.
"""
from __future__ import annotations
import socket
import time
import random
import os
import sys
from threading import Thread, Event, Lock

# Ensure local library is importable when running from monorepo
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'compact-binary-protocol'))

from compact_binary_protocol import (
    LocationData,
    SensorDataBasic,
    SensorDataNull,
    SensorDataMulti,
    SensorDataSteps,
    TelemetryPacket,
    PowerOnPacket,
    ConfigPacket,
    MotionStartPacket,
    MotionStopPacket,
    UpdateRequestPacket,
    UpdateStatusPacket,
    PacketDecoder,
    DataReader,
)
from config import get_config

# ----- Simple simulated sensor helpers -----
_last_lat = 45.448803450183924
_last_lon = -75.63533774831912
_battery = 100

def sim_temp():
    return round(random.uniform(18.0, 24.0), 1)

def sim_hum():
    return round(random.uniform(35.0, 50.0), 1)

def sim_loc_move():
    global _last_lat, _last_lon
    _last_lat += random.uniform(-0.0001, 0.0001)
    _last_lon += random.uniform(0.0001, 0.0003)
    return round(_last_lat, 6), round(_last_lon, 6)

def sim_batt():
    global _battery
    if random.uniform(1.0, 100.0) > 50.0:
        _battery -= 1
    if _battery < 5:
        _battery = 100
    return _battery

def sim_steps(duration: int) -> int:
    rate = random.uniform(0.8, 1.8)
    steps = int(rate * max(1, duration)) + random.randint(-5, 5)
    return max(0, steps)

# ----- UDP helpers -----

def parse_server(addr: str):
    if not addr or ':' not in addr:
        raise ValueError('server must be host:port')
    host, port_s = addr.split(':', 1)
    return host, int(port_s)


def encode_and_send(sock: socket.socket, server, reason: str, pkt):
    data = pkt.to_bytes()
    sock.sendto(data, server)
    pkt.print(reason)

# Ack tracking
_transaction_id = 0
_ack_events: dict[int, Event] = {}
_acked_ids: set[int] = set()
_ack_lock = Lock()

def next_txn() -> int:
    global _transaction_id
    _transaction_id = (_transaction_id + 1) % 65536
    return _transaction_id


def wait_ack(txn_id: int, timeout: float = 30.0) -> bool:
    with _ack_lock:
        ev = _ack_events.get(txn_id)
        if ev is None:
            ev = Event()
            _ack_events[txn_id] = ev
    return ev.wait(timeout)

# ----- Update handling -----

def simulate_update(sock: socket.socket, server, imei: str, txn_id: int, req: UpdateRequestPacket):
    try:
        started = UpdateStatusPacket(imei, txn_id, req.component, 'started', '')
        encode_and_send(sock, server, 'Update Started', started)
        try:
            duration = int(get_config('updateDuration', 5))
        except Exception:
            duration = 5
        time.sleep(max(0, duration))
        try:
            failure_rate = float(get_config('updateFailureRate', 0.0))
        except Exception:
            failure_rate = 0.0
        failure_rate = min(1.0, max(0.0, failure_rate))
        failed = random.random() < failure_rate
        if failed:
            final = UpdateStatusPacket(imei, txn_id, req.component, 'failed', 'Simulated failure')
            encode_and_send(sock, server, 'Update Failed', final)
        else:
            final = UpdateStatusPacket(imei, txn_id, req.component, 'success', '')
            encode_and_send(sock, server, 'Update Success', final)
    except Exception as ex:
        try:
            err = UpdateStatusPacket(imei, txn_id, req.component, 'failed', f'Exception: {ex}')
            encode_and_send(sock, server, 'Update Failed (exception)', err)
        except Exception:
            pass

# ----- Main client -----

def main():
    # Load config
    server_address = get_config('server')
    reporting_interval = int(get_config('interval', 120))
    reading_interval = int(get_config('readings', 60))
    imei = str(get_config('imei') or '')
    customer_id = get_config('code', '00000000')
    iccid = get_config('iccid')  # optional, for logs only
    mcc = str(get_config('mcc', '001'))
    mnc = str(get_config('mnc', '01'))
    rat = str(get_config('rat', 'LTE-M'))

    if not imei:
        print('IMEI must be provided via YAML or CLI (--imei).')
        return

    # Apply optional initial location from YAML
    try:
        lat = get_config('location.lat')
        lon = get_config('location.lon')
        if lat is not None and lon is not None:
            global _last_lat, _last_lon
            _last_lat = float(lat)
            _last_lon = float(lon)
    except Exception as e:
        print(f"Warning: invalid configured lat/lon: {e}")

    # Motion configuration
    motion_duration = get_config('motionDuration')
    motion_interval = get_config('motionInterval')
    motion_enabled = bool(motion_duration and motion_interval and int(motion_duration) > 0 and int(motion_interval) > 0)

    # Create UDP socket
    try:
        host, port = parse_server(server_address)
    except Exception:
        print('Invalid server address. Expected host:port')
        return

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)
    server = (host, port)

    # Start a background receiver thread
    def receiver():
        nonlocal server_address, reporting_interval, reading_interval
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except Exception:
                break
            try:
                # We expect hex string from the network in zencross, but here assume raw bytes already
                # Decode header directly from bytes
                b = data
                if not b:
                    continue
                version = b[0]
                cmd = (chr(b[1]) + chr(b[2])) if len(b) >= 3 else None
                txn_id = int.from_bytes(b[3:5], 'big') if len(b) >= 5 else None
                body = b[5:]
                print("*" * 50)
                print(f"  Received packet: {b.hex()}")
                print(f"  Decoded header: version={version}, command={cmd}, txn_id={txn_id}, data={body.hex()}")
                # Handle ACKs
                if cmd and cmd[0] == 'A':
                    with _ack_lock:
                        ev = _ack_events.pop(txn_id, None)
                    if ev is not None:
                        ev.set()
                    print("  Ack received")
                elif cmd and cmd[0] == 'C':
                    print("  Config request received; sending configuration")
                    pkt = ConfigPacket(imei, server_address, reporting_interval, reading_interval, txn_id)
                    encode_and_send(sock, server, 'Requested Configuration', pkt)
                elif cmd and cmd[0] == 'W':
                    print("  Write config received; applying")
                    try:
                        decoded = ConfigPacket.decode(imei, txn_id, body)
                        server_address = decoded.server_address if decoded.server_address else server_address
                        reporting_interval = decoded.reporting_interval
                        reading_interval = decoded.reading_interval
                    except Exception as e:
                        print(f"  Failed to decode W payload: {e}")
                    print(f"  New config: server={server_address}, interval={reporting_interval}, readings={reading_interval}")
                    pkt = ConfigPacket(imei, server_address, reporting_interval, reading_interval, txn_id)
                    encode_and_send(sock, server, 'Configuration Updated', pkt)
                elif cmd and cmd.startswith('U+'):
                    print("  Update request received")
                    try:
                        req = UpdateRequestPacket.decode(imei, txn_id, body)
                        print(f"  UpdateRequest: component={req.component}, url={req.url}, args={req.arguments}")
                        Thread(target=simulate_update, args=(sock, server, imei, txn_id, req), daemon=True).start()
                    except Exception as e:
                        print(f"  Failed to decode/process UpdateRequest: {e}")
                else:
                    print(f"  Unsupported command {cmd}")
                print("*" * 50)
            except Exception as e:
                print(f"Receiver error: {e}")

    Thread(target=receiver, daemon=True).start()

    # Helper to wait for ack
    def send_and_wait(reason: str, pkt) -> bool:
        txn_id = pkt.transaction_id
        with _ack_lock:
            _ack_events[txn_id] = Event()
        encode_and_send(sock, server, reason, pkt)
        ok = wait_ack(txn_id, timeout=30)
        if not ok:
            print(f"Warning: Ack timeout for txn {txn_id}")
        return ok

    # Next: send P+
    global _transaction_id
    _transaction_id = 0
    txn = next_txn()
    software_version = 'generic-udp-1.0.0'
    modem_version = 'generic'
    power = PowerOnPacket(imei, txn, customer_id, software_version, modem_version, mcc, mnc, rat)
    send_and_wait('Power On', power)

    # Send initial C
    txn = next_txn()
    cfg = ConfigPacket(imei, server_address, reporting_interval, reading_interval, txn)
    send_and_wait('Initial Configuration', cfg)

    # Telemetry loop with optional motion
    def make_location():
        loc_type = (get_config('location.type') or 'simulated').lower()
        if loc_type == 'cellid':
            # Use MCC/MNC from config and some fake LAC/Cell
            return LocationData.cell(mcc, mnc, '1234', '5678', 30)
        else:
            lat, lon = sim_loc_move()
            return LocationData.gnss(lat, lon)

    def telemetry_once(batch_seconds: int):
        ts = int(time.time())
        loc = make_location()
        # Fill a SensorDataMulti record bundle
        records = []
        n = max(1, int(batch_seconds / max(1, reading_interval)))
        first_ts = ts - (n - 1) * max(1, reading_interval)
        for i in range(n):
            records.append({'temperature': sim_temp(), 'humidity': sim_hum()})
        batt = sim_batt()
        rssi = 30
        sensor = SensorDataMulti(battery=batt, rssi=rssi, first_timestamp=first_ts, interval=max(1, reading_interval), records=records)
        txn = next_txn()
        pkt = TelemetryPacket(imei, ts, txn, loc, sensor)
        send_and_wait('Telemetry', pkt)

    motion_running = False
    last_motion_end = 0
    while True:
        start = time.time()
        if motion_enabled and not motion_running and (time.time() - last_motion_end) >= int(motion_interval or 0):
            # send motion start with Null sensor
            ts = int(time.time())
            loc = make_location()
            txn = next_txn()
            mstart = MotionStartPacket(imei, ts, txn, loc, SensorDataNull())
            send_and_wait('Motion Start', mstart)
            # later send stop
            time.sleep(int(motion_duration))
            steps = sim_steps(int(motion_duration))
            batt = sim_batt()
            rssi = 30
            loc = make_location()
            txn = next_txn()
            mstop = MotionStopPacket(imei, int(time.time()), txn, loc, SensorDataSteps(batt, rssi, steps))
            send_and_wait('Motion Stop', mstop)
            last_motion_end = time.time()
        else:
            telemetry_once(reporting_interval)
        # Sleep remaining time to align approximately with interval
        elapsed = time.time() - start
        sleep_for = max(0, int(reporting_interval) - int(elapsed))
        time.sleep(sleep_for if sleep_for < 60 else min(sleep_for, 60))  # wake periodically for receiver


if __name__ == '__main__':
    main()
