# Zehnder VMC MQTT Daemon

A custom Python daemon designed to bridge a **Zehnder ComfoAir FIT 100** VMC (Ventilazione Meccanica Controllata) unit with Home Assistant via MQTT. 

This project completely bypasses and replaces the original **Zehnder ComfoLed** hardware controller. It communicates directly with the VMC over RS485, processes the proprietary serial packets, and provides a seamless Home Assistant integration using MQTT Auto-Discovery.

## Supported Hardware & Specs

* **VMC Unit:** Zehnder ComfoAir FIT 100
  * Flow rate: up to 100 m³/h
  * Heat Recovery: Enthalpy exchanger (up to 83% efficiency, no condensate drain needed)
  * Control logic: 4 fan speeds + Standby + Filter/Fault alerts
* **Replaced Controller:** Zehnder ComfoLed

## Infrastructure Requirements

The project has been developed and tested with the following hardware:
* **Raspberry Pi Zero 2 W** (equipped with a standard SD card and a CPU heatsink for thermal management).
* **RS485 to TTL Module** (strictly powered at 3.3V to ensure compatibility with the Raspberry Pi GPIO logic levels).
* *Alternative:* A standard **USB to RS485 adapter** can also be used, adjusting the serial port configuration accordingly.

## Raspberry Pi Serial Configuration

To ensure reliable communication at 9600 baud via the GPIO pins, a specific configuration on the Raspberry Pi is required. The primary hardware UART (which provides a stable clock) is normally assigned to the Bluetooth module. You must remap this UART to the GPIO pins (usually disabling Bluetooth or moving it to the mini-UART). 

Depending on your OS version, this typically involves adding the `dtoverlay=disable-bt` or `dtoverlay=miniuart-bt` directive in your `/boot/config.txt` file and ensuring the serial console is disabled via `raspi-config`.

## Configuration

Before running the script, create a `.env` file in the root directory. 

### `.env` Example:

```ini
# Serial Configuration
SERIAL_PORT=/dev/ttyAMA0  # Use /dev/ttyUSB0 if using a USB adapter
SERIAL_BAUD=9600

# MQTT Broker Configuration
MQTT_BROKER=192.168.1.100
MQTT_PORT=1883
MQTT_USER=your_mqtt_username
MQTT_PASS=your_mqtt_password

# Debugging and Testing Flags
ENABLE_RAW_LOG=false
READ_ONLY_MODE=false
```

### Debugging Variables Explanation:
* `ENABLE_RAW_LOG`: If set to `true`, the daemon will dump all raw hexadecimal packets (TX and RX) into a local `vmc_raw.log` file. Useful for reverse-engineering new commands or troubleshooting serial noise.
* `READ_ONLY_MODE`: If set to `true`, the daemon will only listen to the serial bus and update Home Assistant, but will absolutely refrain from injecting any TX packets into the RS485 bus. Useful for safe initial testing.

## Operational Notes

* **Synchronous Operation:** The daemon operates in a synchronous, blocking mode relative to the serial packet flow. 
* **Infinite Loop & Polling:** The core logic relies on an infinite loop that continuously reads the incoming state broadcasts from the VMC. Commands (like speed changes) are injected precisely between the machine's transmission cycles.
* **Debouncing:** Physical state changes require multiple identical packets to be confirmed (debounce) before triggering an MQTT update, ensuring high reliability and ignoring bus noise.

## Systemd Service Installation (Linux)

To ensure the daemon runs automatically on boot and restarts in case of failure, configure it as a systemd service.

1. Create a service file:

```bash
sudo nano /etc/systemd/system/vmc.service
```

2. Paste the following configuration (adjust `/path/to/your/project` and `pi` user accordingly):

```ini
[Unit]
Description=Zehnder VMC MQTT Controller Daemon
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/path/to/your/project
# If using a virtual environment, point to its python executable:
ExecStart=/path/to/your/project/venv/bin/python vmc_daemon.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

3. Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable vmc.service
sudo systemctl start vmc.service
```

4. **View Live Logs:**
To monitor the output of the daemon in real-time, use:

```bash
sudo journalctl -u vmc.service -f
```

## Roadmap & Known Limitations

This project is in active development. The following features and machine states are currently missing and planned for future releases:
* **Malfunction Alerts:** Detection and decoding of hardware error states.
* **Filter Change Alert:** Intercepting the specific packet indicating a dirty filter.
* **"Exhaust Only" State:** Decoding the physical state confirming the machine is running in extraction-only mode.
* **Unknown Edge Cases:** Other proprietary behaviors or packets not yet encountered during standard testing.