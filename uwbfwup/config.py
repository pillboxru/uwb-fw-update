"""Разбор конфигурации wb-mqtt-serial в список шин и устройств.

Шина (Bus) — одна физическая линия RS-485: локальный порт (path) или порт шлюза (address:port).
Каждое устройство либо попадает в работу, либо получает причину пропуска (skip_reason).
"""

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

PARITIES = ("N", "E", "O")
FACTORY_BAUD = 9600


@dataclass(frozen=True)
class SerialParams:
    baud: int = 9600
    parity: str = "N"
    stop: int = 2

    def __str__(self):
        return f"{self.baud}{self.parity}{self.stop}"

    @classmethod
    def factory(cls):
        """Настройки загрузчика после перехода через рег. 129 или включения питания."""
        return cls(FACTORY_BAUD, "N", 2)

    @classmethod
    def parse(cls, text):
        """'115200N2' -> SerialParams."""
        text = text.strip().upper()
        return cls(int(text[:-2]), text[-2], int(text[-1]))


@dataclass
class Bus:
    key: str  # "/dev/ttyRS485-1" или "192.168.1.24:23"
    kind: str  # "serial" | "tcp" | "unsupported"
    enabled: bool
    path: Optional[str] = None
    address: Optional[str] = None
    port: Optional[int] = None
    params: SerialParams = field(default_factory=SerialParams)
    port_type: str = "serial"

    @property
    def is_tcp(self):
        return self.kind == "tcp"


@dataclass
class DeviceEntry:
    bus_key: str
    raw_slave: str
    slave: Optional[int]
    device_type: str
    id: str
    name: str
    params: SerialParams
    skip_reason: Optional[str] = None

    @property
    def label(self):
        return self.name or self.id or f"slave {self.raw_slave}"


@dataclass
class ConfigModel:
    buses: List[Bus]
    devices: List[DeviceEntry]
    warnings: List[str]

    def bus(self, key) -> Bus:
        for b in self.buses:
            if b.key == key:
                return b
        raise KeyError(key)

    def active_devices(self):
        return [d for d in self.devices if d.skip_reason is None]


def normalize_baud(value, default=FACTORY_BAUD):
    """wb-mqtt-serial допускает сокращённую запись скорости: 96 -> 9600, 1152 -> 115200.

    Реальные скорости Modbus-устройств WB (1200..115200) кратны 100, поэтому число, не кратное
    100, считаем сокращённой записью.
    """
    if value in (None, ""):
        return default
    value = int(value)
    if value % 100:
        value *= 100
    return value


def _parity(value, default="N"):
    if value in (None, ""):
        return default
    value = str(value).upper()[:1]
    if value not in PARITIES:
        raise ValueError(f"неизвестная чётность {value!r}")
    return value


def _params(obj, base: SerialParams) -> SerialParams:
    return SerialParams(
        baud=normalize_baud(obj.get("baud_rate"), base.baud),
        parity=_parity(obj.get("parity"), base.parity),
        stop=int(obj.get("stop_bits", base.stop)),
    )


def _bus_from_port(port: dict, index: int) -> Bus:
    enabled = port.get("enabled", True) is not False
    port_type = port.get("port_type", "serial")
    base = SerialParams()
    if port_type == "tcp":
        address, tcp_port = port.get("address", ""), int(port.get("port", 0))
        return Bus(f"{address}:{tcp_port}", "tcp", enabled, address=address, port=tcp_port,
                   params=_params(port, base), port_type=port_type)
    if port_type == "modbus tcp":
        address, tcp_port = port.get("address", ""), int(port.get("port", 502))
        return Bus(f"modbus-tcp://{address}:{tcp_port}", "unsupported", enabled, address=address,
                   port=tcp_port, port_type=port_type)
    if port.get("path"):
        return Bus(port["path"], "serial", enabled, path=port["path"], params=_params(port, base),
                   port_type=port_type)
    return Bus(f"port#{index}", "unsupported", enabled, port_type=port_type)


def parse_config(data: dict) -> ConfigModel:
    buses: List[Bus] = []
    devices: List[DeviceEntry] = []
    warnings: List[str] = []

    for index, port in enumerate(data.get("ports", [])):
        bus = _bus_from_port(port, index)
        buses.append(bus)
        seen: Dict[int, str] = {}
        for dev in port.get("devices", []):
            raw_slave = str(dev.get("slave_id", "")).strip()
            entry = DeviceEntry(
                bus_key=bus.key,
                raw_slave=raw_slave,
                slave=None,
                device_type=str(dev.get("device_type", "")),
                id=str(dev.get("id", "") or ""),
                name=str(dev.get("name", "") or ""),
                params=_params(dev, bus.params),
            )
            entry.skip_reason = _skip_reason(bus, dev, entry)
            if entry.skip_reason is None:
                if entry.slave in seen:
                    warnings.append(
                        f"{bus.key}: slave_id {entry.slave} встречается несколько раз "
                        f"({seen[entry.slave]}, {entry.label}); обрабатывается один раз")
                    entry.skip_reason = "duplicate_slave_id"
                else:
                    seen[entry.slave] = entry.label
            devices.append(entry)
    return ConfigModel(buses, devices, warnings)


def _skip_reason(bus: Bus, dev: dict, entry: DeviceEntry) -> Optional[str]:
    if not bus.enabled:
        return "port_disabled"
    if bus.kind == "unsupported":
        return "modbus_tcp_unsupported" if bus.port_type == "modbus tcp" else "unsupported_port"
    if dev.get("enabled", True) is False:
        return "device_disabled"
    protocol = dev.get("protocol")
    if protocol not in (None, "", "modbus"):
        return "non_modbus_protocol"
    if ":" in entry.raw_slave:
        # Модуль WBIO за WB-MIO: адрес "<адрес MIO>:<номер>"; прошивается сам MIO.
        return "wbio_module"
    try:
        slave = int(entry.raw_slave, 0)
    except ValueError:
        return "bad_slave_id"
    if not 1 <= slave <= 247:
        return "bad_slave_id"
    entry.slave = slave
    return None


def load_config(path) -> ConfigModel:
    with open(path, encoding="utf-8") as fp:
        return parse_config(json.load(fp))


# --- фильтры --------------------------------------------------------------

def bus_matches(bus: Bus, pattern: str) -> bool:
    """Фильтр шины: путь, "адрес:порт" или просто адрес шлюза (все его порты)."""
    if pattern == bus.key:
        return True
    if bus.address and pattern == bus.address:
        return True
    return False


def select_devices(model: ConfigModel, ports=(), slaves=(), device_ids=()) -> List[DeviceEntry]:
    """Устройства (включая пропускаемые), попавшие под фильтры."""
    result = []
    for dev in model.devices:
        bus = model.bus(dev.bus_key)
        if ports and not any(bus_matches(bus, p) for p in ports):
            continue
        if slaves and dev.slave not in slaves:
            continue
        if device_ids and dev.id not in device_ids and dev.name not in device_ids:
            continue
        result.append(dev)
    return result
