#!/usr/bin/env python3
"""Import the infrastructure master workbook into NetBox.

The importer is intentionally idempotent: each row is applied with an
"ensure present" workflow, so re-running the script updates existing NetBox
objects instead of blindly creating duplicates.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from config_common import load_project_env, netbox_settings


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT / "data" / "Master_Infra_Config.xlsx"
FALLBACK_SOURCE = REPO_ROOT / "data" / "Infra_config.xlsx"
DEFAULT_CSV_DIR = REPO_ROOT / "data"
DEFAULT_INVENTORY = REPO_ROOT / "inventories" / "lab" / "netbox_inventory.yml"
EMPTY_VALUES = {"", "nan", "none", "null", "nat"}


def import_tabular_dependencies() -> Any:
    try:
        import pandas as pd
    except ModuleNotFoundError as exc:
        print(
            "Missing Python package: pandas\n"
            "Install dependencies with: python3 -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    return pd


def import_netbox_dependency() -> tuple[Any, Any]:
    try:
        import pynetbox
        from pynetbox.core.query import RequestError
    except ModuleNotFoundError as exc:
        missing = exc.name or "pynetbox"
        print(
            f"Missing Python package: {missing}\n"
            "Install dependencies with: python3 -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    return pynetbox, RequestError


def load_project_netbox_defaults(path: Path = DEFAULT_INVENTORY) -> dict[str, str]:
    defaults: dict[str, str] = {}
    if not path.exists():
        return defaults

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key == "api_endpoint":
            defaults["url"] = value
        elif key == "token":
            defaults["token"] = value

    return defaults


def slugify(value: Any) -> str:
    text = clean_text(value)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text or "unnamed"


def clean_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    if text.lower() in EMPTY_VALUES:
        return default
    return text


def clean_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip()
    if text.lower() in EMPTY_VALUES:
        return None
    return value


def clean_column_name(value: Any) -> str:
    return clean_text(value).replace("\ufeff", "").strip()


def normalize_status(value: Any, default: str = "active") -> str:
    status = clean_text(value, default).lower()
    aliases = {
        "enabled": "active",
        "disabled": "offline",
        "planned": "planned",
        "connected": "connected",
        "active": "active",
        "offline": "offline",
        "staged": "staged",
        "inventory": "inventory",
        "failed": "failed",
        "decommissioning": "decommissioning",
    }
    return aliases.get(status, status)


def to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = clean_text(value).lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on", "enabled", "enable"}


def to_int(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = clean_text(value)
    if not text:
        return default
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else default


def split_list(value: Any) -> list[str]:
    text = clean_text(value)
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,;\n]+", text) if part.strip()]


def first(row: dict[str, Any], *names: str, default: Any = None) -> Any:
    normalized = {clean_column_name(key).lower(): value for key, value in row.items()}
    for name in names:
        key = clean_column_name(name).lower()
        if key in normalized:
            value = clean_scalar(normalized[key])
            if value is not None:
                return value
    return default


def require_value(row: dict[str, Any], *names: str) -> Any:
    value = first(row, *names)
    if value is None:
        joined = ", ".join(names)
        raise ValueError(f"Missing required column/value: {joined}. Row: {row}")
    text = clean_text(value)
    if text.startswith("="):
        raise ValueError(
            f"Formula was not evaluated for {joined}: {text}. "
            "Open and save the workbook in Excel/LibreOffice, or import from the CSV export."
        )
    return value


def optional_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in (None, "")}


def custom_fields_from_row(row: dict[str, Any]) -> dict[str, Any]:
    custom_fields: dict[str, Any] = {}
    for raw_key, raw_value in row.items():
        key = clean_column_name(raw_key)
        value = clean_scalar(raw_value)
        if value is None or not key.lower().startswith("cf_"):
            continue
        custom_fields[key[3:]] = value
    return custom_fields


def infer_device_number(device_name: Any) -> str:
    name = clean_text(device_name)
    match = re.search(r"(?:^|[-_])(\d+)$", name)
    return match.group(1) if match else ""


@dataclass
class TableSource:
    source: Path
    csv_dir: Path
    pd: Any
    sheets: dict[str, Any] = field(default_factory=dict)

    def load(self) -> None:
        if self.source.exists():
            self.sheets = self.pd.read_excel(
                self.source,
                sheet_name=None,
                dtype=object,
                engine="openpyxl",
            )
            self.sheets = {name: self.clean_frame(frame) for name, frame in self.sheets.items()}

    def clean_frame(self, frame: Any) -> Any:
        frame = frame.copy()
        frame.columns = [clean_column_name(column) for column in frame.columns]
        frame = frame.loc[:, [column for column in frame.columns if column and not column.startswith("Unnamed")]]
        frame = frame.dropna(how="all")
        return frame

    def table(
        self,
        logical_name: str,
        sheet_candidates: Iterable[str],
        csv_candidates: Iterable[str] = (),
        required: bool = False,
    ) -> list[dict[str, Any]]:
        for sheet_name in sheet_candidates:
            if sheet_name in self.sheets:
                return self.rows_from_frame(self.sheets[sheet_name])

        for csv_name in csv_candidates:
            csv_path = self.csv_dir / csv_name
            if csv_path.exists():
                frame = self.pd.read_csv(csv_path, dtype=object, encoding="utf-8-sig")
                return self.rows_from_frame(self.clean_frame(frame))

        if required:
            candidates = ", ".join([*sheet_candidates, *csv_candidates])
            raise FileNotFoundError(f"Missing data table for {logical_name}. Tried: {candidates}")
        return []

    def rows_from_frame(self, frame: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for raw_row in frame.to_dict(orient="records"):
            row = {clean_column_name(key): clean_scalar(value) for key, value in raw_row.items()}
            if any(value is not None for value in row.values()):
                rows.append(row)
        return rows


@dataclass
class DryRunRecord:
    id: int


class NetBoxImporter:
    def __init__(self, nb: Any, request_error: type[Exception], dry_run: bool = False) -> None:
        self.nb = nb
        self.request_error = request_error
        self.dry_run = dry_run
        self.counters = {"created": 0, "updated": 0, "skipped": 0}
        self.next_dry_id = 1_000_000
        self.dry_exact_cache: dict[tuple[str, tuple[tuple[str, str], ...]], DryRunRecord] = {}
        self.dry_field_cache: dict[tuple[str, str, str], DryRunRecord] = {}

    def endpoint_key(self, endpoint: Any) -> str:
        return str(getattr(endpoint, "url", None) or repr(endpoint))

    def lookup_key(self, endpoint: Any, lookup: dict[str, Any]) -> tuple[str, tuple[tuple[str, str], ...]]:
        items = tuple(sorted((key, clean_text(value)) for key, value in optional_payload(lookup).items()))
        return self.endpoint_key(endpoint), items

    def new_dry_record(self) -> DryRunRecord:
        record = DryRunRecord(id=self.next_dry_id)
        self.next_dry_id += 1
        return record

    def cache_dry_record(self, endpoint: Any, lookup: dict[str, Any], payload: dict[str, Any], record: DryRunRecord) -> None:
        self.dry_exact_cache[self.lookup_key(endpoint, lookup)] = record
        endpoint_key = self.endpoint_key(endpoint)
        for key, value in {**payload, **lookup}.items():
            if isinstance(value, (str, int, float)):
                self.dry_field_cache[(endpoint_key, key, clean_text(value))] = record

    def endpoint_get(self, endpoint: Any, **lookup: Any) -> Any:
        lookup = optional_payload(lookup)
        if self.dry_run:
            cached = self.dry_exact_cache.get(self.lookup_key(endpoint, lookup))
            if cached:
                return cached
            endpoint_key = self.endpoint_key(endpoint)
            for key, value in lookup.items():
                cached = self.dry_field_cache.get((endpoint_key, key, clean_text(value)))
                if cached:
                    return cached
            if any(isinstance(value, int) and value >= 1_000_000 for value in lookup.values()):
                return None
        try:
            return endpoint.get(**lookup)
        except Exception:
            results = list(endpoint.filter(**lookup))
            return results[0] if results else None

    def ensure(self, endpoint: Any, lookup: dict[str, Any], payload: dict[str, Any], label: str) -> Any:
        lookup = optional_payload(lookup)
        payload = optional_payload(payload)
        existing = self.endpoint_get(endpoint, **lookup)

        if existing:
            if self.dry_run:
                print(f"[DRY-RUN] update {label}")
                self.counters["updated"] += 1
                return existing
            existing.update(payload)
            print(f"Updated {label}")
            self.counters["updated"] += 1
            return existing

        if self.dry_run:
            print(f"[DRY-RUN] create {label}")
            self.counters["created"] += 1
            record = self.new_dry_record()
            self.cache_dry_record(endpoint, lookup, payload, record)
            return record

        created = endpoint.create(payload)
        print(f"Created {label}")
        self.counters["created"] += 1
        return created

    def object_id(self, endpoint: Any, label: Any, lookup_field: str = "name", required: bool = True) -> int | None:
        text = clean_text(label)
        if not text:
            if required:
                raise ValueError(f"Missing reference for {endpoint}")
            return None
        filters = {lookup_field: text}
        obj = self.endpoint_get(endpoint, **filters)
        if obj is None and lookup_field != "slug":
            obj = self.endpoint_get(endpoint, slug=slugify(text))
        if obj is None and required:
            raise ValueError(f"Cannot find NetBox object {endpoint} where {lookup_field}={text}")
        return getattr(obj, "id", None) if obj else None

    def vlan_id(self, value: Any, required: bool = False) -> int | None:
        vid = to_int(value)
        if vid is None:
            if required:
                raise ValueError(f"Invalid VLAN reference: {value}")
            return None
        vlan = self.endpoint_get(self.nb.ipam.vlans, vid=vid)
        if vlan is None and required:
            raise ValueError(f"Cannot find VLAN VID {vid}")
        return getattr(vlan, "id", None) if vlan else None

    def interface(self, device_name: Any, interface_name: Any, required: bool = True) -> Any:
        device_id = self.object_id(self.nb.dcim.devices, device_name, required=required)
        if device_id is None:
            return None
        name = clean_text(interface_name)
        interface = self.endpoint_get(self.nb.dcim.interfaces, device_id=device_id, name=name)
        if interface is None and required:
            raise ValueError(f"Cannot find interface {device_name} {name}")
        return interface

    def import_regions(self, rows: list[dict[str, Any]], site_rows: list[dict[str, Any]]) -> None:
        if not rows:
            seen: set[str] = set()
            for site in site_rows:
                region_name = clean_text(first(site, "region"))
                if region_name and region_name not in seen:
                    rows.append({"name": region_name, "slug": slugify(region_name)})
                    seen.add(region_name)

        for row in rows:
            name = require_value(row, "name", "region", "region_name")
            slug = clean_text(first(row, "slug"), slugify(name))
            payload = {
                "name": clean_text(name),
                "slug": slug,
                "description": clean_text(first(row, "description")),
            }
            self.ensure(self.nb.dcim.regions, {"slug": slug}, payload, f"region {name}")

    def import_sites(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            name = require_value(row, "name", "site", "site_name")
            slug = clean_text(first(row, "slug"), slugify(name))
            region = first(row, "region")
            payload = {
                "name": clean_text(name),
                "slug": slug,
                "status": normalize_status(first(row, "status")),
                "description": clean_text(first(row, "description")),
                "time_zone": clean_text(first(row, "time_zone")),
                "region": self.object_id(self.nb.dcim.regions, region, required=False),
            }
            self.ensure(self.nb.dcim.sites, {"slug": slug}, payload, f"site {name}")

    def import_locations(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            name = require_value(row, "name", "location")
            site = require_value(row, "site")
            site_id = self.object_id(self.nb.dcim.sites, site)
            slug = clean_text(first(row, "slug"), slugify(name))
            payload = {
                "name": clean_text(name),
                "slug": slug,
                "site": site_id,
                "status": normalize_status(first(row, "status")),
                "description": clean_text(first(row, "description")),
            }
            self.ensure(self.nb.dcim.locations, {"slug": slug, "site_id": site_id}, payload, f"location {name}")

    def import_racks(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            name = clean_text(require_value(row, "name", "rack"))
            site = require_value(row, "site")
            site_id = self.object_id(self.nb.dcim.sites, site)
            payload = {
                "name": name,
                "site": site_id,
                "location": self.object_id(self.nb.dcim.locations, first(row, "location"), required=False),
                "status": normalize_status(first(row, "status")),
                "u_height": to_int(first(row, "u_height", "height"), 42),
                "width": to_int(first(row, "width"), 19),
            }
            self.ensure(self.nb.dcim.racks, {"name": name, "site_id": site_id}, payload, f"rack {name}")

    def import_vlans(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            vid = to_int(require_value(row, "vid", "vlan", "VLAN Number"))
            name = require_value(row, "name", "VLAN Name")
            site = first(row, "site")
            payload = {
                "vid": vid,
                "name": clean_text(name),
                "status": normalize_status(first(row, "status", "Trang thai")),
                "description": clean_text(first(row, "description", "Mo ta")),
                "site": self.object_id(self.nb.dcim.sites, site, required=False),
            }
            self.ensure(self.nb.ipam.vlans, {"vid": vid}, payload, f"vlan {vid} {name}")

    def import_prefixes(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            prefix = require_value(row, "prefix", "Prefix")
            site = first(row, "site", "Site")
            payload = {
                "prefix": clean_text(prefix),
                "status": normalize_status(first(row, "status", "Status")),
                "description": clean_text(first(row, "description", "Description")),
                "vlan": self.vlan_id(first(row, "vlan", "VLAN"), required=False),
                "site": self.object_id(self.nb.dcim.sites, site, required=False),
            }
            self.ensure(self.nb.ipam.prefixes, {"prefix": prefix}, payload, f"prefix {prefix}")

    def import_manufacturers(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            name = require_value(row, "name", "manufacturer", "Manufacturer")
            slug = clean_text(first(row, "slug"), slugify(name))
            payload = {
                "name": clean_text(name),
                "slug": slug,
                "description": clean_text(first(row, "description")),
            }
            self.ensure(self.nb.dcim.manufacturers, {"slug": slug}, payload, f"manufacturer {name}")

    def import_device_roles(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            name = require_value(row, "name", "role", "device_role", "DEVICE ROLE")
            slug = clean_text(first(row, "slug"), slugify(name))
            payload = {
                "name": clean_text(name),
                "slug": slug,
                "color": clean_text(first(row, "color"), "9e9e9e").lstrip("#"),
            }
            self.ensure(self.nb.dcim.device_roles, {"slug": slug}, payload, f"device role {name}")

    def import_device_types(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            model = require_value(row, "model", "Device Type", "device_type", "MODEL")
            manufacturer = require_value(row, "manufacturer", "Manufacturer")
            slug = clean_text(first(row, "slug"), slugify(model))
            payload = {
                "model": clean_text(model),
                "slug": slug,
                "manufacturer": self.object_id(self.nb.dcim.manufacturers, manufacturer),
                "u_height": to_int(first(row, "u_height", "U Height"), 1),
            }
            self.ensure(self.nb.dcim.device_types, {"slug": slug}, payload, f"device type {model}")

    def import_devices(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            name = require_value(row, "name", "HOST NAME")
            device_type = require_value(row, "device_type", "MODEL")
            role = require_value(row, "role", "device_role", "DEVICE ROLE")
            site = require_value(row, "site", "Site")
            payload = {
                "name": clean_text(name),
                "device_type": self.object_id(self.nb.dcim.device_types, device_type, "model"),
                "role": self.object_id(self.nb.dcim.device_roles, role),
                "site": self.object_id(self.nb.dcim.sites, site),
                "location": self.object_id(self.nb.dcim.locations, first(row, "location", "Location"), required=False),
                "rack": self.object_id(self.nb.dcim.racks, first(row, "rack", "Rack"), required=False),
                "status": normalize_status(first(row, "status", "STATUS")),
                "serial": clean_text(first(row, "serial", "Serial")),
            }
            custom_fields = custom_fields_from_row(row)
            device_number = clean_text(first(row, "cf_device_number", "device_number", "Device Number"))
            if not device_number:
                device_number = infer_device_number(name)
            if device_number:
                custom_fields["device_number"] = device_number
            if custom_fields:
                payload["custom_fields"] = custom_fields

            try:
                self.ensure(self.nb.dcim.devices, {"name": name}, payload, f"device {name}")
            except self.request_error as exc:
                if "role" not in str(exc).lower():
                    raise
                payload["device_role"] = payload.pop("role")
                self.ensure(self.nb.dcim.devices, {"name": name}, payload, f"device {name}")

    def import_interfaces(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            name = require_value(row, "name", "Cong (Interface)")
            device = require_value(row, "device", "Thiet bi")
            device_id = self.object_id(self.nb.dcim.devices, device)
            tagged_vlan_ids = [self.vlan_id(vlan, required=True) for vlan in split_list(first(row, "tagged_vlans", "Tagged VLANs"))]
            payload = {
                "device": device_id,
                "name": clean_text(name),
                "enabled": to_bool(first(row, "enabled", "Trang thai"), True),
                "type": clean_text(first(row, "type", "Loai cong"), "100base-tx").lower(),
                "description": clean_text(first(row, "description", "Mo ta")),
                "mode": clean_text(first(row, "mode", "Mode")).lower(),
                "untagged_vlan": self.vlan_id(first(row, "untagged_vlan", "Untagged VLAN"), required=False),
                "tagged_vlans": [vlan_id for vlan_id in tagged_vlan_ids if vlan_id is not None],
            }
            port_role = clean_text(first(row, "cf_port_role", "Port Role"))
            if port_role:
                payload["custom_fields"] = {"port_role": port_role}

            try:
                self.ensure(
                    self.nb.dcim.interfaces,
                    {"device_id": device_id, "name": name},
                    payload,
                    f"interface {device} {name}",
                )
            except self.request_error as exc:
                if "custom" not in str(exc).lower():
                    raise
                payload.pop("custom_fields", None)
                print(f"Skipped custom field for interface {device} {name}: NetBox rejected it")
                self.ensure(
                    self.nb.dcim.interfaces,
                    {"device_id": device_id, "name": name},
                    payload,
                    f"interface {device} {name}",
                )

    def import_ip_addresses(self, rows: list[dict[str, Any]]) -> None:
        primary_links: list[tuple[str, str]] = []
        for row in rows:
            address = require_value(row, "address", "IP Address")
            device = require_value(row, "device", "Device")
            interface_name = require_value(row, "interface", "Interface")
            interface = self.interface(device, interface_name)
            payload = {
                "address": clean_text(address),
                "status": normalize_status(first(row, "status", "Status")),
                "description": clean_text(first(row, "description", "Description")),
                "dns_name": clean_text(first(row, "dns_name", "DNS Name")),
                "assigned_object_type": "dcim.interface",
                "assigned_object_id": getattr(interface, "id", None),
            }
            ip = self.ensure(self.nb.ipam.ip_addresses, {"address": address}, payload, f"ip address {address}")
            if to_bool(first(row, "is_primary"), False):
                primary_links.append((clean_text(device), clean_text(address)))

        for device_name, address in primary_links:
            self.set_primary_ip(device_name, address)

    def set_primary_ip(self, device_name: str, address: str) -> None:
        device = self.endpoint_get(self.nb.dcim.devices, name=device_name)
        ip = self.endpoint_get(self.nb.ipam.ip_addresses, address=address)
        if not device or not ip:
            self.counters["skipped"] += 1
            print(f"Skipped primary IP for {device_name}: missing device or IP {address}")
            return
        if self.dry_run:
            print(f"[DRY-RUN] set primary IPv4 {address} on {device_name}")
            self.counters["updated"] += 1
            return
        device.update({"primary_ip4": ip.id})
        self.counters["updated"] += 1
        print(f"Set primary IPv4 {address} on {device_name}")

    def import_cables(self, rows: list[dict[str, Any]]) -> None:
        cabled_endpoints: set[tuple[str, str]] = set()
        for row in rows:
            side_a_device = require_value(row, "side_a_device", "Side A Device")
            side_a_name = require_value(row, "side_a_name", "Side A Name")
            side_b_device = require_value(row, "side_b_device", "Side B Device")
            side_b_name = require_value(row, "side_b_name", "Side B Name")
            side_a_key = (clean_text(side_a_device), clean_text(side_a_name))
            side_b_key = (clean_text(side_b_device), clean_text(side_b_name))

            if side_a_key in cabled_endpoints or side_b_key in cabled_endpoints:
                self.counters["skipped"] += 1
                print(f"Skipped cable {side_a_device} {side_a_name} <-> {side_b_device} {side_b_name}: duplicate endpoint in source data")
                continue

            side_a = self.interface(side_a_device, side_a_name)
            side_b = self.interface(side_b_device, side_b_name)
            label = clean_text(first(row, "label", "description", "Description"))

            if getattr(side_a, "cable", None) or getattr(side_b, "cable", None):
                self.counters["skipped"] += 1
                cabled_endpoints.update({side_a_key, side_b_key})
                print(f"Skipped cable {side_a_device} {side_a_name} <-> {side_b_device} {side_b_name}: endpoint already cabled")
                continue

            payload = {
                "a_terminations": [{"object_type": "dcim.interface", "object_id": side_a.id}],
                "b_terminations": [{"object_type": "dcim.interface", "object_id": side_b.id}],
                "status": normalize_status(first(row, "status"), "connected"),
                "label": label,
            }
            cable_label = f"cable {side_a_device} {side_a_name} <-> {side_b_device} {side_b_name}"
            if self.dry_run:
                print(f"[DRY-RUN] create {cable_label}")
                self.counters["created"] += 1
                cabled_endpoints.update({side_a_key, side_b_key})
                continue
            self.nb.dcim.cables.create(optional_payload(payload))
            self.counters["created"] += 1
            cabled_endpoints.update({side_a_key, side_b_key})
            print(f"Created {cable_label}")


def resolve_source(path_arg: str | None) -> Path:
    if path_arg:
        return Path(path_arg).expanduser().resolve()
    if DEFAULT_SOURCE.exists():
        return DEFAULT_SOURCE
    return FALLBACK_SOURCE


def parse_args() -> argparse.Namespace:
    load_project_env(override=True)
    project_defaults = load_project_netbox_defaults()
    settings = netbox_settings(load_env=False)
    default_url = settings.url or project_defaults.get("url") or "http://192.168.80.20:8000"
    default_token = settings.token or project_defaults.get("token")

    parser = argparse.ArgumentParser(description="Import Excel master infrastructure data into NetBox.")
    parser.add_argument("--source", help=f"Workbook path. Default: {DEFAULT_SOURCE} then {FALLBACK_SOURCE}")
    parser.add_argument("--csv-dir", default=str(DEFAULT_CSV_DIR), help=f"CSV fallback directory. Default: {DEFAULT_CSV_DIR}")
    parser.add_argument("--netbox-url", default=default_url, help=f"NetBox URL. Default: {default_url}")
    parser.add_argument("--token", default=default_token, help="NetBox API token. Can use NETBOX_TOKEN.")
    parser.add_argument("--dry-run", action="store_true", help="Connect to NetBox and print create/update actions.")
    parser.add_argument("--validate-only", action="store_true", help="Only read the workbook/CSV data and print row counts.")
    return parser.parse_args()


def load_source(args: argparse.Namespace) -> tuple[TableSource, dict[str, list[dict[str, Any]]]]:
    pd = import_tabular_dependencies()
    source = resolve_source(args.source)
    tables = TableSource(source=source, csv_dir=Path(args.csv_dir).expanduser().resolve(), pd=pd)
    tables.load()

    data = {
        "regions": tables.table("regions", ["tbl_regions", "Regions"], ["01_regions.csv"]),
        "sites": tables.table("sites", ["tbl_sites", "Sites"], ["02_sites.csv"], required=True),
        "locations": tables.table("locations", ["tbl_locations", "Locations"], ["netbox_locations (1).csv"]),
        "racks": tables.table("racks", ["tbl_racks", "Racks"], ["netbox_racks.csv"]),
        "vlans": tables.table("vlans", ["tbl_vlan", "tbl_vlans", "VLANs"], ["02_VLANs.csv"], required=True),
        "prefixes": tables.table("prefixes", ["tbl_prefixes", "Prefixes"], ["03_prefixes.csv"]),
        "manufacturers": tables.table("manufacturers", ["tbl_manufacturers", "Manufacturers"], ["04_manufacturers.csv"], required=True),
        "device_roles": tables.table("device_roles", ["tbl_device-roles", "tbl_device_roles", "Device_Roles"], ["06_device roles.csv"], required=True),
        "device_types": tables.table("device_types", ["tbl_device-types", "tbl_device_types", "Device_Types"], ["05_device_types.csv"], required=True),
        "devices": tables.table("devices", ["tbl_devices", "Devices"], ["07_devices.csv"], required=True),
        "interfaces": tables.table("interfaces", ["tbl_interfaces", "Interfaces"], ["08_interfaces.csv"]),
        "ip_addresses": tables.table("ip_addresses", ["tbl_IP-addresses", "tbl_ip_addresses", "IP_Addresses"], ["09_IP addresses.csv"]),
        "cables": tables.table("cables", ["tbl_netbox-cables", "tbl_netbox_cables", "Cables"], ["10_netbox_cables.csv"]),
    }
    return tables, data


def validate_data(source: TableSource, data: dict[str, list[dict[str, Any]]]) -> None:
    print(f"Source workbook: {source.source}")
    if source.sheets:
        print(f"Workbook sheets: {', '.join(source.sheets)}")
    else:
        print("Workbook not found/readable; using CSV fallback where available.")
    print("Data row counts:")
    for name, rows in data.items():
        print(f"- {name}: {len(rows)}")
    warn_about_data(data)


def warn_about_data(data: dict[str, list[dict[str, Any]]]) -> None:
    warnings: list[str] = []

    primary_by_device: dict[str, list[str]] = {}
    for row in data.get("ip_addresses", []):
        if not to_bool(first(row, "is_primary"), False):
            continue
        device = clean_text(first(row, "device", "Device"))
        address = clean_text(first(row, "address", "IP Address"))
        primary_by_device.setdefault(device, []).append(address)

    for device, addresses in primary_by_device.items():
        if len(addresses) > 1:
            warnings.append(
                f"{device} has multiple primary IP rows; final applied primary will be {addresses[-1]}."
            )

    seen_cable_endpoints: dict[tuple[str, str], str] = {}
    for row in data.get("cables", []):
        endpoints = [
            (clean_text(first(row, "side_a_device", "Side A Device")), clean_text(first(row, "side_a_name", "Side A Name"))),
            (clean_text(first(row, "side_b_device", "Side B Device")), clean_text(first(row, "side_b_name", "Side B Name"))),
        ]
        cable_name = f"{endpoints[0][0]} {endpoints[0][1]} <-> {endpoints[1][0]} {endpoints[1][1]}"
        for endpoint in endpoints:
            if endpoint in seen_cable_endpoints:
                warnings.append(
                    f"Duplicate cable endpoint {endpoint[0]} {endpoint[1]} in '{cable_name}'; "
                    f"already used by '{seen_cable_endpoints[endpoint]}'."
                )
            else:
                seen_cable_endpoints[endpoint] = cable_name

    if warnings:
        print("Data warnings:")
        for warning in warnings:
            print(f"- {warning}")


def main() -> int:
    args = parse_args()
    source, data = load_source(args)
    validate_data(source, data)

    if args.validate_only:
        return 0
    if not args.token:
        print("NetBox API token is missing. Set NETBOX_TOKEN, use scripts/.env, or pass --token.", file=sys.stderr)
        return 2

    pynetbox, request_error = import_netbox_dependency()
    nb = pynetbox.api(args.netbox_url.rstrip("/"), token=args.token)
    importer = NetBoxImporter(nb, request_error, dry_run=args.dry_run)

    importer.import_regions(data["regions"], data["sites"])
    importer.import_sites(data["sites"])
    importer.import_locations(data["locations"])
    importer.import_racks(data["racks"])
    importer.import_vlans(data["vlans"])
    importer.import_prefixes(data["prefixes"])
    importer.import_manufacturers(data["manufacturers"])
    importer.import_device_roles(data["device_roles"])
    importer.import_device_types(data["device_types"])
    importer.import_devices(data["devices"])
    importer.import_interfaces(data["interfaces"])
    importer.import_ip_addresses(data["ip_addresses"])
    importer.import_cables(data["cables"])

    print(
        "Done: "
        f"{importer.counters['created']} created, "
        f"{importer.counters['updated']} updated, "
        f"{importer.counters['skipped']} skipped."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
