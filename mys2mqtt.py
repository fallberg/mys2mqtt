import paho.mqtt.client as mqtt
import mys2mqtt.constants as c
import time
import sys
import os
import json

class mys2mqtt:
    "This is a mysensors/mqtt helper class"
    def __init__(self, broker = "localhost", client_id = "", username = None, password = None, node_id = 255, sketch_name = "", sketch_version = "", mqtt_root_incoming = "mys-out", mqtt_root_outgoing = "mys-in"):
        self.mqtt = mqtt.Client(client_id)
        self.mqtt.username_pw_set(username, password)
        self.mqtt.on_connect = self.__on_connect
        self.mqtt.on_disconnect = self.__on_disconnect
        self.mqtt.on_message = self.__on_message
        self.mqtt.connected_flag = False
        self.mqtt.bad_connection_flag = False
        self.root_topic_in = mqtt_root_incoming
        self.root_topic_out = mqtt_root_outgoing
        self.broker = broker
        self.sketch_name = sketch_name
        self.sketch_version = sketch_version
        self.node_id = node_id
        self.sensor_list = []
        self.imperial_format = False
        self.has_config = False

        # Attempt to load existing configuration (node-id)
        try:
            with open('mys2mqtt.config.json', 'r') as f:
                config = json.load(f)
                self.node_id = config['node-id']
                print("Loaded node ID: {}".format(self.node_id))
        except:
            print("No config found, creating an empty one")
            config = {  
                'node-id': 255 # 255 indicate that we have no ID, we will request one and store the one received
            }
            with open('mys2mqtt.config.json', 'w') as outfile:
                json.dump(config, outfile)

        self.has_node_id = False if self.node_id == 255 else True

    def __mys2topic_in(self, sensor_id, command, type, ack = False):
        # TODO: Figure out a more intelligent way of handling manufacturing inbound and outgoing topics. These two are very similar.
        return "{}/{}/{}/{}/{}/{}".format(self.root_topic_in, self.node_id, sensor_id, command, 1 if ack else 0, type)

    def __mys2topic_out(self, sensor_id, command, type, ack = False):
        return "{}/{}/{}/{}/{}/{}".format(self.root_topic_out, self.node_id, sensor_id, command, 1 if ack else 0, type)

    def __register_subscription_callbacks(self):
        self.mqtt.message_callback_add(self.__mys2topic_in('+', c.C_INTERNAL, c.I_REBOOT), self.__handle_reboot)
        self.mqtt.message_callback_add(self.__mys2topic_in('+', c.C_INTERNAL, c.I_CONFIG), self.__handle_config)

    def __handle_id_response(self, client, userdata, message):
        config = {
            'node-id': int(message.payload, 10)
        }
        # TODO: Handle merging of existing configuration
        with open('mys2mqtt.config.json', 'w') as outfile:
            json.dump(config, outfile)
        self.node_id = config['node-id']
        print("Received a new node ID: {}".format(self.node_id))
        self.has_node_id = True

    def __handle_reboot(self, client, userdata, message):
        # TODO: How to handle non-linux hosts, and are there better ways than 'sudo'?
        os.system('sudo reboot')

    def __handle_config(self, client, userdata, message):
        self.has_config = True
        self.imperial_format = True if (message.payload == b'I') else False

    def __on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            print("Connected to broker")
            self.mqtt.connected_flag = True

        else:
            self.mqtt.bad_connection_flag = True
            if rc == 1:
                print("Connection refused – incorrect protocol version")
            elif rc == 2:
                print("Connection refused – invalid client identifier")
            elif rc == 3:
                print("Connection refused – server unavailable")
            elif rc == 4:
                print("Connection refused – bad username or password")
            elif rc == 5:
                print("Connection refused – not authorised")
            else:
                print("Connection failed - unknown reason")

    def __on_disconnect(self, client, userdata, rc):
        print("Disconnected from broker, reason "+str(rc))
        self.mqtt.connected_flag = False

    def __on_message(self, client, userdata, message):
        print("Received message without handler '" + str(message.payload) + "' on topic '"+ message.topic + "' with QoS " + str(message.qos))

    def connect(self):
        "Connects to the broker and starts processing callbacks"
        try:
            self.mqtt.connect(self.broker)
            self.mqtt.loop_start()
        except:
            print("Exception caught trying to connect to broker")

        while not self.mqtt.connected_flag and not self.mqtt.bad_connection_flag:
            time.sleep(1)
        if self.mqtt.bad_connection_flag:
            self.mqtt.loop_stop()
            sys.exit()

        # Request a node ID from the controller if not known
        if not self.has_node_id:
            self.mqtt.message_callback_add(self.__mys2topic_in(0, c.C_INTERNAL, c.I_ID_RESPONSE), self.__handle_id_response)
            self.mqtt.subscribe("{}/255/#".format(self.root_topic_in)) # Subscribe to broadcasts until we have a node ID
            self.mqtt.publish(self.__mys2topic_out(0, c.C_INTERNAL, c.I_ID_REQUEST))
            # Wait for us to get the node ID
            while not self.has_node_id:
                time.sleep(3)
            self.mqtt.unsubscribe("{}/255/#".format(self.root_topic_in)) # Stop subscribing to broadcasts as we have a node ID

        # Register callbacks to commands we can handle sine we know our ID
        self.__register_subscription_callbacks()

        # Subscribe to controller output targetting us
        self.mqtt.subscribe("{}/{}/#".format(self.root_topic_in, self.node_id))

        # Provide information about this node
        self.mqtt.publish(self.__mys2topic_out(0, c.C_INTERNAL, c.I_SKETCH_NAME), self.sketch_name)
        self.mqtt.publish(self.__mys2topic_out(0, c.C_INTERNAL, c.I_SKETCH_VERSION), self.sketch_version)

        # Present all registered nodes/children
        for i in self.sensor_list:
            self.mqtt.publish(self.__mys2topic_out(i[0], c.C_PRESENTATION, i[1]))

        # Send our battery level. Since we run python, it is pretty safe to assume battery is at maximum
        self.mqtt.publish(self.__mys2topic_out(0, c.C_INTERNAL, c.I_BATTERY_LEVEL), "100")

    def register_sensor(self, sensor_id, sensor):
        "Register a sensor which will be presented when broker is connected. sensor is a tuple with an S-type and a V-type."
        self.sensor_list.append((sensor_id, sensor[0], sensor[1]))

    def get_metric(self):
        "Returns true if controller is set to metric units, false if controller is set to imperial units"
        self.has_config = False

        # Send request for configuration (metric/imperial units)
        self.mqtt.publish(self.__mys2topic_out(0, c.C_INTERNAL, c.I_CONFIG))

        # Wait for a reply with the configuration
        while not self.has_config:
            time.sleep(1)

        return False if self.imperial_format else True    

    def send_debug(self, sensor_id, data):
        "Send a debug message to the controller"
        self.mqtt.publish(self.__mys2topic_out(sensor_id, c.C_INTERNAL, c.I_LOG_MESSAGE), data)

    def send_float(self, sensor_id, value):
        "Send data for provided sensor ID in float form"
        self.mqtt.publish(self.__mys2topic_out(sensor_id, c.C_SET, self.sensor_list[sensor_id][2]), '%f' % value)

    def send_int(self, sensor_id, value):
        "Send data for provided sensor ID in integer form"
        self.mqtt.publish(self.__mys2topic_out(sensor_id, c.C_SET, self.sensor_list[sensor_id][2]), '%d' % value)
