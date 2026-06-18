import paho.mqtt.client as paho
import tkinter as tk
from tkinter import ttk
import configparser
import threading
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

class App(tk.Tk):
    def __init__(self, config_path, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config_path = config_path

        self.title("Power Buttons")
        self.protocol("WM_DELETE_WINDOW", self._on_closing)
        # self.iconbitmap(os.path.join(os.path.dirname(__file__), "Assets", "icon.ico"))

        self.config = configparser.ConfigParser()
        self.config.read(config_path)

        self.omada_client = Omada(config_path)
        self.omada_client.login()

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
            friendlyname = send_raw_tasmota_http(ip, self.config["tasmota"]["password"], "DeviceName")["DeviceName"]
            self.devices[friendlyname] = {
                field: None
                for field in TASMOTA_TRANSFORMATIONS.values()
            }
            type_ = "Tasmota HTTP"
            self.device_widgets[friendlyname] = DeviceButtonWidget(self, friendlyname, type_)
            self.device_widgets[friendlyname].pack(fill = tk.BOTH, expand = True)
            self.device_types[friendlyname] = type_
            self.tasmota_ips[friendlyname] = ip

        for profile in self.config["omada"]["profiles"].split(","):
            self.devices[profile] = omada_query_to_fields(query_omada_profile(self.omada_client, profile))
            type_ = "Omada HTTP"
            self.device_widgets[profile] = DeviceButtonWidget(self, profile, type_)
            self.device_widgets[profile].pack(fill = tk.BOTH, expand = True)
            self.device_types[profile] = type_

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

        for profile in self.config["omada"]["profiles"].split(","):
            self.devices[profile] = omada_query_to_fields(query_omada_profile(self.omada_client, profile))
            self.device_widgets[profile].update()
            time.sleep(0.1)

        self.after(30 * 1000, self._after)        

    def _on_connect_cb(self, mqtt, userdata, flags, rc):
        print("Connected to broker")
        self.mqttc.subscribe(self.config["zigbee"]["mqtt_topic"])

    def _on_message_cb(self, mqtt, userdata, msg):
        print('Topic: {0} | Message: {1}'.format(msg.topic, msg.payload))

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
        self.omada_client.logout()
        print("Omada disconnected")
        self.mqttc.disconnect()
        print("MQTT client disconnected")
        self.destroy()

class DeviceButtonWidget(tk.Frame):
    def __init__(self, parent:App, devicename, method):
        tk.Frame.__init__(self, parent)
        self.devicename = devicename
        self.method = method
        self.parent = parent
        
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

def send_raw_tasmota_http(host, password, command):
    req = requests.get("http://%s/cm" % host, params = {
        "cmnd": str(command),
        "user": "admin",
        "password": password
    })
    return req.json()

def query_omada_profile(omada, profile_name):
    profileId = omada.getProfileId(profile_name)
    print(profile_name, profileId)
    settings = omada.getProfileSettings(profileId)
    return settings

def omada_query_to_fields(profile_query_results):
    return {"on": bool(profile_query_results["poe"]), "power": None, "current": None, "voltage": None}

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
            
            exit()

    print("Couldn't find a config file :c")
