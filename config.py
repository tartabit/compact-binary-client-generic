"""
Configuration loader for Generic UDP Client.

- Parses command-line arguments
- Loads optional YAML configuration file
- Exposes get_config(key) with precedence: CLI > YAML > DEFAULTS
- Supports dotted notation for nested YAML keys (e.g., "location.lat")

This is adapted from the zencross client but removes modem/AT specifics.
"""
from __future__ import annotations
import os
import argparse
from typing import Any, Dict, Optional

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

# Defaults tailored for generic client
DEFAULTS: Dict[str, Any] = {
    'server': 'udp-eu.tartabit.com:10106',
    'interval': 120,
    'readings': 60,
    'imei': None,
    'iccid': None,
    'code': '00000000',
    'mcc': '001',
    'mnc': '01',
    'rat': 'LTE-M',
    'updateDuration': 5,
    'updateFailureRate': 0.0,
}

_parser = argparse.ArgumentParser(
    prog='Generic UDP Client',
    description='UDP client using compact-binary protocol without AT commands',
)
_parser.add_argument('-s', '--server', help='Server address "host:port"', default=None)
_parser.add_argument('-i', '--interval', help='Reporting interval in seconds', type=int, default=None)
_parser.add_argument('-r', '--readings', help='Reading interval in seconds', type=int, default=None)
_parser.add_argument('-m', '--imei', help='IMEI (15 digits) provided via config/CLI', default=None)
_parser.add_argument('--iccid', help='ICCID string (optional, for logs only)', default=None)
_parser.add_argument('-c', '--code', help='Customer code (even-length hex)', default=None)
_parser.add_argument('--mcc', help='Mobile Country Code string', default=None)
_parser.add_argument('--mnc', help='Mobile Network Code string', default=None)
_parser.add_argument('--rat', help='Radio access tech string (e.g., LTE-M, NB-IoT)', default=None)
_parser.add_argument('--config', help='Path to YAML config file', default=None)

_args, _unknown = _parser.parse_known_args()

_config: Dict[str, Any] = {}
_config_path = _args.config or os.path.join(os.path.dirname(__file__), 'config.yaml')
if _config_path and os.path.exists(_config_path) and yaml is not None:
    try:
        with open(_config_path, 'r') as f:
            loaded = yaml.safe_load(f) or {}
            if isinstance(loaded, dict):
                _config = loaded
    except Exception as e:
        print(f"Warning: Failed to load config file {_config_path}: {e}")


def _get_from_dict_path(d: Dict[str, Any], dotted: str) -> Optional[Any]:
    cur: Any = d
    for part in dotted.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def get_config(key: str, default: Any = None) -> Any:
    # CLI first for flat keys
    if '.' not in key:
        cli_val = getattr(_args, key, None)
        if cli_val is not None:
            return cli_val

    # YAML via dotted
    yaml_val: Any = None
    if isinstance(_config, dict):
        if '.' in key:
            yaml_val = _get_from_dict_path(_config, key)
            if yaml_val is None and key in ('location.lat', 'location.lon'):
                flat_key = 'lat' if key.endswith('.lat') else 'lon'
                yaml_val = _config.get(flat_key)
        else:
            yaml_val = _config.get(key)
    if yaml_val is not None:
        return yaml_val

    if '.' not in key and key in DEFAULTS:
        return DEFAULTS[key]

    return default

__all__ = ['get_config', 'DEFAULTS']
