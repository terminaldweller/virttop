#!/usr/bin/env python
"""virt top"""

# ideally we would like to use the monkeypatch but it is untested
# and experimental
# import defusedxml  # type:ignore
# defusedxml.defuse_stdlib()
import argparse
import csv
import dataclasses
import enum
import os
import signal
import sys
import time
import typing

# we are only using this for type annotation
from xml.dom.minidom import Document  # nosec

from defusedxml import ElementTree  # type:ignore
from defusedxml import minidom
import libvirt  # type:ignore


# pylint: disable=unused-argument
def sig_handler_sigint(signum, frame):
    """Just to handle C-c gracefully"""
    print()
    sys.exit(0)


class Argparser:  # pylint: disable=too-few-public-methods
    """Argparser class."""

    def __init__(self):
        self.parser = argparse.ArgumentParser()
        self.parser.add_argument(
            "--delay",
            "-d",
            type=float,
            help="The delay between updates",
            default=5,
        )
        self.parser.add_argument(
            "--uri",
            "-u",
            nargs="+",
            type=str,
            help="A list of URIs to connect to seperated by commas",
            default=["qemu:///system"],
        )
        self.args = self.parser.parse_args()


# pylint: disable=too-few-public-methods
class Colors(enum.EnumType):
    """static color definitions"""

    purple = "\033[95m"
    blue = "\033[94m"
    green = "\033[92m"
    yellow = "\033[93m"
    red = "\033[91m"
    grey = "\033[1;37m"
    darkgrey = "\033[1;30m"
    cyan = "\033[1;36m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    blueblue = "\x1b[38;5;24m"
    greenie = "\x1b[38;5;23m"
    goo = "\x1b[38;5;22m"
    screen_clear = "\033c\033[3J"
    hide_cursor = "\033[?25l"


@dataclasses.dataclass
# pylint: disable=too-many-instance-attributes
class VirtData:
    """Holds the data that we collect to display to the user"""

    vm_id: typing.List[str] = dataclasses.field(default_factory=list)
    name: typing.List[str] = dataclasses.field(default_factory=list)
    cpu_times: typing.List[str] = dataclasses.field(default_factory=list)
    mem_actual: typing.List[str] = dataclasses.field(default_factory=list)
    mem_unused: typing.List[str] = dataclasses.field(default_factory=list)
    write_bytes: typing.List[str] = dataclasses.field(default_factory=list)
    read_bytes: typing.List[str] = dataclasses.field(default_factory=list)
    macs: typing.List[str] = dataclasses.field(default_factory=list)
    ips: typing.List[str] = dataclasses.field(default_factory=list)
    disk_reads: typing.List[str] = dataclasses.field(default_factory=list)
    disk_writes: typing.List[str] = dataclasses.field(default_factory=list)
    snapshot_counts: typing.List[str] = dataclasses.field(default_factory=list)
    uri: typing.List[str] = dataclasses.field(default_factory=list)
    memory_pool: typing.List[str] = dataclasses.field(default_factory=list)

    pools: typing.List[libvirt.virStoragePool] = dataclasses.field(
        default_factory=list
    )


def get_network_info(
    xml_doc: Document,
) -> typing.Dict[str, str]:
    """returns the network info"""
    result_dict = {}
    interface_types = xml_doc.getElementsByTagName("interface")
    for interface_type in interface_types:
        interface_nodes = interface_type.childNodes
        for interface_node in interface_nodes:
            if interface_node.nodeName[0:1] != "#":
                for attr in interface_node.attributes.keys():
                    result_dict[
                        interface_node.attributes[attr].name
                    ] = interface_node.attributes[attr].value
    return result_dict


def get_arp_table() -> typing.Dict[str, str]:
    """Get the ARP table. return a dict with MAC/IP as key/value pair."""
    result: typing.Dict[str, str] = {}
    with open("/proc/net/arp", encoding="utf-8") as arp_table:
        reader = csv.reader(arp_table, skipinitialspace=True, delimiter=" ")
        for arp_entry in reader:
            result[arp_entry[3]] = arp_entry[0]

    return result


def get_ip_from_arp_table(arp_table: typing.Dict[str, str], mac: str) -> str:
    """get IP from MAC address using the arp table"""
    try:
        return arp_table[mac]
    except KeyError:
        return "N/A"


def get_disk_info(
    xml_doc: Document,
) -> typing.Dict[str, str]:
    """returns the disk info"""
    result_dict: typing.Dict = {}
    disk_types = xml_doc.getElementsByTagName("disk")
    for disk_type in disk_types:
        disk_nodes = disk_type.childNodes
        for disk_node in disk_nodes:
            if disk_node.nodeName[0:1] != "#":
                for attr in disk_node.attributes.keys():
                    result_dict[
                        disk_node.attributes[attr].name
                    ] = disk_node.attributes[attr].value

    return result_dict


# pylint: disable=too-many-locals
def ffs(
    offset: int,
    header_list: typing.Optional[typing.List[str]],
    numbered: bool,
    *args,
) -> typing.List[str]:
    """A simple columnar printer"""
    max_column_width = []
    lines = []
    numbers_f: typing.List[int] = []
    dummy = []

    if sys.stdout.isatty():
        greenie = Colors.greenie
        bold = Colors.BOLD
        endc = Colors.ENDC
        goo = Colors.goo
        blueblue = Colors.blueblue
    else:
        greenie = ""
        bold = ""
        endc = ""
        goo = ""
        blueblue = ""

    for arg in args:
        max_column_width.append(max(len(repr(argette)) for argette in arg))

    if header_list is not None:
        if numbered:
            numbers_f.extend(range(1, len(args[-1]) + 1))
            max_column_width.append(
                max(len(repr(number)) for number in numbers_f)
            )
            header_list.insert(0, "idx")

        index = range(0, len(header_list))
        for header, width, i in zip(header_list, max_column_width, index):
            max_column_width[i] = max(len(header), width) + offset

        for i in index:
            dummy.append(
                greenie
                + bold
                + header_list[i].ljust(max_column_width[i])
                + endc
            )
        lines.append("".join(dummy))
        dummy.clear()

    index2 = range(0, len(args[-1]))
    for i in index2:
        if numbered:
            dummy.append(
                goo + bold + repr(i).ljust(max_column_width[0]) + endc
            )
            for arg, width in zip(args, max_column_width[1:]):
                dummy.append(blueblue + (arg[i]).ljust(width) + endc)
        else:
            for arg, width in zip(args, max_column_width):
                dummy.append(blueblue + (arg[i]).ljust(width) + endc)
        lines.append("".join(dummy))
        dummy.clear()
    return lines


def size_abr(num: float, shift_by: float) -> str:
    """Rounds and abbreviates floats."""
    num = num * shift_by
    if num < 1000:
        return repr(num)
    if num < 1_000_000.0:
        return repr(round(num / 1_000, 2)) + " KB"
    if num < 1_000_000_000:
        return repr(round(num / 1_000_000, 2)) + " MB"
    if num < 1_000_000_000_000:
        return repr(round(num / 1_000_000_000, 2)) + " GB"
    return "N/A"


# pylint: disable=too-many-locals
def fill_virt_data_uri(
    conn: libvirt.virConnect,
    active_hosts: typing.List[int],
    virt_data: VirtData,
    arp_table: typing.Dict[str, str],
) -> None:
    """fill VirtData for one URI."""
    for host_id in active_hosts:
        virt_data.uri.append(conn.getURI())
        dom = conn.lookupByID(host_id)
        virt_data.snapshot_counts.append(repr(dom.snapshotNum()))
        try:
            virt_data.cpu_times.append(
                repr(
                    int(
                        dom.getCPUStats(total=True)[0]["cpu_time"]
                        / 1_000_000_000
                    )
                )
                + "s"
            )
        except:
            virt_data.cpu_times.append("n/a")
        xml_doc = minidom.parseString(dom.XMLDesc())
        virt_data.name.append(dom.name())

        mem_stats = dom.memoryStats()
        if "actual" in mem_stats:
            virt_data.mem_actual.append(size_abr(mem_stats["actual"], 1000))
        else:
            virt_data.mem_actual.append("n/a")

        # BSD guests dont support mem balloons?
        try:
            virt_data.mem_unused.append(size_abr(mem_stats["available"], 1000))
        except KeyError:
            virt_data.mem_unused.append("N/A")

        tree = ElementTree.fromstring(dom.XMLDesc())
        iface = tree.find("devices/interface/target").get("dev")
        stats = dom.interfaceStats(iface)
        virt_data.write_bytes.append(size_abr(stats[4], 1))
        virt_data.read_bytes.append(size_abr(stats[0], 1))

        found_the_pool: bool = False
        disk = tree.find("devices/disk/source").get("file")
        for pool in virt_data.pools:
            if os.path.basename(disk) in pool.listVolumes():
                virt_data.memory_pool.append(pool.name())
                found_the_pool = True
        # you could delete the pool but keep the volumes inside
        # which results in a functional VM but it wont have a
        # volume inside a pool that we can detect
        if not found_the_pool:
            virt_data.memory_pool.append("N/A")

        disk_info = get_disk_info(xml_doc)
        image_name = disk_info["file"]
        _, rd_bytes, _, wr_bytes, _ = dom.blockStats(image_name)
        virt_data.disk_reads.append(size_abr(rd_bytes, 1))
        virt_data.disk_writes.append(size_abr(wr_bytes, 1))

        network_info = get_network_info(xml_doc)
        virt_data.macs.append(network_info["address"])
        # virt_data.ips.append(get_ip_by_arp(network_info["address"]))
        # TODO-this is obviously not going to work for remote URIs
        virt_data.ips.append(
            get_ip_from_arp_table(arp_table, network_info["address"])
        )


def main() -> None:
    """entrypoint"""
    signal.signal(signal.SIGINT, sig_handler_sigint)
    argparser = Argparser()
    print(Colors.screen_clear, end="")
    while True:
        virt_data = VirtData()
        arp_table = get_arp_table()
        for hv_host in argparser.args.uri:
            conn = libvirt.openReadOnly(hv_host)
            active_hosts = conn.listDomainsID()
            if len(active_hosts) > 0:
                virt_data.pools = conn.listAllStoragePools()
                # for pool in virt_data.pools:
                #     print(pool.listVolumes())
                # networks = conn.listAllNetworks()
                # print([pool.name() for pool in conn.listAllStoragePools()])
                # print([net.name() for net in conn.listAllNetworks()])
                virt_data.vm_id = [
                    repr(vm_id) for vm_id in conn.listDomainsID()
                ]
                fill_virt_data_uri(conn, active_hosts, virt_data, arp_table)
                # for conn_id in conn.listAllDomains():
                #     print(conn_id.name())
                #     print(conn_id.state())
            else:
                print("no active VMs found.")
                sys.exit(1)

        lines = ffs(
            2,
            [
                "ID",
                "NAME",
                "CPU",
                "MEM_ACTUAL",
                "MEM_AVAIL",
                "NET_WRITE_B",
                "NET_READ_B",
                "MAC",
                "IP",
                "IO_READ_B",
                "IO_WRITE_B",
                "SNAPSHOTS",
                "URI",
                "STORAGE_POOL",
            ],
            False,
            virt_data.vm_id,
            virt_data.name,
            virt_data.cpu_times,
            virt_data.mem_actual,
            virt_data.mem_unused,
            virt_data.write_bytes,
            virt_data.read_bytes,
            virt_data.macs,
            virt_data.ips,
            virt_data.disk_reads,
            virt_data.disk_writes,
            virt_data.snapshot_counts,
            virt_data.uri,
            virt_data.memory_pool,
        )
        for line in lines:
            print(line)
        time.sleep(argparser.args.delay)
        # clears the screen
        print(Colors.screen_clear, end="")
        print(Colors.hide_cursor, end="")


if __name__ == "__main__":
    main()
