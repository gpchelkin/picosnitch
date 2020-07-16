# MIT License

# Copyright (c) 2020 Eric Lesiuta

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import ipaddress
import json
import multiprocessing
import os
import signal
import sys
import time
import typing

import plyer
import psutil


def read() -> dict:
    file_path = os.path.join(os.path.expanduser("~"), ".config", "picosnitch", "snitch.json")
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8", errors="surrogateescape") as json_file:
            data = json.load(json_file)
        assert all(key in data for key in ["Config", "Errors", "Executables", "Names", "Processes"])
        return data
    return {
        "Config": {"Polling interval": 0.2, "Write interval": 600, "Use pcap": False},
        "Errors": [],
        "Executables": [],
        "Names": [],
        "Processes": {}
        }


def write(snitch: dict):
    file_path = os.path.join(os.path.expanduser("~"), ".config", "picosnitch", "snitch.json")
    if not os.path.isdir(os.path.dirname(file_path)):
        os.makedirs(os.path.dirname(file_path))
    try:
        with open(file_path, "w", encoding="utf-8", errors="surrogateescape") as json_file:
            json.dump(snitch, json_file, indent=2, separators=(',', ': '), sort_keys=True, ensure_ascii=False)
    except Exception:
        toast("picosnitch write error", file=sys.stderr)


def terminate(snitch: dict, process: multiprocessing.Process = None):
    write(snitch)
    if process is not None:
        process.terminate()
    sys.exit(0)


def poll(snitch: dict, last_connections: set, pcap_dict: dict) -> set:
    ctime = time.ctime()
    proc = {"name": "", "exe": "", "cmdline": "", "pid": ""}
    current_connections = set(psutil.net_connections(kind='inet'))
    for conn in current_connections - last_connections:
        try:
            if conn.raddr and not ipaddress.ip_address(conn.raddr.ip).is_private:
                _ = pcap_dict.pop(conn.laddr.port, None)
                proc = psutil.Process(conn.pid).as_dict(attrs=["name", "exe", "cmdline", "pid"], ad_value="")
                if proc["exe"] not in snitch["Processes"]:
                    new_entry(snitch, proc, conn.raddr.ip, ctime)
                else:
                    update_entry(snitch, proc, conn.raddr.ip, ctime)
        except Exception:
            error = str(conn)
            if conn.pid == proc["pid"]:
                error += str(proc["pid"])
            else:
                error += "{process no longer exists}"
            snitch["Errors"].append(ctime + " " + error)
            toast("picosnitch polling error: " + error, file=sys.stderr)
    for conn in pcap_dict:
        snitch["Errors"].append(ctime + " " + str(conn))
        toast("picosnitch missed connection: " + str(conn), file=sys.stderr)
    return current_connections


def new_entry(snitch: dict, proc: dict, raddr_ip: str, ctime: str):
    snitch["Executables"].append(proc["exe"])
    snitch["Names"].append(proc["name"])
    if snitch["Names"].count(proc["name"]) > 1:
        snitch["Names"][-1] += " (different executable location)"
    snitch["Processes"][proc["exe"]] = {
        "name": proc["name"],
        "cmdlines": [str(proc["cmdline"])],
        "first seen": ctime,
        "last seen": ctime,
        "days seen": 1,
        "remote addresses": [raddr_ip]
    }
    toast("First network connection detected for " + proc["name"])


def update_entry(snitch: dict, proc: dict, raddr_ip: str, ctime: str):
    entry = snitch["Processes"][proc["exe"]]
    if proc["name"] not in entry["name"]:
        entry["name"] += " alternative=" + proc["name"]
    if str(proc["cmdline"]) not in entry["cmdlines"]:
        entry["cmdlines"].append(str(proc["cmdline"]))
    if raddr_ip not in entry["remote addresses"]:
        entry["remote addresses"].append(raddr_ip)
    if ctime.split()[:3] != entry["last seen"].split()[:3]:
        entry["days seen"] += 1
    entry["last seen"] = ctime


def loop():
    snitch = read()
    pcap_proc, queue = init_pcap(snitch)
    pcap_dict = {}
    signal.signal(signal.SIGTERM, lambda *args: terminate(snitch, pcap_proc))
    signal.signal(signal.SIGINT, lambda *args: terminate(snitch, pcap_proc))
    connections = set()
    polling_interval = snitch["Config"]["Polling interval"]
    write_counter = int(snitch["Config"]["Write interval"] / polling_interval)
    counter = 0
    while True:
        if queue is not None:
            pcap_dict = {}
            known_ports = [conn.laddr.port for conn in connections]
            while not queue.empty():
                packet = q.get()
                if packet["laddr_port"] not in known_ports:
                    pcap_dict[packet["laddr_port"]] = packet
        connections = poll(snitch, connections, pcap_dict)
        time.sleep(polling_interval)
        if counter >= write_counter:
            write(snitch)
            counter = 0
        else:
            counter += 1


def toast(msg: str, file=sys.stdout):
    try:
        plyer.notification.notify(title="picosnitch",
                                  message=msg,
                                  app_name="picosnitch")
    except Exception:
        print(msg, file=file)


def init_pcap(snitch: dict) -> typing.Tuple[multiprocessing.Process, multiprocessing.Queue]:
    if snitch["Config"]["Use pcap"]:
        import scapy
        from scapy.all import sniff

        def parse_packet(packet) -> dict:
            output = {}
            src = packet.getlayer(scapy.layers.all.IP).src
            dst = packet.getlayer(scapy.layers.all.IP).dst
            if ipaddress.ip_address(src).is_private:
                output["direction"] = "outgoing"
                output["laddr_ip"] = src
                output["laddr_port"] = packet.sport
                output["raddr_ip"] = dst
            elif ipaddress.ip_address(dst).is_private:
                output["direction"] = "incoming"
                output["laddr_ip"] = dst
                output["laddr_port"] = packet.dport
                output["raddr_ip"] = src
            return output

        def filter_packet(packet) -> bool:
            try:
                src = ipaddress.ip_address(packet.getlayer(scapy.layers.all.IP).src)
                dst = ipaddress.ip_address(packet.getlayer(scapy.layers.all.IP).dst)
                return src.is_private != dst.is_private
            except:
                return False

        def sniffer(q):
            sniff(count=0, prn=lambda x: q.put(parse_packet(x)), lfilter=filter_packet)

        if __name__ == "__main__":
            q = multiprocessing.Queue()
            p = multiprocessing.Process(target=sniffer, args=(q,))
            p.start()
            return p, q

    return None, None


def main():
    if os.name == "posix":
        import daemon
        with daemon.DaemonContext():
            loop()
    else:
        loop()


if __name__ == "__main__":
    sys.exit(main())
