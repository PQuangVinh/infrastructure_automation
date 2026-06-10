#!/usr/bin/env python3
"""Render Mermaid, Graphviz DOT, and optional SVG topology from NetBox cables."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

from config_common import load_project_env, netbox_auth_header, netbox_settings

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is optional at runtime
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_local_env() -> None:
    load_project_env(override=True, preserve=("NETBOX_SITE",))


def clean_url(url: str) -> str:
    return url.rstrip("/")


def api_base(netbox_url: str) -> str:
    url = clean_url(netbox_url)
    return url if url.endswith("/api") else f"{url}/api"


def slug(value: str) -> str:
    value = value.strip() or "all"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "all"


def dot_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def mermaid_id(value: str) -> str:
    ident = re.sub(r"[^A-Za-z0-9_]+", "_", value)
    if not ident or ident[0].isdigit():
        ident = f"n_{ident}"
    return ident


def pick_name(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("name", "display", "label", "value"):
            if value.get(key):
                return str(value[key])
    if value is None:
        return ""
    return str(value)


def paginated_get(session: requests.Session, endpoint: str, verify: bool) -> list[dict[str, Any]]:
    url = endpoint
    results: list[dict[str, Any]] = []

    while url:
        response = session.get(url, timeout=30, verify=verify)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and "results" in payload:
            results.extend(payload["results"])
            url = payload.get("next")
        elif isinstance(payload, list):
            results.extend(payload)
            url = ""
        else:
            raise ValueError(f"Unexpected NetBox API response from {endpoint}")

    return results


def normalize_termination(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {"device": "", "port": "", "site": ""}

    obj = raw.get("object") if isinstance(raw.get("object"), dict) else raw
    device = obj.get("device") if isinstance(obj, dict) else {}
    parent = obj.get("parent") if isinstance(obj, dict) else {}

    device_name = pick_name(device) or pick_name(parent)
    port_name = pick_name(obj)

    site = ""
    if isinstance(device, dict):
        site = pick_name(device.get("site"))
    if not site and isinstance(parent, dict):
        site = pick_name(parent.get("site"))

    return {
        "device": device_name,
        "port": port_name,
        "site": site,
    }


def cable_side(cable: dict[str, Any], side: str) -> dict[str, str]:
    plural_key = "a_terminations" if side == "a" else "b_terminations"
    legacy_key = "termination_a" if side == "a" else "termination_b"
    legacy_object_key = "termination_a_object" if side == "a" else "termination_b_object"

    terminations = cable.get(plural_key) or []
    if terminations:
        return normalize_termination(terminations[0])

    return normalize_termination(cable.get(legacy_key) or cable.get(legacy_object_key))


def status_label(cable: dict[str, Any]) -> str:
    status = cable.get("status")
    return pick_name(status) or "unknown"


def build_edges(cables: list[dict[str, Any]], site: str | None) -> list[dict[str, str]]:
    edges: list[dict[str, str]] = []

    for cable in cables:
        side_a = cable_side(cable, "a")
        side_b = cable_side(cable, "b")
        if not side_a["device"] or not side_b["device"]:
            continue

        if site:
            sites = {side_a["site"], side_b["site"]}
            known_sites = {item for item in sites if item}
            if known_sites and site not in known_sites:
                continue

        cable_label = cable.get("label") or cable.get("display") or f"cable-{cable.get('id', 'unknown')}"
        edges.append(
            {
                "a_device": side_a["device"],
                "a_port": side_a["port"],
                "b_device": side_b["device"],
                "b_port": side_b["port"],
                "status": status_label(cable),
                "label": str(cable_label),
            }
        )

    return edges


def render_mermaid(edges: list[dict[str, str]], title: str) -> str:
    nodes = sorted({edge["a_device"] for edge in edges} | {edge["b_device"] for edge in edges})
    lines = [f"# {title}", "", "```mermaid", "flowchart LR"]

    for node in nodes:
        lines.append(f'  {mermaid_id(node)}["{html.escape(node)}"]')

    for edge in edges:
        label = f"{edge['a_port']} <--> {edge['b_port']} | {edge['status']}"
        lines.append(
            f"  {mermaid_id(edge['a_device'])} ---|\"{html.escape(label)}\"| {mermaid_id(edge['b_device'])}"
        )

    lines.extend(["```", ""])
    return "\n".join(lines)


def render_dot(edges: list[dict[str, str]], title: str) -> str:
    nodes = sorted({edge["a_device"] for edge in edges} | {edge["b_device"] for edge in edges})
    lines = [
        "graph G {",
        f'  graph [label="{dot_escape(title)}", labelloc=t, layout=dot, splines=true, overlap=false];',
        '  node [shape=box, style="rounded,filled", fillcolor="#eef5ff", color="#446688"];',
        '  edge [color="#557799", fontname="Arial", fontsize=10];',
    ]

    for node in nodes:
        lines.append(f'  "{dot_escape(node)}" [zbx_host="{dot_escape(node)}", zbx_label="{dot_escape(node)}"];')

    for edge in edges:
        label = f"{edge['a_port']} <-> {edge['b_port']}"
        tooltip = f"{edge['label']} | {edge['status']}"
        lines.append(
            f'  "{dot_escape(edge["a_device"])}" -- "{dot_escape(edge["b_device"])}" '
            f'[label="{dot_escape(label)}", tooltip="{dot_escape(tooltip)}"];'
        )

    lines.append("}")
    return "\n".join(lines) + "\n"


def maybe_render_svg(dot_path: Path, svg_path: Path) -> bool:
    if not shutil.which("dot"):
        return False
    subprocess.run(["dot", "-Tsvg", str(dot_path), "-o", str(svg_path)], check=True)
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.getenv("NETBOX_URL", "http://localhost:8000"))
    parser.add_argument("--token", default=os.getenv("NETBOX_TOKEN") or os.getenv("NETBOX_API_TOKEN"))
    parser.add_argument("--site", default=os.getenv("NETBOX_SITE", ""))
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "outputs"))
    parser.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification.")
    return parser.parse_args()


def token_header(value: str) -> str:
    return netbox_auth_header(value, os.getenv("NETBOX_TOKEN_TYPE", "Token"))


def main() -> int:
    load_local_env()
    settings = netbox_settings(load_env=False)
    args = parse_args()
    if args.url == "http://localhost:8000":
        args.url = settings.url
    if not args.token:
        args.token = settings.token

    if not args.token:
        print("NetBox API token is missing. Set NETBOX_TOKEN or NETBOX_API_TOKEN.", file=sys.stderr)
        return 2

    session = requests.Session()
    session.headers.update({"Authorization": token_header(args.token), "Accept": "application/json"})

    cables_url = urljoin(f"{api_base(args.url)}/", "dcim/cables/?limit=0")
    cables = paginated_get(session, cables_url, verify=not args.insecure)
    edges = build_edges(cables, args.site or None)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    scope = args.site or "all"
    title = f"NetBox topology - {scope}"
    base = output_dir / f"topology_{slug(scope)}"
    md_path = base.with_suffix(".md")
    dot_path = base.with_suffix(".dot")
    svg_path = base.with_suffix(".svg")
    json_path = base.with_suffix(".json")

    md_path.write_text(render_mermaid(edges, title), encoding="utf-8")
    dot_path.write_text(render_dot(edges, title), encoding="utf-8")
    json_path.write_text(json.dumps(edges, ensure_ascii=False, indent=2), encoding="utf-8")

    rendered_svg = maybe_render_svg(dot_path, svg_path)
    print(f"Rendered {len(edges)} cable links")
    print(f"Mermaid: {md_path}")
    print(f"DOT: {dot_path}")
    print(f"JSON: {json_path}")
    if rendered_svg:
        print(f"SVG: {svg_path}")
    else:
        print("SVG: skipped because Graphviz 'dot' is not installed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
