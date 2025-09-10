# compact-binary-client-generic

A more complete UDP client that mirrors the Zencross client's behavior but uses standard UDP sockets instead of AT commands. It uses the shared `compact_binary_protocol` library and reads IMEI/ICCID and other settings from YAML.

## Usage
Run with YAML config:
```
python main.py --config sample_configs/pet_tracker.yaml
```

Override values from the YAML on the command line:
```
python main.py --config sample_configs/pet_tracker.yaml -s udp-eu.tartabit.com:10106 -i 120
```

## Configuration
- server: host:port
- interval: telemetry publish interval (seconds)
- readings: sampling interval for SensorMulti records (seconds)
- motionDuration, motionInterval: enable motion events when both > 0
- updateDuration (seconds), updateFailureRate (0..1): simulated update behavior
- imei (required), iccid (optional)
- code: customer code (hex) used in PowerOn P+
- mcc, mnc, rat: network values used in PowerOn P+
- location.type: simulated|cellid; for simulated you may provide location.lat/lon

## Notes
This client is transport-generic and uses Python's standard `socket` (UDP). It does not rely on Murata AT commands. It mirrors the behavior of the Zencross client, but IMEI/ICCID and network values come from YAML instead of AT commands.
