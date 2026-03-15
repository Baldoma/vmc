import serial
import time
import binascii
import os
import sys
import paho.mqtt.client as mqtt
import json
from dotenv import load_dotenv
import psutil 
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime  

# --- ENVIRONMENT VARIABLES ---
load_dotenv()

SERIAL_PORT = os.getenv('SERIAL_PORT', '/dev/ttyUSB0')
SERIAL_BAUD = int(os.getenv('SERIAL_BAUD', 9600))
MQTT_BROKER = os.getenv('MQTT_BROKER')
MQTT_PORT = int(os.getenv('MQTT_PORT', 1883))
MQTT_USER = os.getenv('MQTT_USER')
MQTT_PASS = os.getenv('MQTT_PASS')
ENABLE_RAW_LOG = os.getenv('ENABLE_RAW_LOG', 'false').lower() == 'true'
READ_ONLY_MODE = os.getenv('READ_ONLY_MODE', 'false').lower() == 'true'

SERIAL_TIMEOUT = 0.5
TOPIC_CMD = "vmc/command"
TOPIC_STATE = "vmc/state"
TOPIC_MODE = "vmc/mode"

TOPIC_SYS_TEMP = "vmc/system/temperature"
TOPIC_SYS_CPU = "vmc/system/cpu_usage"
TOPIC_SYS_MEM = "vmc/system/memory_usage"
TOPIC_SYS_DISK = "vmc/system/disk_usage"
TOPIC_SYS_UPTIME = "vmc/system/uptime_hours"

TOPIC_FAN_CMD = "vmc/fan/onoff/set"
TOPIC_FAN_STATE = "vmc/fan/onoff/state"
TOPIC_FAN_PRESET_SET = "vmc/fan/preset/set"
TOPIC_FAN_PRESET_STATE = "vmc/fan/preset/state"
FAN_DELAY_CYCLES = 15

RAW_LOG_FILE = "vmc_raw.log"
LOG_MAX_BYTES = 3 * 1024 * 1024  
LOG_BACKUP_COUNT = 5             

raw_logger = logging.getLogger("RawPacketLogger")
raw_logger.setLevel(logging.INFO)
raw_logger.propagate = False 

if ENABLE_RAW_LOG:
    handler = RotatingFileHandler(RAW_LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
    handler.setFormatter(logging.Formatter('%(message)s'))
    raw_logger.addHandler(handler)

# --- TIMING AND CYCLES PARAMETERS ---
POLLING_INTERVAL = 600          
SYSTEM_STATS_INTERVAL = 60      

CMD_CYCLES_NORMAL = 2
WAKEUP_ACTIVE_CYCLES = 3   
WAKEUP_WAIT_CYCLES   = 8   
CMD_CYCLES_WAKEUP    = WAKEUP_ACTIVE_CYCLES + WAKEUP_WAIT_CYCLES 

# Transmission cycles for Long Press
CMD_CYCLES_LONG      = 35

# Debounce threshold
STATE_CONFIRM_THRESHOLD = 4     

RESPONSE_DELAY_MS = 3  

# --- PACKETS AND COMMAND MAPS ---
PKT_IDLE  = binascii.unhexlify("0101A281B02700")
PKT_PLUS  = binascii.unhexlify("0101A281913701")
PKT_MINUS = binascii.unhexlify("0101A281F20702")

HEX_STANDBY = "0104a38236e80000"
HEX_VEL1    = "0104a38207db0100"
HEX_ALERT   = "0104a382c4f54f00"

SPEED_MAP = {
    HEX_STANDBY:        "OFF",
    HEX_VEL1:           "1",
    "0104a38265bd0300": "2",
    "0104a382a1710700": "3",
    "0104a38208f80f00": "4",
    HEX_ALERT:          "ALERT"
}

tx_session = { "pkt": PKT_IDLE, "counter": 0, "tag": None }

# --- GLOBAL SYSTEM STATE ---
system_state = {
    "mqtt_client": None,
    "current_virtual_state": "UNKNOWN", 
    "stable_hex_state": None,
    "last_published_state": "",
    
    # Debounce variables
    "raw_hex_prev": None,
    "consistency_count": 0,
    
    "pending_cmd": None,
    "last_mqtt_publish_time": 0,
    
    # Controlled OFF evaluation window
    "eval_off_at": 0.0,

    "target_fan_speed": -1,  # -1 indicates no pending operation
    "fan_cycle_counter": 0,
}

ser = None

def log(msg):
    ts = time.strftime("[%H:%M:%S]")
    print(f"{ts} {msg}")
    sys.stdout.flush()

def log_raw_packet(direction, packet_bytes):
    if not ENABLE_RAW_LOG: return
    try:
        hex_str = binascii.hexlify(packet_bytes).decode('utf-8').upper()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        raw_logger.info(f"[{ts}] {direction}: {hex_str}")
    except Exception: pass

def publish_ha_discovery(client):
    """Publish Home Assistant Auto-Discovery configurations."""
    discovery_prefix = "homeassistant"
    
    # --- Master VMC Device Info ---
    device_info_vmc = {
        "identifiers": ["vmc_controller_master_01"],
        "name": "VMC Zehnder",
        "model": "ComfoAir FIT 100",
        "manufacturer": "Zehnder"
    }

    # --- Gateway (Raspberry Pi) Device Info ---
    device_info_gateway = {
        "identifiers": ["vmc_gateway_pi"],
        "name": "VMC Gateway",
        "manufacturer": "Raspberry Pi Foundation"
    }

    # VMC Entities (State Sensor and Commands)
    entities_vmc = [
        {"component": "sensor", "id": "vmc_stato_main", "name": "Status", "state_topic": TOPIC_STATE, "icon": "mdi:air-conditioner"},
        {"component": "button", "id": "vmc_cmd_plus", "name": "Speed +", "command_topic": TOPIC_CMD, "payload_press": "PLUS", "icon": "mdi:fan-plus"},
        {"component": "button", "id": "vmc_cmd_minus", "name": "Speed -", "command_topic": TOPIC_CMD, "payload_press": "MINUS", "icon": "mdi:fan-minus"},
        {"component": "button", "id": "vmc_cmd_check", "name": "Update Status", "command_topic": TOPIC_CMD, "payload_press": "CHECK", "icon": "mdi:refresh"},
        {"component": "button", "id": "vmc_cmd_aspirazione", "name": "Exhaust Only", "command_topic": TOPIC_CMD, "payload_press": "LONG_PRESS_A", "icon": "mdi:weather-windy"},
        {"component": "fan", "id": "vmc_fan_preset", "name": "VMC Fan", 
         "command_topic": TOPIC_FAN_CMD, 
         "state_topic": TOPIC_FAN_STATE, 
         "preset_mode_command_topic": TOPIC_FAN_PRESET_SET, 
         "preset_mode_state_topic": TOPIC_FAN_PRESET_STATE, 
         "preset_modes": ["1", "2", "3", "4"], 
         "icon": "mdi:fan"},
    ]

    # Gateway System Entities
    entities_gateway = [
        {"component": "sensor", "id": "vmc_sys_temp", "name": "CPU Temperature", "state_topic": TOPIC_SYS_TEMP, "unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement", "icon": "mdi:thermometer"},
        {"component": "sensor", "id": "vmc_sys_cpu", "name": "CPU Usage", "state_topic": TOPIC_SYS_CPU, "unit_of_measurement": "%", "state_class": "measurement", "icon": "mdi:cpu-64-bit"},
        {"component": "sensor", "id": "vmc_sys_mem", "name": "Memory Usage", "state_topic": TOPIC_SYS_MEM, "unit_of_measurement": "%", "state_class": "measurement", "icon": "mdi:memory"},
        {"component": "sensor", "id": "vmc_sys_disk", "name": "Disk Usage", "state_topic": TOPIC_SYS_DISK, "unit_of_measurement": "%", "state_class": "measurement", "icon": "mdi:harddisk"},
        {"component": "sensor", "id": "vmc_sys_uptime", "name": "Uptime", "state_topic": "vmc/system/uptime_hours", "unit_of_measurement": "h", "device_class": "duration", "state_class": "total_increasing", "icon": "mdi:clock-outline"}
    ]

    # Publish VMC MQTT payloads
    for item in entities_vmc:
        component = item.pop("component")
        object_id = item["id"]
        topic = f"{discovery_prefix}/{component}/{object_id}/config"
        
        payload = item.copy()
        payload["unique_id"] = object_id
        payload["object_id"] = object_id
        payload["device"] = device_info_vmc
        
        client.publish(topic, json.dumps(payload), retain=True)

    # Publish Gateway MQTT payloads
    for item in entities_gateway:
        component = item.pop("component")
        object_id = item["id"]
        topic = f"{discovery_prefix}/{component}/{object_id}/config"
        
        payload = item.copy()
        payload["unique_id"] = object_id
        payload["object_id"] = object_id
        payload["device"] = device_info_gateway
        
        client.publish(topic, json.dumps(payload), retain=True)
    
    log("MQTT Auto-Discovery sent to Home Assistant (Devices: VMC, Gateway).")
    
def set_tx_sequence(packet, cycles, tag):
    global tx_session
    log(f"Logic: Transmission {tag} started ({cycles} cycles).")
    tx_session["pkt"] = packet
    tx_session["counter"] = cycles
    tx_session["tag"] = tag
    
    # Cancel pending OFF evaluations when sending a new command
    system_state["eval_off_at"] = 0.0

def on_connect(client, userdata, flags, rc, *args):
    if rc == 0:
        log("MQTT Connected.")
        client.subscribe([(TOPIC_CMD, 0), (TOPIC_FAN_CMD, 0), (TOPIC_FAN_PRESET_SET, 0)])
        publish_ha_discovery(client)
    else:
        log(f"MQTT Connection error: {rc}")

def on_message(client, userdata, msg):
    payload = msg.payload.decode()
    log(f"CMD RX ({msg.topic}): {payload}")
    
    if READ_ONLY_MODE: return

    # --- FAN MANAGEMENT (BASIC ON/OFF) ---
    if msg.topic == TOPIC_FAN_CMD:
        payload = payload.upper()
        target = -1
        if payload == "OFF":
            target = 0
        elif payload == "ON":
            target = 1  # Basic ON defaults to minimum speed
            
        if target != -1:
            system_state["target_fan_speed"] = target
            system_state["fan_cycle_counter"] = FAN_DELAY_CYCLES 
            set_tx_sequence(PKT_MINUS, CMD_CYCLES_WAKEUP, "FAN_WAKEUP")
        return

    # --- FAN MANAGEMENT (PRESET MODES) ---
    if msg.topic == TOPIC_FAN_PRESET_SET:
        payload = payload.upper()
        # Map text presets to numeric machine targets
        mapping = {"1": 1, "2": 2, "3": 3, "4": 4}
        
        if payload in mapping:
            target = mapping[payload]
            system_state["target_fan_speed"] = target
            system_state["fan_cycle_counter"] = FAN_DELAY_CYCLES 
            set_tx_sequence(PKT_MINUS, CMD_CYCLES_WAKEUP, "FAN_WAKEUP")
            log(f"Fan command: Preset '{payload}' -> Target {target}")
        return

    if payload in ["PLUS", "MINUS", "LONG_PRESS_A"]:
        system_state["pending_cmd"] = payload
        
        # Physical state check, safe even on startup (None)
        if system_state["stable_hex_state"] in [HEX_STANDBY, None]:
            log("Physical state Standby (or Unknown) -> WAKEUP required before command.")
            set_tx_sequence(PKT_MINUS, CMD_CYCLES_WAKEUP, "WAKEUP")
        else:
            log("Physical state Active -> Immediate command execution.")
            if payload == "LONG_PRESS_A":
                set_tx_sequence(PKT_MINUS, CMD_CYCLES_LONG, "LONG")
            else:
                pkt = PKT_PLUS if payload == "PLUS" else PKT_MINUS
                set_tx_sequence(pkt, CMD_CYCLES_NORMAL, "CMD")
            system_state["pending_cmd"] = None

    elif payload == "CHECK": 
        system_state["last_published_state"] = "" 
        log("CHECK: Update requested. Sending WAKEUP.")
        set_tx_sequence(PKT_MINUS, CMD_CYCLES_WAKEUP, "WAKEUP")

def publish_system_stats(client):
    try:
        cpu_usage = psutil.cpu_percent()
        mem_usage = psutil.virtual_memory().percent
        disk_usage = psutil.disk_usage('/').percent
        boot_time = datetime.fromtimestamp(psutil.boot_time())
        uptime = (datetime.now() - boot_time).total_seconds() / 3600

        client.publish(TOPIC_SYS_CPU, f"{cpu_usage:.1f}", retain=True)
        client.publish(TOPIC_SYS_MEM, f"{mem_usage:.1f}", retain=True)
        client.publish(TOPIC_SYS_DISK, f"{disk_usage:.1f}", retain=True)
        client.publish(TOPIC_SYS_UPTIME, f"{uptime:.2f}", retain=True)
    except Exception: pass

def connect_vmc():
    global ser
    while True:
        try:
            log(f"Connecting to VMC on {SERIAL_PORT}...")
            ser = serial.Serial(port=SERIAL_PORT, baudrate=SERIAL_BAUD, parity=serial.PARITY_EVEN, timeout=SERIAL_TIMEOUT)
            log("VMC Connected")
            return
        except Exception as e:
            time.sleep(5)

def process_packet(packet: bytes):
    current_raw_hex = binascii.hexlify(packet).decode('utf-8').lower()
    decoded_current = SPEED_MAP.get(current_raw_hex, "OFF")

    # Debounce logic
    if current_raw_hex == system_state["raw_hex_prev"]:
        system_state["consistency_count"] += 1
    else:
        system_state["consistency_count"] = 1
        system_state["raw_hex_prev"] = current_raw_hex

    # Stable State Validation
    if system_state["consistency_count"] >= STATE_CONFIRM_THRESHOLD:
        if system_state["stable_hex_state"] != current_raw_hex:
            system_state["stable_hex_state"] = current_raw_hex
            
            # --- EXCLUSIVE CONSOLE OUTPUT ---
            log(f"Physical state confirmed (4 cycles): {decoded_current}")

            # --- MQTT LOGIC ---
            if decoded_current not in ["OFF", "ALERT"]:
                if system_state["current_virtual_state"] != decoded_current:
                    log(f"Valid state detected: {decoded_current} (Sleep ignored).")
                system_state["current_virtual_state"] = decoded_current
                system_state["eval_off_at"] = 0.0  
                
            elif decoded_current == "ALERT":
                system_state["current_virtual_state"] = "ALERT"
                system_state["eval_off_at"] = 0.0

def update_mqtt_state():
    """Publish state. Inhibited during initial UNKNOWN phase."""
    if system_state["current_virtual_state"] == "UNKNOWN":
        return

    mqtt_state = system_state["current_virtual_state"]
    mqtt_mode = "NORMAL" 

    full_sig = f"{mqtt_state}|{mqtt_mode}"
    now = time.time()
    
    if full_sig != system_state["last_published_state"] or (now - system_state["last_mqtt_publish_time"]) >= 60:
        log(f"TX MQTT: {mqtt_state} | {mqtt_mode}")
        system_state["mqtt_client"].publish(TOPIC_STATE, mqtt_state, retain=True)
        system_state["mqtt_client"].publish(TOPIC_MODE, mqtt_mode, retain=True)

        # --- FAN ENTITY SYNCHRONIZATION (PRESET) ---
        fan_onoff = "OFF" if mqtt_state == "OFF" else "ON"
        
        # Translate numeric VMC state to Preset name
        preset_map = {"1": "1", "2": "2", "3": "3", "4": "4"}
        fan_preset = preset_map.get(mqtt_state, "1")

        # Publish UI alignment
        system_state["mqtt_client"].publish(TOPIC_FAN_STATE, fan_onoff, retain=True)
        if fan_onoff == "ON":
            system_state["mqtt_client"].publish(TOPIC_FAN_PRESET_STATE, fan_preset, retain=True)

        system_state["last_published_state"] = full_sig
        system_state["last_mqtt_publish_time"] = now

def handle_serial_transmission():
    time.sleep(RESPONSE_DELAY_MS / 1000.0)
    pkt_to_send = PKT_IDLE

    if tx_session["counter"] > 0:
        if tx_session["tag"] == "WAKEUP":
            cycles_done = CMD_CYCLES_WAKEUP - tx_session["counter"]
            if cycles_done < WAKEUP_ACTIVE_CYCLES:
                pkt_to_send = tx_session["pkt"]
        else:
            pkt_to_send = tx_session["pkt"]

        tx_session["counter"] -= 1

        if tx_session["counter"] == 0:
            finished_tag = tx_session["tag"]
            if finished_tag is not None:
                log(f"Sequence {finished_tag} completed.")
                
                # CRITICAL FIX: Arm OFF window only if the machine 
                # is physically transmitting STANDBY at sequence end.
                if system_state["stable_hex_state"] in [HEX_STANDBY, None]:
                    system_state["eval_off_at"] = time.time() + 6.0
                    log("OFF evaluation window (6s) started.")
                else:
                    system_state["eval_off_at"] = 0.0
            
            tx_session["tag"] = None

            # Execute pending commands after Wakeup
            if finished_tag == "WAKEUP":
                if system_state["pending_cmd"]:
                    # Suspend OFF reading due to imminent command
                    system_state["eval_off_at"] = 0.0
                    log(f"Executing pending command: {system_state['pending_cmd']}")
                    cmd = system_state["pending_cmd"]
                    if cmd == "LONG_PRESS_A":
                        set_tx_sequence(PKT_MINUS, CMD_CYCLES_LONG, "LONG")
                    else:
                        pkt = PKT_PLUS if cmd == "PLUS" else PKT_MINUS
                        set_tx_sequence(pkt, CMD_CYCLES_NORMAL, "CMD")
                    system_state["pending_cmd"] = None

    if not READ_ONLY_MODE:
        ser.write(pkt_to_send)
        log_raw_packet("TX", pkt_to_send)
        sys.stdout.flush()

def main():
    global ser 

    system_state["mqtt_client"] = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if MQTT_USER and MQTT_PASS: 
        system_state["mqtt_client"].username_pw_set(MQTT_USER, MQTT_PASS)
    system_state["mqtt_client"].on_connect = on_connect
    system_state["mqtt_client"].on_message = on_message
    
    try:
        system_state["mqtt_client"].connect(MQTT_BROKER, MQTT_PORT, 60)
        system_state["mqtt_client"].loop_start()
    except Exception as e: 
        log(f"Critical MQTT Error: {e}")
        return

    connect_vmc()
    buffer = b""
    last_polling_time = time.time() - POLLING_INTERVAL - 10
    last_stats_time = time.time() - SYSTEM_STATS_INTERVAL - 10

    while True:
        try:
            now = time.time()

            # Polling Timer & Startup
            if not READ_ONLY_MODE and tx_session["counter"] == 0 and system_state["eval_off_at"] == 0.0:
                if system_state["current_virtual_state"] == "UNKNOWN":
                    log("Startup: Acquiring initial state -> Sending WAKEUP")
                    system_state["last_published_state"] = ""
                    set_tx_sequence(PKT_MINUS, CMD_CYCLES_WAKEUP, "WAKEUP")
                    last_polling_time = now
                
                elif (now - last_polling_time) > POLLING_INTERVAL:
                    last_polling_time = now
                    if system_state["stable_hex_state"] == HEX_STANDBY:
                        log("Polling Timer: Machine in Standby -> Sending safety WAKEUP")
                        system_state["last_published_state"] = ""
                        set_tx_sequence(PKT_MINUS, CMD_CYCLES_WAKEUP, "WAKEUP")
            
            if (now - last_stats_time) > SYSTEM_STATS_INTERVAL:
                publish_system_stats(system_state["mqtt_client"])
                last_stats_time = now

            try:
                data = ser.read(ser.in_waiting or 1)
                if data:
                    log_raw_packet("RX_RAW", data) 
                    buffer += data
            except serial.SerialException:
                if ser: ser.close()
                connect_vmc()
                buffer = b""
                continue

            idx_state = buffer.find(b'\x01\x04')
            if idx_state == -1:
                if len(buffer) > 64: buffer = b"" 
                continue
            if len(buffer) < idx_state + 8: continue
                
            packet = buffer[idx_state : idx_state + 8]
            buffer = buffer[idx_state + 8:]

            process_packet(packet)
            
            # --- Timed OFF state evaluation ---
            if system_state["eval_off_at"] > 0 and now > system_state["eval_off_at"]:
                log("Reading window (6s) closed without active gears -> Machine confirmed OFF.")
                system_state["current_virtual_state"] = "OFF"
                system_state["eval_off_at"] = 0.0
            
            if tx_session["counter"] == 0:
                update_mqtt_state()

            # --- TARGET FAN SPEED EVALUATION ---
            if system_state["target_fan_speed"] != -1:
                curr_str = system_state["current_virtual_state"]
                
                # If current state is incompatible ("Exhaust only" or errors)
                if curr_str in ["UNKNOWN", "ALERT", "Exhaust only"]:
                    log(f"State '{curr_str}' incompatible with regulation. Canceling.")
                    system_state["target_fan_speed"] = -1
                else:
                    curr_speed = 0 if curr_str == "OFF" else int(curr_str)
                    
                    # Target speed reached, disable control
                    if curr_speed == system_state["target_fan_speed"]:
                        log(f"Target speed {curr_speed} reached.")
                        system_state["target_fan_speed"] = -1
                    
                    # If no commands are executing (tx_session free)
                    elif tx_session["counter"] == 0 and system_state["eval_off_at"] == 0.0:
                        
                        # Counter is 0 or wait threshold exceeded
                        if system_state["fan_cycle_counter"] == 0 or system_state["fan_cycle_counter"] >= FAN_DELAY_CYCLES:
                            # Check direction
                            if system_state["target_fan_speed"] > curr_speed:
                                set_tx_sequence(PKT_PLUS, CMD_CYCLES_NORMAL, "FAN_PLUS")
                            else:
                                set_tx_sequence(PKT_MINUS, CMD_CYCLES_NORMAL, "FAN_MINUS")
                            
                            # Reset counter after scheduling transmission
                            system_state["fan_cycle_counter"] = 0
                        
                        # Increment cycle counter at each wait iteration
                        system_state["fan_cycle_counter"] += 1

            # --- PUBLISH MQTT STATE (IF PENDING) ---
            if tx_session["counter"] == 0:
                update_mqtt_state()

            # --- EXECUTE TRANSMISSION ---
            handle_serial_transmission()

        except KeyboardInterrupt: break
        except Exception as e: log(f"Err: {e}"); time.sleep(5)

if __name__ == "__main__": main()