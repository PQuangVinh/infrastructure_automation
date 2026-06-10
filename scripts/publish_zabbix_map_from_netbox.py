#!/usr/bin/env python3
"""Create or update a Zabbix map directly from NetBox cable data."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

from config_common import load_project_env, netbox_settings
from render_topology_from_netbox import api_base, build_edges, paginated_get

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_local_env() -> None:
    load_project_env(override=True, preserve=("NETBOX_SITE",))


def clean_token(value: str) -> tuple[str, str]:
    value = (value or "").strip().strip('"').strip("'")
    if " " in value:
        prefix, token = value.split(None, 1)
        if prefix.lower() in {"bearer", "token"}:
            return prefix, token.strip()
    return os.getenv("NETBOX_TOKEN_TYPE", "Token"), value


def bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "yes", "true", "on"}


def zabbix_api_url() -> str:
    scheme = "https" if bool_env("ZABBIX_API_USE_SSL") else "http"
    host = os.getenv("ZABBIX_API_HOST", "192.168.80.20")
    port = os.getenv("ZABBIX_API_PORT", "8080")
    path = os.getenv("ZABBIX_URL_PATH", "").strip("/")
    base = f"{scheme}://{host}:{port}"
    return f"{base}/{path + '/' if path else ''}api_jsonrpc.php"


def zabbix_call(session: requests.Session, url: str, token: str, method: str, params: dict[str, Any], request_id: int) -> Any:
    response = session.post(
        url,
        json={
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "auth": token,
            "id": request_id,
        },
        timeout=30,
        verify=bool_env("ZABBIX_VALIDATE_CERTS"),
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(f"{method} failed: {payload['error']}")
    return payload["result"]


def pick_image(images: list[dict[str, str]], preferred: list[str], fallback: str = "1") -> str:
    by_name = {item["name"]: item["imageid"] for item in images}
    for name in preferred:
        if name in by_name:
            return by_name[name]
    return fallback


def aggregate_links(edges: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for edge in edges:
        key = tuple(sorted([edge["a_device"], edge["b_device"]]))
        grouped[key].append(edge)

    links = []
    for (left, right), items in sorted(grouped.items()):
        labels = []
        for item in sorted(items, key=lambda entry: (entry["a_port"], entry["b_port"])):
            if item["a_device"] == left:
                labels.append(f"{item['a_port']} <-> {item['b_port']}")
            else:
                labels.append(f"{item['b_port']} <-> {item['a_port']}")
        links.append(
            {
                "a_device": left,
                "b_device": right,
                "label": "\\n".join(labels),
                "status": ", ".join(sorted({item["status"] for item in items})),
                "count": len(items),
            }
        )
    return links


def layout_nodes(nodes: list[str], links: list[dict[str, Any]]) -> tuple[dict[str, tuple[int, int]], int, int]:
    degree = {node: 0 for node in nodes}
    for link in links:
        degree[link["a_device"]] += link["count"]
        degree[link["b_device"]] += link["count"]

    max_degree = max(degree.values()) if degree else 0
    core_nodes = [
        node for node in nodes
        if "CORE" in node.upper() or degree.get(node, 0) == max_degree
    ]
    core_nodes = sorted(set(core_nodes)) or nodes[:1]
    access_nodes = [node for node in nodes if node not in core_nodes]

    columns = min(5, max(1, len(access_nodes)))
    width = max(1400, columns * 300 + 200)
    rows = math.ceil(len(access_nodes) / columns) if access_nodes else 1
    height = max(850, 260 + rows * 220)

    positions: dict[str, tuple[int, int]] = {}

    core_spacing = width // (len(core_nodes) + 1)
    for index, node in enumerate(core_nodes, start=1):
        positions[node] = (core_spacing * index, 100)

    for index, node in enumerate(sorted(access_nodes)):
        row = index // columns
        col = index % columns
        spacing = width // (columns + 1)
        positions[node] = (spacing * (col + 1), 330 + row * 220)

    return positions, width, height


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def fetch_edges(netbox_url: str, netbox_token: str, netbox_token_type: str, site: str, validate_certs: bool) -> list[dict[str, str]]:
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"{netbox_token_type} {netbox_token}",
            "Accept": "application/json",
        }
    )
    cables_url = urljoin(f"{api_base(netbox_url)}/", "dcim/cables/?limit=0")
    return build_edges(paginated_get(session, cables_url, validate_certs), site or None)


def build_map_payload(
    map_name: str,
    edges: list[dict[str, str]],
    host_ids: dict[str, str],
    images: dict[str, str],
) -> dict[str, Any]:
    aggregated = aggregate_links(edges)
    nodes = sorted({edge["a_device"] for edge in edges} | {edge["b_device"] for edge in edges})
    positions, width, height = layout_nodes(nodes, aggregated)

    selement_ids = {node: str(index) for index, node in enumerate(nodes, start=1)}
    selements = []
    for node in nodes:
        is_core = "CORE" in node.upper()
        icon_id = images["core"] if is_core else images["switch"]
        x, y = positions[node]
        element = {
            "selementid": selement_ids[node],
            "elementtype": 0 if node in host_ids else 4,
            "iconid_off": icon_id,
            "label": "{HOST.NAME}\\n{HOST.CONN}" if node in host_ids else f"{node}\\nnot in Zabbix",
            "x": x,
            "y": y,
        }
        if node in host_ids:
            element["elements"] = [{"hostid": host_ids[node]}]
        selements.append(element)

    links = []
    for link in aggregated:
        color = "00AA00" if "Connected" in link["status"] else "CC6600"
        links.append(
            {
                "selementid1": selement_ids[link["a_device"]],
                "selementid2": selement_ids[link["b_device"]],
                "drawtype": 0,
                "color": color,
                "label": link["label"],
            }
        )

    return {
        "name": map_name,
        "width": width,
        "height": height,
        "label_type": 0,
        "label_location": 0,
        "highlight": 1,
        "expandproblem": 1,
        "markelements": 1,
        "show_unack": 0,
        "grid_size": 50,
        "grid_show": 1,
        "grid_align": 1,
        "label_format": 0,
        "expand_macros": 1,
        "selements": selements,
        "links": links,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--netbox-url", default=os.getenv("NETBOX_URL", "http://192.168.80.20:8000"))
    parser.add_argument("--netbox-token", default=os.getenv("NETBOX_TOKEN") or os.getenv("NETBOX_API_TOKEN"))
    parser.add_argument("--site", default=os.getenv("NETBOX_SITE", ""))
    parser.add_argument("--map-name", default=os.getenv("ZABBIX_MAP_NAME", "NetBox Topology"))
    parser.add_argument("--output-json", default=str(PROJECT_ROOT / "outputs" / "zabbix_map_payload.json"))
    return parser.parse_args()


def main() -> int:
    load_local_env()
    settings = netbox_settings(load_env=False)
    args = parse_args()
    if args.netbox_url == "http://localhost:8000":
        args.netbox_url = settings.url
    if not args.netbox_token:
        args.netbox_token = settings.token

    netbox_type, netbox_token = clean_token(args.netbox_token or "")
    zabbix_token = os.getenv("ANSIBLE_ZABBIX_AUTH_KEY", "").strip()
    if not netbox_token:
        print("Missing NetBox token. Set NETBOX_TOKEN in scripts/.env.", file=sys.stderr)
        return 2
    if not zabbix_token:
        print("Missing Zabbix API token. Set ANSIBLE_ZABBIX_AUTH_KEY in scripts/.env.", file=sys.stderr)
        return 2

    map_name = normalize_name(args.map_name)
    if args.site:
        map_name = f"{map_name} - {args.site}"

    edges = fetch_edges(args.netbox_url, netbox_token, netbox_type, args.site, bool_env("NETBOX_VALIDATE_CERTS"))
    if not edges:
        print("No NetBox cable links found for the selected scope.", file=sys.stderr)
        return 1

    zbx = requests.Session()
    zbx_url = zabbix_api_url()
    nodes = sorted({edge["a_device"] for edge in edges} | {edge["b_device"] for edge in edges})
    hosts = zabbix_call(
        zbx,
        zbx_url,
        zabbix_token,
        "host.get",
        {"output": ["hostid", "host", "name"], "filter": {"host": nodes}},
        1,
    )
    host_ids = {host["host"]: host["hostid"] for host in hosts}

    images = zabbix_call(
        zbx,
        zbx_url,
        zabbix_token,
        "image.get",
        {"output": ["imageid", "name"], "filter": {"name": ["Switch_(64)", "Switch_(96)", "Router_(96)"]}},
        2,
    )
    image_ids = {
        "switch": pick_image(images, ["Switch_(64)", "Switch_(96)"]),
        "core": pick_image(images, ["Router_(96)", "Switch_(96)", "Switch_(64)"]),
    }

    payload = build_map_payload(map_name, edges, host_ids, image_ids)
    existing = zabbix_call(zbx, zbx_url, zabbix_token, "map.get", {"output": ["sysmapid", "name"], "filter": {"name": [map_name]}}, 3)
    if existing:
        payload["sysmapid"] = existing[0]["sysmapid"]
        result = zabbix_call(zbx, zbx_url, zabbix_token, "map.update", payload, 4)
        action = "updated"
        sysmapid = result.get("sysmapids", [payload["sysmapid"]])[0]
    else:
        result = zabbix_call(zbx, zbx_url, zabbix_token, "map.create", payload, 5)
        action = "created"
        sysmapid = result["sysmapids"][0]

    output_path = Path(args.output_json)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    missing = sorted(set(nodes) - set(host_ids))
    print(f"Zabbix map {action}: {map_name} (sysmapid={sysmapid})")
    print(f"Nodes: {len(nodes)}, host elements: {len(host_ids)}, links: {len(payload['links'])}, cable links: {len(edges)}")
    if missing:
        print("Nodes not found as Zabbix hosts: " + ", ".join(missing))
    print(f"Payload: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
