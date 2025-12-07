"""Constants for the AromaTech integration."""

DOMAIN = "aromatech"

# BLE identifiers
SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
CHARACTERISTIC_UUID = "0000fff6-0000-1000-8000-00805f9b34fb"
MANUFACTURER_ID = 22851  # 0x5943

# Device name patterns for discovery
DEVICE_NAME_PATTERNS = [
    "AROMINI BT PLUS",
    "A313",
    "Air Stream",
    "AromaPro",
    "A1",
    "AT-600",
    "AT600",
    "AroMini",
    "SE8150D",
    "SE8150B",
    "8150D",
    "SB-600 BT",
    "SB-1500",
    "SE005",
    "SB-400 BT",
    "AE103",
    "AroMini BT",
]

# Command bytes
CMD_LOGIN = 0x8F
CMD_TIME_V2 = 0x02
CMD_TIME_V3 = 0x21
CMD_SCHEDULE_WRITE_V2 = 0x03
CMD_SCHEDULE_WRITE_V3 = 0x2A
CMD_READ_NAME = 0x7F
CMD_VERSION_V2 = 0x86
CMD_VERSION_V3 = 0x44
CMD_LIMITS_V2 = 0x88
CMD_LIMITS_V3 = 0x46

# Response bytes
RESP_NAME_V2 = 0x81
RESP_NAME_V3 = 0x42
RESP_LIMITS_V2 = 0x84
RESP_LIMITS_V3 = 0x46

# Defaults
DEFAULT_PASSWORD = "8888"
PAIR_CODE = "OK01"
DEFAULT_AROMA_SLOT = 1
DEFAULT_INTENSITY = 1
