## Export NetBox ra Excel Master

Script `scripts/export_netbox_to_excel.py` keo du lieu tu NetBox API va xuat ra file Excel nhieu sheet:

- `Inventory`
- `IP_Seg`
- `Prefixes`
- `Maport`
- `IP_Addresses`
- `Cables`

Cai thu vien:

```bash
python3 -m pip install -r requirements.txt
```

Cai Ansible collection cho Aruba AOS-CX:

```bash
ansible-galaxy collection install -r collections/requirements.yml
```

Chay export voi cau hinh trong `scripts/.env`:

```bash
python3 scripts/export_netbox_to_excel.py
```

Hoac truyen URL/token rieng:

```bash
python3 scripts/export_netbox_to_excel.py --netbox-url http://192.168.80.20:8000 --token nbt_your_token --token-type Bearer
```

File mac dinh se duoc tao tai:

```text
generated_reports/NetBox_Master_Export.xlsx
```

## Import Excel Master vao NetBox

Script `scripts/import_to_netbox.py` doc file Excel master va day du lieu len NetBox theo dung thu tu dependency:

1. Regions, Sites, Locations, Racks
2. VLANs, Prefixes
3. Manufacturers, Device Roles, Device Types
4. Devices, Interfaces
5. IP Addresses, Primary IP, Cables

File mac dinh:

```text
data/Master_Infra_Config.xlsx
```

Neu file tren chua ton tai, script se fallback sang workbook hien co:

```text
data/Infra_config.xlsx
```

Script cung fallback sang cac CSV cu trong `data/` neu workbook bi thieu sheet.

Cau hinh token an toan:

```bash
cp scripts/.env.example scripts/.env
```

Sau do sua `scripts/.env`:

```text
NETBOX_URL=http://192.168.80.20:8000
NETBOX_TOKEN_TYPE=Bearer
NETBOX_TOKEN=nbt_your_token
```

Kiem tra workbook/CSV truoc khi import:

```bash
python3 scripts/import_to_netbox.py --validate-only
```

Chay thu voi NetBox nhung chi in hanh dong:

```bash
python3 scripts/import_to_netbox.py --dry-run
```

Import that:

```bash
python3 scripts/import_to_netbox.py
```

## NetBox dynamic inventory

Inventory chinh cho huong moi la `inventories/netbox.yml`. Trong lab hien tai file inventory nay duoc cau hinh local va bi ignore de tranh commit token.

```bash
cp scripts/.env.example scripts/.env
```

Cap nhat cac gia tri trong `scripts/.env`, toi thieu:

```text
NETBOX_URL=http://192.168.80.20:8000
NETBOX_TOKEN_TYPE=Bearer
NETBOX_TOKEN=nbt_your_token
ANSIBLE_NET_USER=admin
ANSIBLE_NET_PASSWORD=your_password
SNMP_COMMUNITY=your_ro_community
ZABBIX_API_HOST=192.168.80.20
ANSIBLE_ZABBIX_AUTH_KEY=your_zabbix_api_token
```

Kiem tra inventory NetBox:

```bash
ansible-inventory -i inventories/netbox.yml --graph
```

Chay playbook cau hinh tu NetBox:

```bash
ansible-playbook -i inventories/netbox.yml playbooks/sync_netbox_to_switches.yml
```

Neu van dung inventory lab cu:

```bash
ansible-playbook -i inventories/lab/netbox_inventory.yml playbooks/sync_netbox_to_switches.yml
```

## Monitoring va Zabbix

Deploy baseline SNMP/Syslog/NTP/LLDP cho switch:

```bash
ansible-playbook -i inventories/netbox.yml playbooks/deploy_monitoring_baseline.yml
```

Dong bo thiet bi NetBox sang Zabbix:

```bash
ansible-playbook -i inventories/netbox.yml playbooks/sync_zabbix_from_netbox.yml
```

Role `zabbix_sync` map du lieu nhu sau:

- NetBox Site -> Zabbix group `Site/<site>`
- NetBox Device Role -> Zabbix group `Network/<role>`
- NetBox custom field `zabbix_template` -> template override
- NetBox custom field `zabbix_hostgroup` -> host group bo sung
- NetBox custom field `criticality` -> Zabbix tag
- Tag NetBox `ZABBIX` hoac custom field `monitoring_enabled=true` -> dua vao Zabbix
- NetBox tag/custom field `LAB`/`PROD` -> Zabbix tag `environment`
- NetBox role -> Zabbix tag `role_class`, `severity_profile`, `alert_route`

Ap dung monitoring policy tren Zabbix:

```bash
ansible-playbook -i localhost, playbooks/configure_zabbix_monitoring.yml
```

Policy nay cau hinh macro LLD de chi discovery/canh bao interface co description quan trong:
`UPLINK`, `DOWNLINK`, `PEER`, `WAN`, `SRV`, `AP`.
Neu muon gui Telegram, dien them trong `scripts/.env`:

```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
ZABBIX_ALERT_ENV=prod
ZABBIX_NOTIFY_LAB=false
```

Quy trinh deploy co maintenance:

```bash
ansible-playbook -i localhost, playbooks/zabbix_maintenance.yml -e zabbix_maintenance_state=start -e zabbix_maintenance_duration=30m
ansible-playbook -i inventories/netbox.yml playbooks/sync_netbox_to_switches.yml --tags deploy
ansible-playbook -i localhost, playbooks/zabbix_maintenance.yml -e zabbix_maintenance_state=stop
```

## Topology tu NetBox Cable

Render Mermaid, DOT, JSON va SVG tu Cable trong NetBox:

```bash
NETBOX_SITE=TEST_C1 ansible-playbook -i localhost, playbooks/render_topology.yml
```

Output nam trong `outputs/`:

- `topology_TEST_C1.md`
- `topology_TEST_C1.dot`
- `topology_TEST_C1.json`
- `topology_TEST_C1.svg` neu may da cai Graphviz `dot`

Day topology tu NetBox Cable len Zabbix Map:

```bash
NETBOX_SITE=TEST_C1 ansible-playbook -i localhost, playbooks/publish_zabbix_map.yml
```

Playbook tren goi truc tiep Zabbix API va khong can Graphviz. Neu chi can render SVG offline de xem thu, cai them Graphviz:

```bash
python3 -m pip install -r requirements.txt
sudo apt-get install graphviz
```

## Audit va remediation an toan

Thu thap LLDP/CDP neighbor de doi chieu voi NetBox Cable:

```bash
ansible-playbook -i inventories/netbox.yml playbooks/audit_lldp_vs_netbox.yml
```

Dieu tra su co interface down theo che do read-only:

```bash
ansible-playbook -i inventories/netbox.yml playbooks/remediation_interface_down.yml -e affected_interface=Ethernet0/1
```

Playbook remediation hien tai chi thu thap thong tin va luu bao cao, chua tu dong `shutdown/no shutdown`, `clear errdisable`, doi VLAN hay reload thiet bi.
