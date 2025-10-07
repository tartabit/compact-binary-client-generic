"""
Generic UDP client using compact-binary-protocol over raw UDP sockets.

Features (mirrors zencross client, without AT commands):
- Sends startup Telemetry (T) on first connect including CustomerId, Versions, NetworkInfo
- Sends Configuration (C) after startup telemetry
- Periodically sends Telemetry (T) with simulated sensors and location
- Motion events via Telemetry event field if configured
- Handles inbound server commands on UDP socket: C (request), W (write)
- Update packets removed from protocol; no simulated update handling

Configuration precedence: CLI > YAML > defaults.
Two sample YAMLs are available in sample_configs/.
"""
import socket
import time
import random
import os
import sys
from threading import Thread, Event, Lock

# Ensure local library is importable when running from monorepo
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'compact-binary-protocol'))

from compact_binary_protocol import (
    DataLocation,
    DataDeviceStatus,
    DataMulti,
    DataSteps,
    DataVersions,
    DataNetworkInfo,
    DataCustomerId,
    TelemetryPacket,
    ConfigPacket,
    DataKv,
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

def simulate_update(*args, **kwargs):
    # Update functionality removed from protocol; no-op
    return

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
                if not data:
                    continue
                packet_hex = data.hex()
                decoded = PacketDecoder.decode_packet_header(packet_hex)
                version, cmd, txn_id, body = decoded
                print("*" * 50)
                print(f"  Received packet: {packet_hex}")
                print(f"  Decoded header: version={version}, command={cmd}, txn_id={txn_id}, data={body.hex()}")
                # Handle ACKs
                if cmd and cmd[0] == 'A':
                    with _ack_lock:
                        ev = _ack_events.pop(txn_id, None)
                    if ev is not None:
                        ev.set()
                    print("  Ack received")
                elif cmd and cmd[0] == 'C' and cmd[1] == 'R':
                    print("  Config request received; sending configuration")
                    kv = DataKv({
                        'server': server_address,
                        'interval': str(reporting_interval),
                        'readings': str(reading_interval),
                    })
                    tpkt = TelemetryPacket(imei, int(time.time()), txn_id, 'C', kv)
                    encode_and_send(sock, server, 'Requested Configuration (Telemetry/Kv)', tpkt)
                elif cmd and cmd[0] == 'C' and cmd[1] == 'W':
                    print("  Write config received; applying")
                    try:
                        decoded = ConfigPacket.decode(imei, txn_id, body)
                        cfg = decoded.to_dict()
                        print(f"  Decoded config: {cfg}")
                        server_address = cfg.get('server', server_address) or server_address
                        try:
                            reporting_interval = int(cfg.get('interval', reporting_interval))
                        except Exception:
                            pass
                        try:
                            reading_interval = int(cfg.get('readings', reading_interval))
                        except Exception:
                            pass
                    except Exception as e:
                        print(f"  Failed to decode W payload: {e}")
                    print(f"  New config: server={server_address}, interval={reporting_interval}, readings={reading_interval}")
                    kv = DataKv({
                        'server': server_address,
                        'interval': str(reporting_interval),
                        'readings': str(reading_interval),
                    })
                    tpkt = TelemetryPacket(imei, int(time.time()), txn_id, 'C', kv)
                    encode_and_send(sock, server, 'Write Configuration (Telemetry/Kv)', tpkt)
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
    sensors = [
        DataCustomerId(customer_id),
        DataVersions(software_version, modem_version),
        DataNetworkInfo(mcc, mnc, rat),
    ]
    startup = TelemetryPacket(imei, int(time.time()), txn, 'P+', sensors)
    send_and_wait('Startup Telemetry', startup)

    # Send initial configuration via Telemetry (DataKv)
    txn = next_txn()
    kv = DataKv({
        'server': server_address,
        'interval': str(reporting_interval),
        'readings': str(reading_interval),
    })
    cfg_tpkt = TelemetryPacket(imei, int(time.time()), txn, 'C', kv)
    send_and_wait('Initial Configuration (Telemetry/Kv)', cfg_tpkt)

    # Telemetry loop with optional motion
    def make_location():
        loc_type = (get_config('location.type') or 'simulated').lower()
        if loc_type == 'cellid':
            # Use MCC/MNC from config and some fake LAC/Cell
            return DataLocation.cell(mcc, mnc, '1234', '5678', 30)
        else:
            lat, lon = sim_loc_move()
            return DataLocation.gnss(lat, lon)

    def telemetry_once(batch_seconds: int):
        ts = int(time.time())
        loc = make_location()
        # Publish location (no longer embedded in packets)
        try:
            print(f"Published Location: {loc.describe()}")
        except Exception:
            print("Published Location: (unavailable)")
        # Fill a SensorDataMulti record bundle
        records = []
        n = max(1, int(batch_seconds / max(1, reading_interval)))
        first_ts = ts - (n - 1) * max(1, reading_interval)
        for i in range(n):
            records.append({'temperature': sim_temp(), 'humidity': sim_hum()})
        batt = sim_batt()
        rssi = 30
        sensor = DataMulti(first_timestamp=first_ts, interval=max(1, reading_interval), records=records)
        txn = next_txn()
        pkt = TelemetryPacket(imei, ts, txn, 'T', [sensor, loc, DataDeviceStatus(battery=batt, rssi=rssi)])
        send_and_wait('Telemetry', pkt)

    motion_running = False
    last_motion_end = 0
    while True:
        start = time.time()
        if motion_enabled and not motion_running and (time.time() - last_motion_end) >= int(motion_interval or 0):
            # send motion start with Null sensor
            ts = int(time.time())
            loc = make_location()
            try:
                print(f"Published Location: {loc.describe()}")
            except Exception:
                print("Published Location: (unavailable)")
            txn = next_txn()
            mstart = TelemetryPacket(imei, ts, txn, 'M+', loc)
            send_and_wait('Motion Start', mstart)
            # later send stop
            time.sleep(int(motion_duration))
            steps = sim_steps(int(motion_duration))
            batt = sim_batt()
            rssi = 30
            loc = make_location()
            try:
                print(f"Published Location: {loc.describe()}")
            except Exception:
                print("Published Location: (unavailable)")
            txn = next_txn()
            mstop = TelemetryPacket(imei, int(time.time()), txn, 'M-', [loc, DataSteps(steps),DataDeviceStatus(batt, rssi)])
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
