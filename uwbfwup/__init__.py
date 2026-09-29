"""uwbfwup — безопасное массовое обновление прошивок и загрузчиков Modbus-устройств Wiren Board.

Работает на контроллере Wiren Board: локальные RS-485 порты и шлюзы WB-MGE / WB-MIO-E
(порты wb-mqtt-serial с port_type "tcp" — Modbus RTU поверх TCP).
"""

__version__ = "0.2.0"

DEFAULT_CONFIG = "/etc/wb-mqtt-serial.conf"
DEFAULT_HOME = "/mnt/data/uwb-fw-update"
RELEASE_FILE = "/usr/lib/wb-release"
FW_ROOT_URL = "https://fw-releases.wirenboard.com"
LOCK_FILE = "/run/lock/uwb-fw-update.lock"
SERIAL_SERVICE = "wb-mqtt-serial"
GITHUB_REPO = "pillboxru/uwb-fw-update"  # релизы утилиты: проверка свежей версии и self-update
