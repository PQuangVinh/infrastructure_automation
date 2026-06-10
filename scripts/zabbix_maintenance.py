#!/usr/bin/env python3
"""Create or remove Zabbix maintenance windows for Ansible automation."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from config_common import bool_value, load_project_env, strip_quotes


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "outputs" / "zabbix_maintenance.json"


class ZabbixAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class ZabbixSettings:
    url: str
    token: str
    validate_certs: bool


class ZabbixAPI:
    def __init__(self, settings: ZabbixSettings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.request_id = 1

    def call(self, method: str, params: dict[str, Any] | None = None, *, auth: bool = True) -> Any:
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": self.request_id,
        }
        self.request_id += 1
        if auth:
            payload["auth"] = self.settings.token
        response = self.session.post(
            self.settings.url,
            json=payload,
            timeout=30,
            verify=self.settings.validate_certs,
        )
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            error = data["error"]
            raise ZabbixAPIError(f"{method}: {error.get('message')}: {error.get('data')}")
        return data.get("result")


def zabbix_settings() -> ZabbixSettings:
    scheme = "https" if bool_value(os.getenv("ZABBIX_API_USE_SSL"), False) else "http"
    host = os.getenv("ZABBIX_API_HOST", "192.168.80.20").strip()
    port = os.getenv("ZABBIX_API_PORT", "8080").strip()
    path = os.getenv("ZABBIX_URL_PATH", "").strip().strip("/")
    base = f"{scheme}://{host}:{port}"
    if path:
        base = f"{base}/{path}"
    token = strip_quotes(os.getenv("ANSIBLE_ZABBIX_AUTH_KEY", ""))
    if not token:
        raise SystemExit("Missing ANSIBLE_ZABBIX_AUTH_KEY.")
    return ZabbixSettings(
        url=f"{base}/api_jsonrpc.php",
        token=token,
        validate_certs=bool_value(os.getenv("ZABBIX_VALIDATE_CERTS"), False),
    )


def parse_duration(value: str) -> int:
    raw = str(value).strip().lower()
    if raw.isdigit():
        return int(raw)
    match = re.fullmatch(r"(\d+)([smhd])", raw)
    if not match:
        raise SystemExit(f"Invalid duration: {value}. Use seconds or suffix s/m/h/d.")
    number = int(match.group(1))
    unit = match.group(2)
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return number * multiplier


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def resolve_hosts(api: ZabbixAPI, host_names: list[str], hostgroup_names: list[str]) -> tuple[list[dict[str, str]], list[str]]:
    hosts: dict[str, dict[str, str]] = {}
    missing: list[str] = []

    if host_names:
        result = api.call(
            "host.get",
            {
                "output": ["hostid", "host", "name"],
                "filter": {"host": host_names},
            },
        )
        for host in result:
            hosts[host["host"]] = {"hostid": host["hostid"]}
        found = set(hosts)
        missing.extend([name for name in host_names if name not in found])

    for group_name in hostgroup_names:
        groups = api.call(
            "hostgroup.get",
            {
                "output": ["groupid", "name"],
                "filter": {"name": [group_name]},
                "selectHosts": ["hostid", "host"],
            },
        )
        if not groups:
            missing.append(f"group:{group_name}")
            continue
        for host in groups[0].get("hosts", []):
            hosts[host["host"]] = {"hostid": host["hostid"]}

    return list(hosts.values()), missing


def existing_maintenance(api: ZabbixAPI, name: str) -> list[dict[str, Any]]:
    return api.call(
        "maintenance.get",
        {
            "output": ["maintenanceid", "name", "active_since", "active_till"],
            "filter": {"name": [name]},
        },
    )


def start_maintenance(api: ZabbixAPI, args: argparse.Namespace, report: dict[str, Any]) -> None:
    host_names = split_csv(args.hosts)
    group_names = split_csv(args.hostgroups)
    if not host_names and not group_names:
        group_names = [args.default_hostgroup]

    hosts, missing = resolve_hosts(api, host_names, group_names)
    if not hosts:
        raise SystemExit("No Zabbix hosts resolved for maintenance.")

    now = int(time.time())
    duration = parse_duration(args.duration)
    payload = {
        "name": args.name,
        "active_since": now,
        "active_till": now + duration,
        "maintenance_type": "0",
        "description": "Created by Ansible automation from NetBox workflow.",
        "hosts": hosts,
        "timeperiods": [
            {
                "timeperiod_type": "0",
                "period": duration,
            }
        ],
    }

    existing = existing_maintenance(api, args.name)
    if existing:
        payload["maintenanceid"] = existing[0]["maintenanceid"]
        if not args.dry_run:
            api.call("maintenance.update", payload)
        action = "updated"
        maintenanceid = existing[0]["maintenanceid"]
    else:
        if args.dry_run:
            maintenanceid = "dry-run"
        else:
            result = api.call("maintenance.create", payload)
            maintenanceid = result["maintenanceids"][0]
        action = "created"

    report.update(
        {
            "action": action,
            "maintenanceid": maintenanceid,
            "hosts_count": len(hosts),
            "missing": missing,
            "active_till": payload["active_till"],
        }
    )


def stop_maintenance(api: ZabbixAPI, args: argparse.Namespace, report: dict[str, Any]) -> None:
    existing = existing_maintenance(api, args.name)
    if not existing:
        report.update({"action": "not_found", "maintenanceid": None})
        return
    ids = [item["maintenanceid"] for item in existing]
    if not args.dry_run:
        api.call("maintenance.delete", ids)
    report.update({"action": "deleted", "maintenanceids": ids})


def write_report(report: dict[str, Any], output_path: Path | None) -> None:
    content = json.dumps(report, indent=2, ensure_ascii=False)
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content + "\n", encoding="utf-8")
    print(content)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage Zabbix maintenance for Ansible runs.")
    parser.add_argument("state", choices=["start", "stop"], help="Create/update or remove a maintenance window.")
    parser.add_argument("--name", default="Ansible NetBox Deploy", help="Maintenance name.")
    parser.add_argument("--duration", default="30m", help="Maintenance duration for start, e.g. 30m, 1h.")
    parser.add_argument("--hosts", default="", help="Comma-separated Zabbix host names.")
    parser.add_argument("--hostgroups", default="", help="Comma-separated Zabbix host group names.")
    parser.add_argument("--default-hostgroup", default="Network Devices", help="Used when --hosts/--hostgroups are omitted.")
    parser.add_argument("--dry-run", action="store_true", help="Report actions without changing Zabbix.")
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT), help="Write result JSON to this path.")
    parser.add_argument("--no-output-json", action="store_true", help="Do not write a result file.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_project_env(override=True)
    api = ZabbixAPI(zabbix_settings())
    report: dict[str, Any] = {
        "state": args.state,
        "dry_run": bool(args.dry_run),
        "zabbix_version": api.call("apiinfo.version", auth=False),
    }
    if args.state == "start":
        start_maintenance(api, args, report)
    else:
        stop_maintenance(api, args, report)
    report["changed"] = report.get("action") in {"created", "updated", "deleted"}
    output_path = None if args.no_output_json else Path(args.output_json)
    write_report(report, output_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except requests.RequestException as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except ZabbixAPIError as exc:
        print(f"Zabbix API error: {exc}", file=sys.stderr)
        raise SystemExit(1)
