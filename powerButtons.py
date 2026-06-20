import paho.mqtt.client as paho
import tkinter.messagebox as msgbox
import tkinter as tk
from tkinter import ttk
import configparser
import datetime
import threading
import platform
import requests
import time
import json
import sys
import os

sys.path.insert(1, os.path.join(os.path.dirname(__file__), "omada-api"))
from omada import Omada

ZIGBEE_TRANSFORMATIONS = {
    "Power": "on",
    "ActivePower": "power",
    "RMSVoltage": "voltage",
    "RMSCurrent": "current",
    "RMSPower": "power"
}

TASMOTA_TRANSFORMATIONS = {
    "Power": "power",
    "Voltage": "voltage",
    "Current": "current",
    "POWER": "on"
}

OMADA_MQTT_TRANSFORMATIONS = {
    "tpPoePortStatus": "on",
    "tpPoePower": "power",
    "tpPoeCurrent": "current",
    "tpPoeVoltage": "voltage"
}

class App(tk.Tk):

    last_snmp_mqtt = None
    last_zigbee_mqtt = None

    def __init__(self, config_path, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config_path = config_path

        print("moshi moshi")

        self.title("Power Buttons v3.0")
        self.protocol("WM_DELETE_WINDOW", self._on_closing)
        if platform.system() == "Windows":
            self.iconbitmap(os.path.join(os.path.dirname(__file__), "Assets", "icon.ico"))
        else:
            self.img_icon = tk.PhotoImage(file = os.path.join(os.path.dirname(__file__), "Assets", "icon.png"))
            self.iconphoto(False, self.img_icon)

        self.config = configparser.RawConfigParser()
        self.config.optionxform = str
        self.config.read(config_path)

        self.mqttc = paho.Client("PowerButtonsBUI", clean_session = True)

        self.mqttc.on_connect = self._on_connect_cb
        self.mqttc.on_message = self._on_message_cb

        self.mqttc.username_pw_set(self.config["zigbee"]["mqtt_username"], password = self.config["zigbee"]["mqtt_password"])
        self.mqttc.connect(self.config["zigbee"]["mqtt_host"], 1883, 60)

        self.img_green = tk.PhotoImage(file = os.path.join(os.path.dirname(__file__), "Assets", "green.png"))
        self.img_yellow = tk.PhotoImage(file = os.path.join(os.path.dirname(__file__), "Assets", "yellow.png"))
        self.img_red = tk.PhotoImage(file = os.path.join(os.path.dirname(__file__), "Assets", "red.png"))

        self.devices = {}
        self.device_widgets = {}
        self.device_types = {}
        self.tasmota_ips = {}

        for friendlyname in self.config["zigbee"]["friendlynames"].split(","):
            self.devices[friendlyname] = {
                field: None
                for field in ZIGBEE_TRANSFORMATIONS.values()
            }
            type_ = "Zigbee MQTT"
            self.device_widgets[friendlyname] = DeviceButtonWidget(self, friendlyname, type_)
            self.device_widgets[friendlyname].pack(fill = tk.BOTH, expand = True)
            self.device_types[friendlyname] = type_

        for ip in self.config["tasmota"]["plugs"].split(","):
            try:
                friendlyname = send_raw_tasmota_http(ip, self.config["tasmota"]["password"], "DeviceName")["DeviceName"]
            except requests.exceptions.ConnectionError:
                print("Couldnt find %s" % ip)
                continue

            self.devices[friendlyname] = {
                field: None
                for field in TASMOTA_TRANSFORMATIONS.values()
            }
            type_ = "Tasmota HTTP"
            self.device_widgets[friendlyname] = DeviceButtonWidget(self, friendlyname, type_)
            self.device_widgets[friendlyname].pack(fill = tk.BOTH, expand = True)
            self.device_types[friendlyname] = type_
            self.tasmota_ips[friendlyname] = ip

        self.omada_ports = {}
        self.omada_cache = {}
        for profile in self.config["omada_profile_ports"].keys():
            self.devices[profile] = {
                field: None
                for field in OMADA_MQTT_TRANSFORMATIONS.values()
            }
            type_ = "Omada SNMP"
            self.device_widgets[profile] = DeviceButtonWidget(self, profile, type_)
            self.device_widgets[profile].pack(fill = tk.BOTH, expand = True)
            self.device_types[profile] = type_
            for v in self.config["omada_profile_ports"][profile].split(","):
                self.omada_ports[tuple(v.split(":"))] = profile
            self.omada_cache[profile] = {}

        ttk.Separator(self, orient = tk.HORIZONTAL).pack(expand = True, fill = tk.BOTH)
        self.bottom_widget = BottomWidget(self)
        self.bottom_widget.pack(expand = True, fill = tk.BOTH)

        self.after(30 * 1000, self._after)

        self.mqtt_thread = threading.Thread(target = self._mqtt_thread_func)
        self.mqtt_thread.start()

    def _after(self):
        for ip in self.config["tasmota"]["plugs"].split(","):
            data = query_tasmota_power(ip, self.config["tasmota"]["password"])
            fields = tasmota_query_to_fields(data)
            friendlyname = list(self.tasmota_ips.keys())[list(self.tasmota_ips.values()).index(ip)]
            self.devices[friendlyname] = fields
            self.device_widgets[friendlyname].update()

        for profile, ports in self.omada_cache.items():
            self.devices[profile] = {
                "power": sum([v["power"] for v in ports.values()]),
                "current": sum([v["current"] for v in ports.values()]),
                "voltage": None,
                "num_on": 0,
                "on": None
            }
            for on_device in [v["on"] for v in ports.values()]:
                if on_device:
                    self.devices[profile]["num_on"] += 1
            if len(ports.values()) > 0:
                self.devices[profile]["voltage"] = sum([v["voltage"] for v in ports.values()]) / len(ports.values())
            if self.devices[profile]["num_on"] > 0:
                self.devices[profile]["on"] = True
            else:
                self.devices[profile]["on"] = False
            
            # print(profile, self.devices[profile])
            self.device_widgets[profile].update()

        self.bottom_widget.update_in_sec = 30
        self.after(30 * 1000, self._after)        

    def _on_connect_cb(self, mqtt, userdata, flags, rc):
        print("Connected to broker")
        self.mqttc.subscribe(self.config["zigbee"]["tasmota_topic"])
        self.mqttc.subscribe(self.config["zigbee"]["snmp_topic"])

    def _on_message_cb(self, mqtt, userdata, msg):
        print('Topic: {0} | Message: {1}'.format(msg.topic, msg.payload.decode()))

        if "SwitchSNMP" in msg.topic:
            self.last_snmp_mqtt = datetime.datetime.now()
            # handle POE SNMP shit
            topic_s = msg.topic.split("/")
            switch_host = topic_s[2]
            switch_port = topic_s[3]
            msg_j = json.loads(msg.payload.decode())

            if (switch_host, switch_port) in self.omada_ports.keys():
                profile = self.omada_ports[(switch_host, switch_port)]

                self.omada_cache[profile][(switch_host, switch_port)] = switch_mqtt_to_fields(msg_j)
        else:
            self.last_zigbee_mqtt = datetime.datetime.now()
            # handle tasmota plug shit
            msg_j = json.loads(msg.payload.decode())
            msg_j = msg_j["ZbReceived"][list(msg_j["ZbReceived"].keys())[0]]
            if msg_j["Name"] in self.devices.keys():
                for k, v in msg_j.items():
                    if k in ZIGBEE_TRANSFORMATIONS.keys():
                        self.devices[msg_j["Name"]][ZIGBEE_TRANSFORMATIONS[k]] = v
                
                # print(self.devices)
                self.device_widgets[msg_j["Name"]].update()

    def _mqtt_thread_func(self):
        self.mqttc.loop_forever()
    
    def _on_closing(self):
        # self.omada_client.logout()
        # print("Omada disconnected")
        self.mqttc.disconnect()
        print("MQTT client disconnected")
        self.destroy()

class BottomWidget(tk.Frame):
    
    update_in_sec = 29

    def __init__(self, parent:App):
        tk.Frame.__init__(self, parent)
        self.parent = parent

        self.lbl_update_in = ttk.Label(self, text = "Refresh: 30s")
        self.lbl_update_in.pack(side = tk.LEFT, padx = 5, pady = 5)

        self.lbl_last_zigbee = ttk.Label(self, text = "Zigbee MQTT: Never")
        self.lbl_last_zigbee.pack(side = tk.RIGHT, padx = 5, pady = 5)

        self.lbl_last_snmp = ttk.Label(self, text = "SNMP MQTT: Never")
        self.lbl_last_snmp.pack(side = tk.RIGHT, padx = 5, pady = 5)

        self.after(1000, self._after)

    def _after(self):
        self.lbl_update_in.configure(text = "Refresh: %is" % self.update_in_sec)
        self.update_in_sec -= 1

        if self.parent.last_snmp_mqtt is not None:
            self.lbl_last_snmp.configure(text = "SNMP MQTT: %is" % (datetime.datetime.now() - self.parent.last_snmp_mqtt).seconds)

        if self.parent.last_zigbee_mqtt is not None:
            self.lbl_last_zigbee.configure(text = "Zigbee MQTT: %is" % (datetime.datetime.now() - self.parent.last_zigbee_mqtt).seconds)

        self.after(1000, self._after)

class DeviceButtonWidget(tk.Frame):
    def __init__(self, parent:App, devicename, method):
        tk.Frame.__init__(self, parent)
        self.devicename = devicename
        self.method = method
        self.parent = parent
        
        if method == "Omada SNMP":
            self.btn_img = ttk.Label(self, image = self.parent.img_yellow, text = "?", compound = tk.LEFT)
        else:
            self.btn_img = ttk.Label(self, image = self.parent.img_yellow)
            
        self.btn_img.pack(side = tk.LEFT, padx = 5, pady = 5, fill = tk.BOTH, expand = True)

        self.lbl_name = tk.Label(self, text = devicename, font = "-weight bold")
        self.lbl_name.pack(side = tk.LEFT, fill = tk.BOTH, expand = True)

        self.lbl_details = tk.Label(self, text = "?W ?A ?V")
        self.lbl_details.pack(side = tk.LEFT, padx = 5, pady = 5, fill = tk.BOTH, expand = True)

        self.lbl_method = tk.Label(self, text = method)
        self.lbl_method.pack(side = tk.RIGHT, padx = 5, pady = 5, fill = tk.BOTH, expand = True)

        self.btn_off = ttk.Button(self, text = "Off", command = lambda: self.set_power("off"))
        self.btn_off.pack(side = tk.RIGHT, padx = 5, pady = 5, fill = tk.X, expand = True)

        self.btn_on = ttk.Button(self, text = "On",  command = lambda: self.set_power("on"))
        self.btn_on.pack(side = tk.RIGHT, padx = 5, pady = 5, fill = tk.X, expand = True)

        if self.devicename in self.parent.config["tasmota"]["disabled"].split(","):
            self.btn_on.config(state = tk.DISABLED)
            self.btn_off.config(state = tk.DISABLED)

    def set_power(self, set_to):
        if set_to == "on":
            payload = "1"
        elif set_to == "off":
            payload = "0"
            
        if self.method == "Tasmota HTTP":
            send_raw_tasmota_http(self.parent.tasmota_ips[self.devicename], self.parent.config["tasmota"]["password"], "Power %s" % payload)
            self.after(200, self.update_one_http)
        elif self.method == "Zigbee MQTT":
            self.parent.mqttc.publish(
                self.parent.config["zigbee"]["cmd_mqtt_topic"],
                json.dumps({"device": self.devicename, "send": {"power": int(payload)}}),
                qos = 1
            )
        elif self.method == "Omada SNMP":
            self.omada_client = Omada(self.parent.config_path)
            self.omada_client.login()
            profileId = self.omada_client.getProfileId(self.devicename)
            settings = self.omada_client.getProfileSettings(profileId)
            settings['poe'] = payload
            self.omada_client.setProfileSettings(profileId, settings)
            self.omada_client.logout()

        msgbox.showinfo("Power Set", "Set '%s' device '%s' to '%s'" % (self.method, self.devicename, set_to))

    def update_one_http(self):
        self.parent.devices[self.devicename] = tasmota_query_to_fields(query_tasmota_power(
            self.parent.tasmota_ips[self.devicename], self.parent.config["tasmota"]["password"]
        ))
        self.update()

    def update(self):
        t = ""
        device_info = self.parent.devices[self.devicename]

        if device_info["on"]:
            self.btn_img.configure(image = self.parent.img_green)
        elif device_info["on"] == False:
            self.btn_img.configure(image = self.parent.img_red)

        if device_info["power"] is not None:
            t += "%iW " % device_info["power"]
        else:
            t += "?W "
        if device_info["current"] is not None:
            t += "%.2fA " % device_info["current"]
        else:
            t += "?A "
        if device_info["voltage"] is not None:
            t += "%iV" % device_info["voltage"]
        else:
            t += "?V"

        if "num_on" in device_info.keys():
            self.btn_img.configure(text = str(device_info["num_on"]))
        
        self.lbl_details.configure(text = t)

def tasmota_query_to_fields(query_results):
    def transform_power(p):
        if p == "ON":
            return True
        if p == "OFF":
            return False
        return p
    
    return {TASMOTA_TRANSFORMATIONS[k]: transform_power(v) for k, v in query_results.items() if k in TASMOTA_TRANSFORMATIONS.keys()}

def query_tasmota_power(host, password):
    return send_raw_tasmota_http(
        host, password, "Status 8"
    )["StatusSNS"]["ENERGY"] | send_raw_tasmota_http(
        host, password, "Power"
    )

def switch_mqtt_to_fields(mqtt_fields):
    def transform_power(k, v):
        if v == "enable(1)":
            return True
        if v == "disable(0)":
            return False
        
        if k == "tpPoeCurrent":
            return v / 1000
        else:
            return v


    return {OMADA_MQTT_TRANSFORMATIONS[k]: transform_power(k, v) for k, v in mqtt_fields.items() if k in OMADA_MQTT_TRANSFORMATIONS.keys()}

def send_raw_tasmota_http(host, password, command):
    req = requests.get("http://%s/cm" % host, params = {
        "cmnd": str(command),
        "user": "admin",
        "password": password
    })
    return req.json()

if __name__ == "__main__":
    places_to_look = [
        os.path.dirname(__file__),
        os.path.expanduser("~"),
        os.getcwd()
    ]

    for p in places_to_look:
        fp = os.path.join(p, "powerButtons.ini")
        if os.path.exists(fp):

            root = App(fp)
            root.mainloop()
            break

    print("Couldn't find a config file :c")
