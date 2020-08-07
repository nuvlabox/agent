#!/usr/bin/env python
# -*- coding: utf-8 -*-

""" NuvlaBox Telemetry

It takes care of updating the NuvlaBox status
resource in Nuvla.
"""

import datetime
import docker
import logging
import socket
import json
import os
import psutil
import requests
import paho.mqtt.client as mqtt

from agent.common import NuvlaBoxCommon
from os import path, stat
from subprocess import run, PIPE, STDOUT
from pydoc import locate


class Telemetry(NuvlaBoxCommon.NuvlaBoxCommon):
    """ The Telemetry class, which includes all methods and
    properties necessary to categorize a NuvlaBox and send all
    data into the respective NuvlaBox status at Nuvla

    Attributes:
        data_volume: path to shared NuvlaBox data
    """

    def __init__(self, data_volume, nuvlabox_status_id):
        """ Constructs an Telemetry object, with a status placeholder """

        # self.data_volume = data_volume
        # self.vpn_folder = "{}/vpn".format(data_volume)
        super().__init__(shared_data_volume=data_volume)

        # self.api = nb.ss_api() if not api else api
        self.nb_status_id = nuvlabox_status_id
        self.docker_client = docker.from_env()
        self.status = {'resources': None,
                       'status': None,
                       'nuvlabox-api-endpoint': None,
                       'operating-system': None,
                       'architecture': None,
                       'ip': None,
                       'last-boot': None,
                       'hostname': None,
                       'docker-server-version': None,
                       'gpio-pins': None
                       }

        self.mqtt_telemetry = mqtt.Client()

    def send_mqtt(self, cpu=None, ram=None, disks=None):
        """ Gets the telemetry data and send the stats into the MQTT broker

        :param cpu: tuple (capacity, load)
        :param ram: tuple (capacity, used)
        :param disk: list of {device: partition_name, capacity: value, used: value}
        """

        try:
            self.mqtt_telemetry.connect(self.mqtt_broker_host, self.mqtt_broker_port, self.mqtt_broker_keep_alive)
        except ConnectionRefusedError:
            logging.exception("Connection to NuvlaBox MQTT broker refused")
            self.mqtt_telemetry.disconnect()
            return
        except socket.gaierror:
            logging.exception("The NuvlaBox MQTT broker is not reachable...trying again later")
            self.mqtt_telemetry.disconnect()
            return

        msgs = []
        if cpu:
            # e1 = self.mqtt_telemetry.publish("cpu/capacity", payload=str(cpu[0]))
            # e2 = self.mqtt_telemetry.publish("cpu/load", payload=str(cpu[1]))
            # ISSUE: for some reason, the connection is lost after publishing with paho-mqtt

            # using os.system for now

            os.system("mosquitto_pub -h {} -t {} -m '{}'".format(self.mqtt_broker_host,
                                                                 "cpu",
                                                                 json.dumps(cpu)))

        if ram:
            # self.mqtt_telemetry.publish("ram/capacity", payload=str(ram[0]))
            # self.mqtt_telemetry.publish("ram/used", payload=str(ram[1]))
            # same issue as above
            os.system("mosquitto_pub -h {} -t {} -m '{}'".format(self.mqtt_broker_host,
                                                                 "ram",
                                                                 json.dumps(ram)))

        if disks:
            for dsk in disks:
                # self.mqtt_telemetry.publish("disks", payload=json.dumps(dsk))
                # same issue as above
                os.system("mosquitto_pub -h {} -t {} -m '{}'".format(self.mqtt_broker_host,
                                                                     "disks",
                                                                     json.dumps(dsk)))

        # self.mqtt_telemetry.disconnect()

    def get_status(self):
        """ Gets several types of information to populate the NuvlaBox status """

        # get status for Nuvla
        disk_usage = self.get_disks_usage()
        operational_status = self.get_operational_status()
        docker_info = self.get_docker_info()

        cpu_sample = {
            "capacity": int(psutil.cpu_count()),
            "load": float(psutil.getloadavg()[2])
        }

        ram_sample = {
            "capacity": int(round(psutil.virtual_memory()[0]/1024/1024)),
            "used": int(round(psutil.virtual_memory()[3]/1024/1024))
        }

        self.send_mqtt(cpu_sample, ram_sample, disk_usage)

        cpu = {"topic": "cpu", "raw-sample": json.dumps(cpu_sample)}
        cpu.update(cpu_sample)

        ram = {"topic": "ram", "raw-sample": json.dumps(ram_sample)}
        ram.update(ram_sample)

        disks = []
        for dsk in disk_usage:
            dsk.update({"topic": "disks", "raw-sample": json.dumps(dsk)})
            disks.append(dsk)

        status_for_nuvla = {
            'resources': {
                'cpu': cpu,
                'ram': ram,
                'disks': disks
            },
            'operating-system': docker_info["OperatingSystem"],
            "architecture": docker_info["Architecture"],
            "hostname": docker_info["Name"],
            "ip": self.get_ip(),
            "docker-server-version": self.docker_client.version()["Version"],
            "last-boot": datetime.datetime.fromtimestamp(psutil.boot_time()).strftime("%Y-%m-%dT%H:%M:%SZ"),
            'status': operational_status,
            "nuvlabox-api-endpoint": self.get_nuvlabox_api_endpoint()
        }

        net_stats = self.get_network_info()
        if net_stats:
            status_for_nuvla['resources']['net-stats'] = net_stats

        if self.gpio_utility:
            # Get GPIO pins status
            gpio_pins = self.get_gpio_pins()

            if gpio_pins:
                status_for_nuvla['gpio-pins'] = gpio_pins

        # get all status for internal monitoring
        all_status = status_for_nuvla.copy()
        all_status.update({
            "cpu-usage": psutil.cpu_percent(),
            "cpu-load": cpu_sample['load'],
            "disk-usage": psutil.disk_usage("/")[3],
            "memory-usage": psutil.virtual_memory()[2],
            "cpus": cpu_sample['capacity'],
            "memory": ram_sample['capacity'],
            "disk": int(psutil.disk_usage('/')[0]/1024/1024/1024)
        })

        return status_for_nuvla, all_status

    @staticmethod
    def parse_gpio_pin_cell(indexes, line):
        """ Parses one cell of the output from gpio readall, which has 2 pins

        :param indexes: the index numbers for the values of BCM, Name, Mode, V and Physical (in this order)

        :returns a GPIO dict obj with the parsed pin"""

        # the expected list of attributes is
        expected = [{"position": None, "attribute": "BCM", "type": "int"},
                    {"position": None, "attribute": "NAME", "type": "str"},
                    {"position": None, "attribute": "MODE", "type": "str"},
                    {"position": None, "attribute": "VOLTAGE", "type": "int"}]

        needed_indexes_len = 5

        if len(indexes) < needed_indexes_len:
            logging.error(f"Missing indexes needed to parse GPIO pin: {indexes}. Need {needed_indexes_len}")
            return None

        gpio_values = line.split('|')
        gpio_pin = {}
        try:
            gpio_pin['pin'] = int(gpio_values[indexes[-1]])
            # if we can get the physical pin, we can move on. Pin is the only mandatory attr

            for i, exp in enumerate(expected):
                expected[i]["position"] = indexes[i]

                try:
                    value = locate(exp["type"])
                    gpio_pin[exp["attribute"].lower()] = value(gpio_values[exp["position"]])
                except ValueError:
                    logging.debug(f"No suitable {exp['attribute']} value for pin {gpio_pin['pin']}")
                    continue

            return gpio_pin
        except ValueError:
            logging.warning(f"Unable to get GPIO pin status on {gpio_values}, index {indexes[-1]}")
            return None
        except:
            # if there's any other issue while doing so, it means the provided argument is not valid
            logging.exception(f"Invalid list of indexes {indexes} for GPIO pin in {line}. Cannot parse this pin")
            return None

    def get_gpio_pins(self):
        """ Uses the GPIO utility to scan and get the current status of all GPIO pins in the device.
        It then parses the output and gives back a list of pins

        :returns list of JSONs, i.e. [{pin: 1, name: GPIO. 1, bcm: 4, mode: IN}, {pin: 7, voltage: 0, mode: ALT1}]"""

        command = ["gpio", "readall"]
        gpio_out = run(command, stdout=PIPE, stderr=STDOUT, encoding='UTF-8')

        if gpio_out.returncode != 0 or not gpio_out.stdout:
            return None

        trimmed_gpio_out = gpio_out.stdout.splitlines()[3:-3]

        formatted_gpio_status = []
        for gpio_line in trimmed_gpio_out:

            # each line has two columns = 2 pins

            first_pin_indexes = [1, 3, 4, 5, 6]
            second_pin_indexes = [14, 11, 10, 9, 8]
            first_pin = self.parse_gpio_pin_cell(first_pin_indexes, gpio_line)
            if first_pin:
                formatted_gpio_status.append(first_pin)

            second_pin = self.parse_gpio_pin_cell(second_pin_indexes, gpio_line)
            if second_pin:
                formatted_gpio_status.append(second_pin)

        return formatted_gpio_status

    def get_docker_info(self):
        """ Invokes the command docker info

        :returns JSON structure with all the Docker informations
        """

        return self.docker_client.info()

    def get_network_info(self):
        """ Gets the list of net ifaces and corresponding rxbytes and txbytes

        :returns {"iface1": {"rx_bytes": X, "tx_bytes": Y}, "iface2": ...}
        """

        sysfs_net = "{}/sys/class/net".format(self.hostfs)

        try:
            ifaces = os.listdir(sysfs_net)
        except FileNotFoundError:
            logging.warning("Cannot find network information for this device")
            return {}

        net_stats = []
        for interface in ifaces:
            stats = "{}/{}/statistics".format(sysfs_net, interface)
            try:
                with open("{}/rx_bytes".format(stats)) as rx:
                    rx_bytes = int(rx.read())
                with open("{}/tx_bytes".format(stats)) as tx:
                    tx_bytes = int(tx.read())
            except FileNotFoundError:
                logging.warning("Cannot calculate net usage for interface {}".format(interface))
                continue

            net_stats.append({
                "interface": interface,
                "bytes-transmitted": tx_bytes,
                "bytes-received": rx_bytes
            })

        return net_stats

    @staticmethod
    def get_disks_usage():
        """ Gets disk usage for N partitions """

        return [{'device': 'overlay',
                 'capacity': int(psutil.disk_usage('/')[0]/1024/1024/1024),
                 'used': int(psutil.disk_usage('/')[1]/1024/1024/1024)
                 }]

    def diff(self, old_status, new_status):
        """ Compares the previous status with the new one and discover the minimal changes """

        minimal_update = {}
        delete_attributes = []
        for key in self.status.keys():
            if new_status[key] is None:
                delete_attributes.append(key)
                continue
            if old_status[key] != new_status[key]:
                minimal_update[key] = new_status[key]
        return minimal_update, delete_attributes

    def update_status(self):
        """ Runs a cycle of the categorization, to update the NuvlaBox status """

        new_status, all_status = self.get_status()
        updated_status, delete_attributes = self.diff(self.status, new_status)
        updated_status['current-time'] = datetime.datetime.utcnow().isoformat().split('.')[0] + 'Z'
        updated_status['id'] = self.nb_status_id
        logging.info('Refresh status: %s' % updated_status)
        self.api()._cimi_put(self.nb_status_id,
                             json=updated_status)  # should also include ", select=delete_attributes)" but CIMI does not allow
        self.status = new_status

        # write all status into the shared volume for the other components to re-use if necessary
        with open(self.nuvlabox_status_file, 'w') as nbsf:
            nbsf.write(json.dumps(all_status))

    def update_operational_status(self, status="RUNNING", status_log=None):
        """ Update the NuvlaBox status with the current operational status

        :param status: status, according to the allowed set defined in the api server nuvlabox-status schema
        :param status_log: reason for the specified status
        :return:
        """

        new_operational_status = {'status': status}
        if status_log:
            new_operational_status["status-log"] = status_log

        self.api()._cimi_put(self.nb_status_id, json=new_operational_status)

        self.set_local_operational_status(status)

    def get_nuvlabox_api_endpoint(self):
        """ Double checks that the NuvlaBox API is online

        :returns URL for the NuvlaBox API endpoint
        """

        nb_ext_endpoint = "https://{}:5001/api".format(self.get_ip())
        nb_int_endpoint = "https://management-api:5001/api"

        try:
            requests.get(nb_int_endpoint, verify=False)
        except requests.exceptions.SSLError:
            # the API endpoint exists, we simply did not authenticate
            return nb_ext_endpoint
        except requests.exceptions.ConnectionError:
            return None
        except:
            # let's assume it doesn't exist either
            return None

        return nb_int_endpoint

    def get_ip(self):
        """ Discovers the NuvlaBox IP (aka endpoint) """

        # NOTE: This code does not work on Ubuntu 18.04.
        # with open("/proc/self/cgroup", 'r') as f:
        #    docker_id = f.readlines()[0].replace('\n', '').split("/")[-1]

        # Docker sets the hostname to be the short version of the container id.
        # This method of getting the container id works on both Ubuntu 16 and 18.
        docker_id = socket.gethostname()

        deployment_scenario = self.docker_client.containers.get(docker_id).labels["nuvlabox.deployment"]

        if deployment_scenario == "localhost":
            # Get the Docker IP within the shared Docker network

            # ip = self.docker_client.info()["Swarm"]["NodeAddr"]

            ip = socket.gethostbyname(socket.gethostname())
        elif deployment_scenario == "onpremise":
            # Get the local network IP
            # Hint: look at the local Nuvla IP, and scan the host network interfaces for an IP within the same subnet
            # You might need to launch a new container from here, in host mode, just to run `ifconfig`, something like:
            #       docker run --rm --net host alpine ip addr

            # FIXME: Review whether this is the correct impl. for this case.
            ip = self.docker_client.info()["Swarm"]["NodeAddr"]
        elif deployment_scenario == "production":
            # Get either the public IP (via an online service) or use the VPN IP

            if path.exists(self.vpn_ip_file) and stat(self.vpn_ip_file).st_size != 0:
                ip = str(open(self.vpn_ip_file).read().splitlines()[0])
            else:
                ip = self.docker_client.info()["Swarm"]["NodeAddr"]
        else:
            logging.warning("Cannot infer the NuvlaBox IP!")
            return None

        return ip
