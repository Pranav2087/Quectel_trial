#!/usr/bin/env python3
import subprocess
import serial
import serial.tools.list_ports
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import os
import socket
import re # Moved import to top
import glob
#Check Status V4.2 07/01/2026

# --- CONFIGURATION ---
DEBUG_FILE = "/home/ubuntu/debug.log"
NETDIAG_FILE = "/home/ubuntu/netdiag.log"
PING_COUNT = 5
PING_TIMEOUT = 2
DNS_HOSTS = ["google.com", "mqtts.sibcaconnect.com", "commander.omniconn.ai"]
PING_HOSTS = ["8.8.8.8", "1.1.1.1"]
 
# INTERFACE NAMES (Check these via 'ip a' on your device)
IFACE_WWAN = "wwan0"
IFACE_RNDIS = "usb0"
IFACE_VPN = "tun0"
 
# -----------------------------
# Logging
# -----------------------------
def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"{ts} - {msg}"
    print(line)
# -----------------------------
# Quectel Ports
# -----------------------------
def get_quectel_ports():
    ports = []

    for path in glob.glob("/dev/serial/by-id/*"):
        name = os.path.basename(path)

        # Only allow SimTech devices
        if "usb-Quectel_EG25-G" in name:
            ports.append(os.path.realpath(path))  # resolves to /dev/ttyUSBx

    return ports
# -----------------------------
# Signal Strength
# -----------------------------
def get_signal_strength(port):
    """
    Reads signal strength. Must be called when port is NOT open elsewhere.
    """
    try:
        with serial.Serial(port, 115200, timeout=1, write_timeout=1) as ser:
            ser.reset_input_buffer()
            ser.write(b'AT+QENG="servingcell"\r')
            time.sleep(0.5)
            # Read a larger chunk to ensure we get the full response
            sig_resp = ser.read(ser.in_waiting or 256).decode(errors='ignore')
            if '+QENG:' in sig_resp:
                # Example: +CPSI: LTE,Online,460,01,0x5005,2254901,266,3,0,-49,-14,-35
                # Extract all numbers
                numbers = re.findall(r'-?\d+\.?\d*', sig_resp)
                if len(numbers) < 4:
                    log(f"Signal response parsed incomplete: {sig_resp.strip()}")
                    return
                
                # Usually the last 4 are RSRP, RSRQ, RSSI, SINR in LTE mode
                rsrp, rsrq, rssi, sinr= numbers[-4:]
                rsrp = int(rsrp)
                rsrq = int(rsrq)
                rssi = int(rssi)
                sinr = float(sinr)
                #Converting the rsrq and rssi to dbm
                log(f"Signal Quality (Raw): RSRP={rsrp}, RSRQ={rsrq}, RSSI={rssi}, SINR={sinr}")
            else:
                log("Signal check failed: Unexpected response.")
    except Exception as e:
        log(f"Signal check error: {e}")
# -----------------------------
# AT port detection (parallel)
# -----------------------------
def detect_at_port(timeout=5):
    log("Detecting modem AT port...")
    ports = serial.tools.list_ports.comports()
    candidates = get_quectel_ports()
    if not candidates:
        log("❌ No USB/Serial devices found.")
        return None
 
    found_flag = threading.Event()
    found_port = [None]
 
    def test_port(port):
        if found_flag.is_set(): return
        try:
            # check port
            with serial.Serial(port, 115200, timeout=0.1, write_timeout=0.1) as ser:
                ser.write(b"AT\r")
                resp = ser.read(64)
                if b"OK" in resp:
                    if not found_flag.is_set():
                        found_flag.set()
                        found_port[0] = port
                        log(f"✅ Detected AT port: {port}")
        except Exception:
            return
 
    # CHANGE: Do not use 'with'. Create executor manually.
    executor = ThreadPoolExecutor(max_workers=len(candidates) + 1)
    for p in candidates:
        executor.submit(test_port, p)
    
    start = time.time()
    while not found_flag.is_set() and time.time() - start < timeout:
        time.sleep(0.1)
 
    # The executor is not shutdown here. Let it die in the background.
    # Allows to proceed immediately once the port is found.
 
    if found_port[0]:
        # Wait a split second to ensure the thread releases the file handle
        time.sleep(0.2)
        get_signal_strength(found_port[0])
        return found_port[0]
    
    log("❌ No responsive AT port found.")
    return None 
# -----------------------------
# Connectivity check (parallel)
# -----------------------------
def check_dns_host(host):
    try:
        socket.gethostbyname(host)
        log(f"DNS check for {host} SUCCESS")
        return True
    except socket.gaierror:
        log(f"DNS check for {host} FAILED")
        return False
 
def check_ping_host(host):
    try:
        result = subprocess.run(
            ["ping", "-c", str(PING_COUNT), "-W", str(PING_TIMEOUT), host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        
        if result.returncode == 0:
            log(f"PING check for {host} SUCCESS") 
            return True
        else:
            log(f"PING check for {host} FAILED (Return Code: {result.returncode})") 
            return False

    except Exception as e:
        # This block catches exceptions outside of ping execution (e.g., ping command not found)
        log(f"PING execution for {host} FAILED with Exception: {e}")
        return False

def check_connectivity_parallel():
    log("Running connectivity check...")
    dns_ok = False
    ping_ok = False

    with ThreadPoolExecutor() as executor:
        futures = {
            executor.submit(check_dns_host, h): "dns" for h in DNS_HOSTS
        }
        futures.update({
            executor.submit(check_ping_host, h): "ping" for h in PING_HOSTS
        })

        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as e:
                log(f"Check error: {e}")
                continue

            check_type = futures[future]

            if check_type == "dns" and result:
                dns_ok = True

            if check_type == "ping" and result:
                ping_ok = True

            if dns_ok and ping_ok:
                log("Connectivity check SUCCESS (DNS + Ping OK)")
                return True

    log("Connectivity check FAILED (DNS + Ping not OK)")
    return False
# -----------------------------
# Netdiagnostics
# -----------------------------
def log_cmd(cmd, log_lines, description=""):
    try:
        output = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        out_text = output.stdout.strip()
        if output.stderr:
            out_text += f"\nSTDERR: {output.stderr.strip()}"
        log_lines.append(f"{description}:\n{out_text}\n\n" if description else out_text + "\n")
    except Exception as e:
        log_lines.append(f"{description} - command failed: {e}\n")
 
def netdiag_log(interface):
    log_lines = []
    log_lines.append(f"\n==== Network Diagnostic {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====\n")
    
    try:
        gateway_ip = subprocess.check_output(
            f"ip addr show {interface} | grep peer | awk '{{print $4}}' | cut -d/ -f1",
            shell=True, text=True
        ).strip()
    except Exception:
        gateway_ip = "Not found"
 
    commands = [
        ("ip route show", "Default routes"),
        ("ip a", "Current IP status"),
        (f"ip link show {interface}", "{interface} Link State"),
        (f"ip link show {IFACE_VPN} 2>/dev/null || echo 'tun0 not found'", "VPN interface state"),
        ("lsusb -t", "USB driver status"),
    ]
 
    for cmd, desc in commands:
        log_cmd(cmd, log_lines, desc)
 
    if gateway_ip != "Not found" and gateway_ip != "":
        log_cmd(f"ping -I {interface} -c 2 -W 2 {gateway_ip}", log_lines, f"Ping gateway {gateway_ip}")
    else:
        log_lines.append("Ping gateway: gateway IP not found\n")
 
    log_cmd(f"ping -I {interface} -c 2 -W 2 8.8.8.8", log_lines, "Ping public IP via {interface}")
    log_lines.append("===============================================\n")
    
    with open(NETDIAG_FILE, "a") as f:
        f.writelines(log_lines)
 
# -----------------------------
# Restart connection
# -----------------------------
def restart_connection():
    log("Attempting Service Restart Sequence...")

    log("Resetting USB modem modes...")
    try:
        subprocess.run("sudo usb_modeswitch -R -v 2c7c -p 0125", shell=True, timeout=15)
        time.sleep(4)
    except subprocess.TimeoutExpired:
        log("⚠️ USB mode switch command timed out, continuing anyway.")

    log("Restarting Quectel service...")
    try:
        subprocess.run(
            "sudo systemctl restart quectel_connect.service",
            shell=True,
            timeout=30
        )
        log("Quectel service restart command completed")
    except subprocess.TimeoutExpired:
        log("⚠️ Quectel service restart timed out after 30s, continuing...")

    time.sleep(5)  

    log("Restarting VPN service...")
    try:
        subprocess.run(
            "sudo systemctl restart openvpn@client.service",
            shell=True,
            timeout=30
        )
        log("✅ VPN service restart command completed")
    except subprocess.TimeoutExpired:
        log("⚠️ VPN service restart timed out after 30s, continuing...")

    time.sleep(5)  # Give VPN time to establish

# -----------------------------
# Fail-safe
# -----------------------------
def fail_safe(at_port,timeout=30):
    log("!!! Entering FAIL-SAFE Mode !!!")
    log("Stopping PPP and VPN services...")
    subprocess.run("sudo systemctl stop quectel_connect.service", shell=True)
    subprocess.run("sudo systemctl stop openvpn@client.service", shell=True)
    time.sleep(2)
 
    log(f"Switching Modem PID to RNDIS via {at_port}...")
    try:
        with serial.Serial(at_port, 115200, timeout=1) as ser:
            ser.write(b'at+QCFG="usbnet",3\r')
            time.sleep(0.5)
    except Exception as e:
        log(f"Port write error (likely expected due to reset): {e}")
 
    log("Waiting 20s for modem to re-enumerate on USB bus...")
    time.sleep(20)
 
    # Request IP on RNDIS interface
    log(f"Requesting DHCP on {IFACE_RNDIS}...")
    subprocess.run(f"sudo pkill -f 'dhclient -v {IFACE_RNDIS}' 2>/dev/null", shell=True)
    subprocess.run(f"sudo dhclient {IFACE_RNDIS} -q", shell=True) 
    time.sleep(10)
    
    log("Querying modem for carrier DNS via AT+CGCONTRDP...")
    at_port_dns = detect_at_port()
    dns_servers = []

    try:
        with serial.Serial(at_port_dns, 115200, timeout=2) as ser:
            ser.write(b"AT+CGCONTRDP=1\r")
            time.sleep(1)

            response = ser.read(4096).decode(errors="ignore")
            log(f"CGCONTRDP raw response: {response.strip()}")

            for line in response.splitlines():
                if "+CGCONTRDP:" in line:
                    parts = [p.strip().strip('"') for p in line.split(",")]
                    if len(parts) >= 2:
                        dns1 = parts[-2]
                        dns2 = parts[-1]
                        if dns1 and dns2:
                            dns_servers = [dns1, dns2]
                            break

    except Exception as e:
        log(f"Failed to query DNS via AT port: {e}")

    if dns_servers:
        log(f"Applying carrier DNS: {dns_servers}")
        resolv_conf = "\n".join(
            f"nameserver {dns}" for dns in dns_servers
        ) + "\n"
    else:
        log("Carrier DNS unavailable. Falling back to Google DNS.")
        resolv_conf = (
            "nameserver 8.8.8.8\n"
            "nameserver 8.8.4.4\n"
        )

    subprocess.run(
        f"echo -e '{resolv_conf}' | sudo tee /etc/resolv.conf",
        shell=True
    )

    netdiag_log()

    log("Restarting OpenVPN...")
    subprocess.run("sudo systemctl start openvpn@client.service", shell=True)
# -----------------------------
# Main watchdog
# -----------------------------
def main():
    log("------------------------------------------")
    log("Starting modem connectivity watchdog...")
    
    at_port = detect_at_port()
    
    if check_connectivity_parallel():
        log("✅ System Online. No action needed.")
        return
 
    # --- Phase 1: Diagnostics & Soft Restart ---
    log("⚠️ Connectivity missing. Logging diagnostics.")
    netdiag_log("wwan0")
    
    restart_connection()
    
    if check_connectivity_parallel():
        log("✅ Internet restored after service restart.")
        return
    # --- Phase 2: Fail-safe (PID Switch) ---
    at_port_failsafe = detect_at_port()
    if at_port_failsafe:
        fail_safe(at_port_failsafe)
        if check_connectivity_parallel():
            log("✅ Internet restored with fail-safe (RNDIS).")
            return
    else:
        netdiag_log("usb0")
        log("Cannot attempt fail-safe: No AT port available.")
 
    # --- Phase 3: Hard Reboot ---
    log("❌❌ All recovery attempts failed. REBOOTING SYSTEM. ❌❌")
    #subprocess.run("sudo reboot", shell=True)
 
if __name__ == "__main__":
    main()
