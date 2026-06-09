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

Chay export voi cau hinh NetBox dang co trong `inventories/lab/netbox_inventory.yml`:

```bash
python3 scripts/export_netbox_to_excel.py
```

Hoac truyen URL/token rieng:

```bash
NETBOX_URL=http://localhost:8000 NETBOX_TOKEN=your_token python3 scripts/export_netbox_to_excel.py
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
NETBOX_URL=http://localhost:8000
NETBOX_TOKEN=your_token
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
