#!/usr/bin/env python3
"""Configure Zabbix monitoring policy for NetBox-managed infrastructure.

This script keeps host synchronization separate from alerting policy:
- update Telegram media type parameters when Telegram env vars are present;
- attach Telegram media to the target Zabbix user;
- create/update a NetBox trigger action for Telegram notifications;
- merge NetBox monitoring macros into hosts tagged with source=netbox.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from config_common import bool_value, load_project_env, strip_quotes


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "outputs" / "zabbix_monitoring_policy.json"

SEVERITY_MAP = {
    "not_classified": 0,
    "not classified": 0,
    "information": 1,
    "info": 1,
    "warning": 2,
    "average": 3,
    "high": 4,
    "disaster": 5,
}

ROLE_MACROS = {
    "core": {
        "{$ICMP_LOSS_WARN}": "10",
        "{$ICMP_RESPONSE_TIME_WARN}": "0.15",
        "{$SNMP.TIMEOUT}": "5m",
    },
    "access": {
        "{$ICMP_LOSS_WARN}": "20",
        "{$ICMP_RESPONSE_TIME_WARN}": "0.25",
        "{$SNMP.TIMEOUT}": "5m",
    },
    "firewall": {
        "{$ICMP_LOSS_WARN}": "5",
        "{$ICMP_RESPONSE_TIME_WARN}": "0.10",
        "{$SNMP.TIMEOUT}": "5m",
    },
    "server": {
        "{$ICMP_LOSS_WARN}": "20",
        "{$ICMP_RESPONSE_TIME_WARN}": "0.25",
        "{$SNMP.TIMEOUT}": "5m",
    },
    "network": {
        "{$ICMP_LOSS_WARN}": "20",
        "{$ICMP_RESPONSE_TIME_WARN}": "0.20",
        "{$SNMP.TIMEOUT}": "5m",
    },
}


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


def severity_value(name: str) -> int:
    key = (name or "warning").strip().lower().replace("-", "_")
    if key not in SEVERITY_MAP:
        raise SystemExit(f"Unsupported Zabbix severity: {name}")
    return SEVERITY_MAP[key]


def get_one(api: ZabbixAPI, method: str, params: dict[str, Any], object_name: str) -> dict[str, Any]:
    result = api.call(method, params)
    if not result:
        raise SystemExit(f"{object_name} not found.")
    return result[0]


def desired_telegram_parameters(bot_token: str, parse_mode: str) -> list[dict[str, str]]:
    return [
        {"name": "api_token", "value": bot_token},
        {"name": "api_chat_id", "value": "{ALERT.SENDTO}"},
        {"name": "api_parse_mode", "value": parse_mode},
        {"name": "alert_subject", "value": "{ALERT.SUBJECT}"},
        {"name": "alert_message", "value": "{ALERT.MESSAGE}"},
        {"name": "event_source", "value": "{EVENT.SOURCE}"},
        {"name": "event_value", "value": "{EVENT.VALUE}"},
        {"name": "event_update_status", "value": "{EVENT.UPDATE.STATUS}"},
        {"name": "event_nseverity", "value": "{EVENT.NSEVERITY}"},
        {"name": "event_severity", "value": "{EVENT.SEVERITY}"},
        {"name": "event_update_nseverity", "value": "{EVENT.UPDATE.NSEVERITY}"},
        {"name": "event_update_severity", "value": "{EVENT.UPDATE.SEVERITY}"},
        {"name": "event_tags", "value": "{EVENT.TAGSJSON}"},
        {"name": "http_proxy", "value": ""},
    ]


def ensure_telegram_mediatype(api: ZabbixAPI, *, dry_run: bool, report: dict[str, Any]) -> str | None:
    bot_token = strip_quotes(os.getenv("TELEGRAM_BOT_TOKEN", ""))
    if not bot_token or bot_token.startswith("replace_with_"):
        report["telegram_mediatype"] = "skipped_missing_TELEGRAM_BOT_TOKEN"
        return None

    mediatype = get_one(
        api,
        "mediatype.get",
        {
            "output": ["mediatypeid", "name", "type", "status"],
            "filter": {"name": ["Telegram"]},
            "selectParameters": "extend",
        },
        "Telegram media type",
    )
    mediatypeid = mediatype["mediatypeid"]
    parse_mode = strip_quotes(os.getenv("TELEGRAM_PARSE_MODE", "Markdown")) or "Markdown"
    params = desired_telegram_parameters(bot_token, parse_mode)

    current_params = {p.get("name"): p.get("value", "") for p in mediatype.get("parameters", [])}
    desired_params = {p["name"]: p["value"] for p in params}
    needs_update = mediatype.get("status") != "0" or current_params != desired_params
    if needs_update and not dry_run:
        api.call(
            "mediatype.update",
            {
                "mediatypeid": mediatypeid,
                "status": "0",
                "parameters": params,
            },
        )
    report["telegram_mediatype"] = "updated" if needs_update else "ok"
    return mediatypeid


def ensure_user_telegram_media(
    api: ZabbixAPI,
    *,
    mediatypeid: str,
    dry_run: bool,
    report: dict[str, Any],
) -> str | None:
    chat_id = strip_quotes(os.getenv("TELEGRAM_CHAT_ID", ""))
    if not chat_id or chat_id.startswith("replace_with_"):
        report["telegram_user_media"] = "skipped_missing_TELEGRAM_CHAT_ID"
        return None

    username = strip_quotes(os.getenv("ZABBIX_TELEGRAM_USER", "Admin")) or "Admin"
    user = get_one(
        api,
        "user.get",
        {
            "output": ["userid", "username"],
            "filter": {"username": [username]},
            "selectMedias": ["mediaid", "mediatypeid", "sendto", "active", "severity", "period"],
        },
        f"Zabbix user {username}",
    )
    userid = user["userid"]
    medias = user.get("medias", [])
    new_media = {
        "mediatypeid": mediatypeid,
        "sendto": [chat_id],
        "active": "0",
        "severity": "63",
        "period": "1-7,00:00-24:00",
    }

    updated = False
    next_medias: list[dict[str, Any]] = []
    for media in medias:
        if media.get("mediatypeid") == mediatypeid:
            item = dict(new_media)
            if media.get("mediaid"):
                item["mediaid"] = media["mediaid"]
            next_medias.append(item)
            if normalize_sendto(media.get("sendto")) != [chat_id] or media.get("active") != "0":
                updated = True
        else:
            next_medias.append(media)
    if not any(media.get("mediatypeid") == mediatypeid for media in medias):
        next_medias.append(new_media)
        updated = True

    if updated and not dry_run:
        try:
            api.call("user.update", {"userid": userid, "medias": next_medias})
        except ZabbixAPIError:
            fallback_medias = []
            for media in next_medias:
                item = dict(media)
                if isinstance(item.get("sendto"), list) and len(item["sendto"]) == 1:
                    item["sendto"] = item["sendto"][0]
                fallback_medias.append(item)
            api.call("user.update", {"userid": userid, "medias": fallback_medias})

    report["telegram_user_media"] = "updated" if updated else "ok"
    return userid


def normalize_sendto(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def action_message() -> tuple[str, str]:
    subject = "[{EVENT.SEVERITY}] {HOST.NAME}: {EVENT.NAME}"
    message = "\n".join(
        [
            "Status: {EVENT.STATUS}",
            "Host: {HOST.NAME} ({HOST.CONN})",
            "Problem: {EVENT.NAME}",
            "Severity: {EVENT.SEVERITY}",
            "Operational data: {EVENT.OPDATA}",
            "Tags: {EVENT.TAGS}",
            "Time: {EVENT.DATE} {EVENT.TIME}",
            "Event ID: {EVENT.ID}",
            "Recovery: {EVENT.RECOVERY.DATE} {EVENT.RECOVERY.TIME}",
        ]
    )
    return subject, message


def ensure_telegram_action(
    api: ZabbixAPI,
    *,
    mediatypeid: str,
    userid: str,
    dry_run: bool,
    report: dict[str, Any],
) -> None:
    action_name = strip_quotes(os.getenv("ZABBIX_TELEGRAM_ACTION_NAME", "NetBox PROD Problems to Telegram"))
    min_severity = severity_value(os.getenv("ZABBIX_ALERT_MIN_SEVERITY", "warning"))
    notify_lab = bool_value(os.getenv("ZABBIX_NOTIFY_LAB"), False)
    target_env = strip_quotes(os.getenv("ZABBIX_ALERT_ENV", "prod")).lower() or "prod"
    esc_period = strip_quotes(os.getenv("ZABBIX_ACTION_ESC_PERIOD", "10m")) or "10m"
    subject, message = action_message()

    conditions = [
        {"conditiontype": "4", "operator": "5", "value": str(min_severity)},
        {"conditiontype": "26", "operator": "0", "value": "source", "value2": "netbox"},
        {"conditiontype": "26", "operator": "0", "value": "alert_route", "value2": "telegram"},
    ]
    if not notify_lab:
        conditions.append(
            {
                "conditiontype": "26",
                "operator": "0",
                "value": "environment",
                "value2": target_env,
            }
        )
    formula_terms = [chr(ord("A") + index) for index in range(len(conditions))]
    for condition, formulaid in zip(conditions, formula_terms):
        condition["formulaid"] = formulaid

    action_payload = {
        "name": action_name,
        "eventsource": "0",
        "status": "0",
        "esc_period": esc_period,
        "pause_suppressed": "1",
        "pause_symptoms": "1",
        "notify_if_canceled": "1",
        "filter": {"evaltype": "3", "formula": " and ".join(formula_terms), "conditions": conditions},
        "operations": [
            {
                "operationtype": "0",
                "esc_step_from": "1",
                "esc_step_to": "1",
                "esc_period": "0",
                "opconditions": [],
                "opmessage": {
                    "default_msg": "0",
                    "mediatypeid": mediatypeid,
                    "subject": subject,
                    "message": message,
                },
                "opmessage_usr": [{"userid": userid}],
            }
        ],
        "recovery_operations": [
            {
                "operationtype": "0",
                "opmessage": {
                    "default_msg": "0",
                    "mediatypeid": mediatypeid,
                    "subject": "Resolved: {HOST.NAME}: {EVENT.NAME}",
                    "message": message,
                },
                "opmessage_usr": [{"userid": userid}],
            }
        ],
        "update_operations": [
            {
                "operationtype": "0",
                "opmessage": {
                    "default_msg": "0",
                    "mediatypeid": mediatypeid,
                    "subject": "Updated: {HOST.NAME}: {EVENT.NAME}",
                    "message": "{USER.FULLNAME} updated event {EVENT.ID}: {EVENT.UPDATE.MESSAGE}",
                },
                "opmessage_usr": [{"userid": userid}],
            }
        ],
    }

    existing = api.call(
        "action.get",
        {
            "output": ["actionid", "name"],
            "filter": {"name": [action_name]},
        },
    )
    if existing:
        action_payload["actionid"] = existing[0]["actionid"]
        if not dry_run:
            api.call("action.update", action_payload)
        report["telegram_action"] = "updated"
    else:
        if not dry_run:
            api.call("action.create", action_payload)
        report["telegram_action"] = "created"


def tag_dict(host: dict[str, Any]) -> dict[str, str]:
    tags: dict[str, str] = {}
    for tag in host.get("tags", []):
        tags[str(tag.get("tag", ""))] = str(tag.get("value", ""))
    return tags


def derive_role_class(tags: dict[str, str]) -> str:
    role_class = tags.get("role_class", "").lower()
    if role_class:
        return role_class
    role = tags.get("role", "").lower()
    if "core" in role:
        return "core"
    if "acc" in role or "access" in role:
        return "access"
    if "fw" in role or "firewall" in role:
        return "firewall"
    if "server" in role or "srv" in role:
        return "server"
    return "network"


def desired_host_macros(role_class: str) -> dict[str, dict[str, str]]:
    ifalias_matches = strip_quotes(
        os.getenv("ZABBIX_IFALIAS_MATCHES", r"^(UPLINK|DOWNLINK|PEER|WAN|SRV|AP)(\s*\|.*)?$")
    )
    ifalias_not_matches = strip_quotes(
        os.getenv("ZABBIX_IFALIAS_NOT_MATCHES", r"^(USER|CAM|CAMERA)(\s*\|.*)?$")
    )
    base = {
        "{$NET.IF.IFALIAS.MATCHES}": {
            "value": ifalias_matches,
            "description": "NetBox policy: discover/alert only important described ports.",
        },
        "{$NET.IF.IFALIAS.NOT_MATCHES}": {
            "value": ifalias_not_matches,
            "description": "NetBox policy: suppress normal user/camera access ports.",
        },
        "{$IFCONTROL}": {
            "value": "1",
            "description": "Enable interface operational status triggers from SNMP templates.",
        },
    }
    for macro, value in ROLE_MACROS.get(role_class, ROLE_MACROS["network"]).items():
        base[macro] = {
            "value": value,
            "description": f"NetBox role policy for {role_class}.",
        }
    return base


def merge_host_macros(existing: list[dict[str, Any]], desired: dict[str, dict[str, str]]) -> tuple[list[dict[str, Any]], bool]:
    by_macro = {item.get("macro"): item for item in existing if item.get("macro")}
    merged: list[dict[str, Any]] = []
    changed = False

    for item in existing:
        macro = item.get("macro")
        if macro not in desired:
            merged.append(item)

    for macro, spec in desired.items():
        current = by_macro.get(macro)
        next_item = {
            "macro": macro,
            "value": spec["value"],
            "description": spec.get("description", ""),
        }
        if current and current.get("hostmacroid"):
            next_item["hostmacroid"] = current["hostmacroid"]
        if not current or current.get("value") != spec["value"] or current.get("description", "") != spec.get("description", ""):
            changed = True
        merged.append(next_item)

    return merged, changed


def ensure_netbox_host_macros(api: ZabbixAPI, *, dry_run: bool, report: dict[str, Any]) -> None:
    hosts = api.call(
        "host.get",
        {
            "output": ["hostid", "host", "name"],
            "selectTags": "extend",
            "selectMacros": "extend",
        },
    )
    updated_hosts: list[str] = []
    managed_hosts = 0
    for host in hosts:
        tags = tag_dict(host)
        if tags.get("source") != "netbox":
            continue
        managed_hosts += 1
        role_class = derive_role_class(tags)
        desired = desired_host_macros(role_class)
        merged, changed = merge_host_macros(host.get("macros", []), desired)
        if changed:
            updated_hosts.append(host["host"])
            if not dry_run:
                api.call("host.update", {"hostid": host["hostid"], "macros": merged})

    report["netbox_hosts_seen"] = managed_hosts
    report["host_macros_updated"] = updated_hosts


def write_report(report: dict[str, Any], output_path: Path | None) -> None:
    content = json.dumps(report, indent=2, ensure_ascii=False)
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content + "\n", encoding="utf-8")
    print(content)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Configure Zabbix monitoring policy for NetBox-managed hosts.")
    parser.add_argument("--dry-run", action="store_true", help="Read current Zabbix state and report planned changes only.")
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT), help="Write sanitized result JSON to this path.")
    parser.add_argument("--no-output-json", action="store_true", help="Do not write a result file.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_project_env(override=True)

    api = ZabbixAPI(zabbix_settings())
    report: dict[str, Any] = {
        "changed": False,
        "dry_run": bool(args.dry_run),
        "zabbix_version": api.call("apiinfo.version", auth=False),
    }

    mediatypeid = ensure_telegram_mediatype(api, dry_run=args.dry_run, report=report)
    userid = None
    if mediatypeid:
        userid = ensure_user_telegram_media(api, mediatypeid=mediatypeid, dry_run=args.dry_run, report=report)
    if mediatypeid and userid:
        ensure_telegram_action(api, mediatypeid=mediatypeid, userid=userid, dry_run=args.dry_run, report=report)
    else:
        report["telegram_action"] = "skipped_missing_telegram_config"

    ensure_netbox_host_macros(api, dry_run=args.dry_run, report=report)
    report["changed"] = any(
        (isinstance(value, str) and value in {"created", "updated"})
        or (isinstance(value, list) and len(value) > 0)
        for key, value in report.items()
        if key not in {"changed", "dry_run"}
    )

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
