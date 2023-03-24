#!/usr/bin/env python
"""virttop"""

# ideally we would like to use the monkeypatch but it is untested
# and experimental
# import defusedxml  # type:ignore
# defusedxml.defuse_stdlib()
import argparse
import asyncio
import csv
import curses
import dataclasses
import functools
import logging
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
import tomllib


def request_cred(credentials, sasl_user, sasl_pass):
    """Credential handler."""
    for credential in credentials:
        if credential[0] == libvirt.VIR_CRED_AUTHNAME:
            credential[4] = sasl_user
        elif credential[0] == libvirt.VIR_CRED_PASSPHRASE:
            credential[4] = sasl_pass
    return 0


def do_cleanup(stdscr):
    """Return the terminal to a sane state."""
    curses.nocbreak()
    stdscr.keypad(False)
    curses.echo()
    curses.endwin()
    print()


# pylint: disable=unused-argument
def sig_handler_sigint(signum, frame, stdscr):
    """Just to handle C-c cleanly"""
    do_cleanup(stdscr)
    sys.exit(0)


class Argparser:  # pylint: disable=too-few-public-methods
    """Argparser class."""

    def __init__(self):
        self.parser = argparse.ArgumentParser()
        self.parser.add_argument(
            "--uri",
            "-u",
            nargs="+",
            type=str,
            help="A list of URIs to connect to seperated by commas",
            default=["qemu:///system"],
        )
        self.parser.add_argument(
            "--config",
            "-c",
            type=str,
            help="Path to the config file",
            default="~/.virttop.toml",
        )
        self.parser.add_argument(
            "--active",
            "-a",
            type=bool,
            help="Show active VMs only",
            default=False,
        )
        self.parser.add_argument(
            "--logfile",
            "-l",
            type=str,
            help="Location of the log file",
            default="~/.virttop.log",
        )

        self.args = self.parser.parse_args()


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

    pools: typing.List[libvirt.virStoragePool] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class ConfigData:
    """Holds the config data"""

    sasl_user: str = dataclasses.field(default_factory=str)
    sasl_password: str = dataclasses.field(default_factory=str)
    color: typing.Dict[str, int] = dataclasses.field(default_factory=dict)


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
                    result_dict[disk_node.attributes[attr].name] = disk_node.attributes[
                        attr
                    ].value

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

    for arg in args:
        max_column_width.append(max(len(repr(argette)) for argette in arg))

    if header_list is not None:
        if numbered:
            numbers_f.extend(range(1, len(args[-1]) + 1))
            max_column_width.append(max(len(repr(number)) for number in numbers_f))
            header_list.insert(0, "idx")

        index = range(0, len(header_list))
        for header, width, i in zip(header_list, max_column_width, index):
            max_column_width[i] = max(len(header), width) + offset

        for i in index:
            dummy.append(header_list[i].ljust(max_column_width[i]))
        lines.append("".join(dummy))
        dummy.clear()

    index2 = range(0, len(args[-1]))
    active_count: int = 1
    for i in index2:
        if numbered:
            if int(args[0][i]) >= 0:
                active_count += 1
            dummy.append(repr(i).ljust(max_column_width[0]))
            for arg, width in zip(args, max_column_width[1:]):
                if int(args[0][i]) >= 0:
                    dummy.append((arg[i]).ljust(width))
                else:
                    dummy.append((arg[i]).ljust(width))
        else:
            for arg, width in zip(args, max_column_width):
                if int(args[0][i]) >= 0:
                    dummy.append((arg[i]).ljust(width))
                else:
                    dummy.append((arg[i]).ljust(width))
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
    hosts: typing.List[libvirt.virDomain],
    virt_data: VirtData,
    arp_table: typing.Dict[str, str],
    active_only: bool,
) -> None:
    """fill VirtData for one URI."""
    for host in hosts:
        try:
            if active_only and host.ID() <= 0:
                continue
            virt_data.vm_id.append(repr(host.ID()))
            virt_data.uri.append(conn.getURI())
            virt_data.name.append(host.name())
            dom = conn.lookupByName(host.name())

            virt_data.snapshot_counts.append(repr(dom.snapshotNum()))
            if host.ID() > 0:
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
            else:
                virt_data.cpu_times.append("-")

            xml_doc = minidom.parseString(dom.XMLDesc())

            if host.ID() >= 0:
                mem_stats = dom.memoryStats()
                if "actual" in mem_stats:
                    virt_data.mem_actual.append(size_abr(mem_stats["actual"], 1000))
                else:
                    virt_data.mem_actual.append("n/a")

                try:
                    virt_data.mem_unused.append(size_abr(mem_stats["available"], 1000))
                except KeyError:
                    virt_data.mem_unused.append("N/A")
            else:
                virt_data.mem_actual.append("-")
                virt_data.mem_unused.append("-")

            tree = ElementTree.fromstring(dom.XMLDesc())
            if host.ID() >= 0:
                iface = tree.find("devices/interface/target").get("dev")
                stats = dom.interfaceStats(iface)
                virt_data.write_bytes.append(size_abr(stats[4], 1))
                virt_data.read_bytes.append(size_abr(stats[0], 1))
            else:
                virt_data.write_bytes.append("-")
                virt_data.read_bytes.append("-")

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

            try:
                disk_info = get_disk_info(xml_doc)
                image_name = disk_info["file"]
            except Exception as exception:
                image_name = "X"
                logging.exception(exception)
            if host.ID() >= 0:
                _, rd_bytes, _, wr_bytes, _ = dom.blockStats(image_name)
                virt_data.disk_reads.append(size_abr(rd_bytes, 1))
                virt_data.disk_writes.append(size_abr(wr_bytes, 1))

                network_info = get_network_info(xml_doc)
                virt_data.macs.append(network_info["address"])
                # TODO-this is obviously not going to work for remote URIs
                virt_data.ips.append(
                    get_ip_from_arp_table(arp_table, network_info["address"])
                )
            else:
                virt_data.disk_reads.append("-")
                virt_data.disk_writes.append("-")
                virt_data.macs.append("-")
                virt_data.ips.append("-")
        except Exception as exception:
            logging.exception(exception)
            pass


def read_config(config_path) -> ConfigData:
    """read the config"""
    config_data = ConfigData()
    try:
        with open(os.path.expanduser(config_path), "rb") as conf_file:
            data = tomllib.load(conf_file)
            for key, value in data.items():
                match key:
                    case "sasl_user":
                        config_data.sasl_user = value
                    case "sasl_password":
                        config_data.sasl_password = value
                    case "color":
                        for key, value in value.items():
                            config_data.color[key] = value
                    case _:
                        print(f"warning: unknown key, {key}, found.")
    except FileNotFoundError:
        pass

    return config_data


def curses_init():
    """Initialize ncurses."""
    stdscr = curses.initscr()
    curses.start_color()
    curses.use_default_colors()
    curses.curs_set(False)
    curses.noecho()
    curses.cbreak()
    stdscr.keypad(True)
    curses.halfdelay(4)
    return stdscr


def init_color_pairs(config_data: ConfigData) -> None:
    """Initialize the curses color pairs."""
    curses.init_pair(
        1, config_data.color["name_column_fg"], config_data.color["name_column_bg"]
    )
    curses.init_pair(
        2, config_data.color["active_row_fg"], config_data.color["active_row_bg"]
    )
    curses.init_pair(
        3, config_data.color["inactive_row_fg"], config_data.color["inactive_row_bg"]
    )
    curses.init_pair(4, config_data.color["box_fg"], config_data.color["box_bg"])
    curses.init_pair(
        5, config_data.color["selected_fg"], config_data.color["selected_bg"]
    )


def get_visible_rows(max_rows: int, sel: int) -> typing.Tuple[int, int]:
    """Returns the range of columns that will be visible based on max_rows."""
    win_min_row = sel + 2 - int(max_rows / 2)
    win_min_row = max(win_min_row, 0)
    win_max_row = sel + 2 + int(max_rows / 2)
    win_max_row = max(win_max_row, max_rows)
    return win_min_row, win_max_row


async def start_domain(dom):
    """Start a domain."""
    try:
        task = asyncio.create_task(dom.createWithFlags())
        await asyncio.sleep(0)
        return task
    except Exception as exception:
        logging.exception(exception)


async def destroy_domain(dom):
    """Destroy a domain gracefully."""
    try:
        task = asyncio.create_task(
            dom.destroyFlags(flags=libvirt.VIR_DOMAIN_DESTROY_GRACEFUL)
        )
        await asyncio.sleep(0)
        return task
    except Exception as exception:
        logging.exception(exception)


async def shutdown_domain(dom):
    """Shutdown a domain."""
    try:
        task = asyncio.create_task(dom.shutdownFlags())
        await asyncio.sleep(0)
        return task
    except Exception as exception:
        logging.exception(exception)


async def main_loop(argparser, stdscr) -> None:
    """Main TUI loop."""
    sel: int = 0
    current_row: int = 0
    current_visi: int = 0
    vm_name_ordered_list: typing.List[str] = []
    task_list: typing.List[asyncio.Task] = []

    sigint_handler = functools.partial(sig_handler_sigint, stdscr=stdscr)
    signal.signal(signal.SIGINT, sigint_handler)
    config_data = read_config(argparser.args.config)
    arp_table = get_arp_table()
    init_color_pairs(config_data)
    while True:
        stdscr.clear()
        virt_data = VirtData()
        for hv_host in argparser.args.uri:
            auth = [
                [libvirt.VIR_CRED_AUTHNAME, libvirt.VIR_CRED_PASSPHRASE],
                request_cred,
                None,
            ]
            conn = libvirt.openAuth(hv_host, auth, 0)
            hosts = conn.listAllDomains()
            if len(hosts) > 0:
                virt_data.pools = conn.listAllStoragePools()
                fill_virt_data_uri(
                    conn, hosts, virt_data, arp_table, argparser.args.active
                )
            else:
                print("no active VMs found.")
                time.sleep(3)
                continue

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
        stdscr.attron(curses.color_pair(4))
        stdscr.box()
        stdscr.attroff(curses.color_pair(4))
        stdscr.addstr(1, 1, lines[0], curses.color_pair(1))

        max_rows, _ = stdscr.getmaxyx()

        win_min_row, win_max_row = get_visible_rows(max_rows - 3, sel)

        current_row = 0
        current_visi = 0
        active_ids: typing.List[int] = []
        for count, (line, vm_id, vm_name) in enumerate(
            zip(lines[1:], virt_data.vm_id, virt_data.name)
        ):
            if int(vm_id) >= 0:
                vm_name_ordered_list.append(vm_name)
                active_ids.append(count)
                current_row += 1
                if current_row > win_max_row or current_row <= win_min_row:
                    continue
                if sel == current_row - 1:
                    stdscr.addstr(current_visi + 2, 1, line, curses.color_pair(5))
                else:
                    stdscr.addstr(current_visi + 2, 1, line, curses.color_pair(2))
                current_visi += 1

        inactive_count: int = len(active_ids)
        if not argparser.args.active:
            for count, (line, vm_name) in enumerate(zip(lines[1:], virt_data.name)):
                if count not in active_ids:
                    vm_name_ordered_list.append(vm_name)
                    inactive_count += 1
                    current_row += 1
                    if current_row >= win_max_row or current_row < win_min_row:
                        continue
                    if sel == current_row - 1:
                        stdscr.addstr(current_visi + 2, 1, line, curses.color_pair(5))
                    else:
                        stdscr.addstr(current_visi + 2, 1, line, curses.color_pair(3))
                    current_visi += 1

        char = stdscr.getch()
        if char == ord("j") or char == curses.KEY_DOWN:
            sel = (sel + 1) % (len(lines) - 1)
        elif char == ord("k") or char == curses.KEY_UP:
            sel = (sel - 1) % (len(lines) - 1)
        elif char == ord("g"):
            sel = 0
        elif char == ord("G"):
            sel = len(lines) - 2
        elif char == ord("q"):
            break
        elif char == ord("d"):
            dom = conn.lookupByName(vm_name_ordered_list[sel])
            logging.debug("shutting down domain %s", vm_name_ordered_list[sel])
            task_list.append(await destroy_domain(dom))
        elif char == ord("s"):
            dom = conn.lookupByName(vm_name_ordered_list[sel])
            logging.debug("shutting down domain %s", vm_name_ordered_list[sel])
            task_list.append(await shutdown_domain(dom))
        elif char == ord("r"):
            dom = conn.lookupByName(vm_name_ordered_list[sel])
            logging.debug("shutting down domain %s", vm_name_ordered_list[sel])
            task_list.append(await start_domain(dom))
        else:
            pass

        stdscr.refresh()
        vm_name_ordered_list = []


def main() -> None:
    """Entry point."""
    try:
        argparser = Argparser()
        stdscr = curses_init()
        logging.basicConfig(
            filename=os.path.expanduser(argparser.args.logfile),
            filemode="w",
            format="%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
            level=logging.DEBUG,
        )
        asyncio.run(main_loop(argparser, stdscr))
    except Exception as exception:
        logging.exception(exception)
        do_cleanup(stdscr)


if __name__ == "__main__":
    main()
