# AromaTech Scent Diffuser Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration for controlling AromaTech scent diffusers
via Bluetooth Low Energy (BLE). Control your diffuser's power and intensity
directly from Home Assistant without any cloud connectivity.

I own an AroMini BT Plus, so that's the model I tested this with, but this
integration should theoretically support all Bluetooth-enabled AromaTech
diffusers, as well as any other generic scent diffusers that use the same
protocol (it is a fairly common protocol, from what I've researched).

To build this integration, I reverse-engineered the AromaTech mobile app's BLE
communication by decompiling the APK and studying the code. I have tested this
heavily with my own device, but don't have a "protocol 2.0" device to test with,
so please open an issue and contribute a PR if you find any issues.

## Features

- **Local Control**: Direct Bluetooth communication with no cloud dependencies
  (the gold standard)
- **Power Control**: Turn your diffuser on and off
- **Intensity Adjustment**: Set fragrance intensity (1-5 levels, device
  dependent)
- **Auto-Discovery**: Automatically detects nearby AromaTech diffusers
- **Protocol Support**: Compatible with both V2.0 and V3.0 AromaTech protocols

## Supported Devices

This integration supports AromaTech diffusers with Bluetooth connectivity,
including:

- AroMini BT / AroMini BT Plus
- AromaPro
- Air Stream
- AT-600 / AT600
- SB-400 BT / SB-600 BT / SB-1500
- SE8150D / SE8150B / 8150D
- A313 / A1 / SE005 / AE103

## Requirements

- Home Assistant with Bluetooth support enabled
- AromaTech diffuser with Bluetooth capability
- Diffuser within Bluetooth range of your Home Assistant instance

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots in the top right corner and select "Custom repositories"
3. Add `ttrushin/ha-aromatech-scent-diffuser` with category "Integration"
4. Click "Install" on the AromaTech Scent Diffusers integration
5. Restart Home Assistant

### Manual Installation

1. Download the latest release from GitHub
2. Copy the `custom_components/aromatech` folder to your Home Assistant
   `config/custom_components/` directory
3. Restart Home Assistant

## Configuration

1. Go to **Settings** > **Devices & Services** > **Integrations**
2. Click **+ Add Integration**
3. Search for "AromaTech Scent Diffuser"
4. Select your diffuser from the list of discovered devices
5. Enter the device password (default: `8888`)
6. Click **Submit**

## Entities

Once configured, the integration creates the following entities:

### Switch: Power

Controls the diffuser power state.

- **Entity**: `switch.<device_name>_power`
- **Actions**: Turn on / Turn off

### Select: Intensity

Controls the fragrance intensity level.

- **Entity**: `select.<device_name>_intensity`
- **Options**: 1 through max intensity (typically 5)

## State Attributes

The power switch entity exposes additional attributes:

| Attribute                   | Description                                      |
| --------------------------- | ------------------------------------------------ |
| `device_name`               | Bluetooth device name                            |
| `product_name`              | Product model (e.g., "AROMINI BT PLUS")          |
| `protocol_version`          | Protocol version (V2.0 or V3.0)                  |
| `connected`                 | Current connection state                         |
| `current_intensity`         | Current intensity setting (1-5)                  |
| `max_intensity`             | Maximum supported intensity                      |
| `fan_on`                    | Fan power state                                  |
| `oil_support`               | Device supports oil level tracking               |
| `battery_support`           | Device has battery                               |
| `oil_name`                  | Name of the loaded oil/fragrance                 |
| `oil_total`                 | Total oil capacity                               |
| `oil_remaining`             | Remaining oil amount                             |
| `oil_percentage`            | Oil remaining percentage                         |
| `battery_level`             | Battery level (if supported)                     |
| `pcb_version`               | PCB firmware version                             |
| `equipment_version`         | Equipment firmware version                       |
| `rssi`                      | Bluetooth signal strength                        |
| `last_seen`                 | Last communication timestamp                     |

Note: Multi-aroma devices will have numbered oil attributes (e.g., `oil_1_name`,
`oil_2_name`, etc.).

## Example Automations

### Turn on diffuser at sunset

```yaml
automation:
    - alias: "Evening Scent"
      trigger:
          - platform: sun
            event: sunset
      action:
          - service: switch.turn_on
            target:
                entity_id: switch.living_room_diffuser_power
```

### Adjust intensity by time of day

```yaml
automation:
    - alias: "Morning Intensity"
      trigger:
          - platform: time
            at: "07:00:00"
      action:
          - service: select.select_option
            target:
                entity_id: select.living_room_diffuser_intensity
            data:
                option: "5"

    - alias: "Evening Intensity"
      trigger:
          - platform: time
            at: "20:00:00"
      action:
          - service: select.select_option
            target:
                entity_id: select.living_room_diffuser_intensity
            data:
                option: "2"
```

## Troubleshooting

### Device not discovered

- Ensure the diffuser is powered on
- Check that Bluetooth is enabled in Home Assistant
- Verify the diffuser is within Bluetooth range (I **highly** recommend using a
  Bluetooth Proxy!)
- Try restarting the diffuser

### Authentication failed

- The default password is `8888` on most devices (note: this integration can
  actually work with tons of generic scent diffusers that use the same protocol
  that AromaTech uses, so check your device documentation)
- Password must be exactly 4 digits
- Ensure no other device is connected to the diffuser

### Control not responding

- Check the `rssi` attribute to verify signal strength
- Ensure the diffuser is still in range
- Try power cycling the diffuser
- Check Home Assistant logs for errors

## Technical Details

- **Communication**: Bluetooth Low Energy (BLE)
- **Protocol**: Supports AromaTech V2.0 and V3.0 protocols
- **IoT Class**: Local Polling
- **Dependencies**: Home Assistant Bluetooth component

## License

This project is licensed under the MIT License. Have at it!

## Contributing

Contributions are always welcome! Please open an issue or submit a pull request
on GitHub. I can't guarantee timely responses, but I appreciate well-written
contributions and friendly dialog.
