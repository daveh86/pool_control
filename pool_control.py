#!/usr/bin/env python
import time
import os
import sys
import argparse
import logging

import daemon
from omegaconf import OmegaConf, DictConfig
import paho.mqtt.client as mqtt
import paho.mqtt.subscribe as subscribe
import pypowerwall
import statistics
import ast
import datetime

logger = logging.getLogger(__name__)

global state, current_draw, runtime, pool_hours, status, counter, manual

pool_hours = [0, 8, 8, 6, 6, 6, 4, 4, 4, 6, 6, 8, 8]

status = {}

counter = 0

def on_connect(client, userdata, flags, rc):
    logger.info("Connected with result code %d", rc)

def on_message(client, userdata, msg):
    logger.info(f"mqtt message: {msg.payload}")

def on_message_plug_sensor(client, userdata, msg):
    global current_draw
    payload = ast.literal_eval(msg.payload.decode('utf-8'))
    logger.info(f"mqtt message: {msg.payload}")
    logger.info(f"plug is currently using: {payload['ENERGY']['Power']}")
    current_draw = int(payload['ENERGY']['Power'])

def on_message_override(client, userdata, msg):
    global manual
    payload = msg.payload.decode('ascii')
    logger.info(f"mqtt message: {payload}")
    manual = (payload == 'MANUAL')
    logger.info(f"setting manual mode to be {manual}")

def on_message_plug_state(client, userdata, msg):
    global state
    payload = ast.literal_eval(msg.payload.decode('utf-8'))
    logger.info(f"mqtt message: {payload}")
    logger.info(f"plug is currently: {payload['POWER']}")
    state = (payload['POWER']== "ON")

def on_disconnect(client, userdata, rc):
    logger.error("Disconnected %s", rc)
    logging.info("Reconnecting...")
    time.sleep(2)
    client.reconnect()

def poll_pw(cfg: DictConfig, pw: pypowerwall.Powerwall, client: mqtt.Client) -> None:
    global state
    global runtime
    global pool_hours
    global counter
    global manual

    grid = pw.grid()
    solar = pw.solar()
    battery = pw.battery()
    home = pw.home()
    battery_level = pw.level()
    solar_excess = solar - home
    logger.info(f" grid: {grid} solar: {solar} battery: {battery} home: {home} battery_level: {battery_level} solar_excess: {solar_excess}")
    client.publish(f"{cfg.mqtt_pool_topic}/solar_free", solar_excess, retain=True) # Retain here as we want this for state tracking 

    if counter > 0:
        counter -= 1

    now = datetime.datetime.now()                  
    logger.info(f"Steering state: {state}, manual: {manual}, runtime: {runtime}, time: {now}, hour: {now.hour}")   
    if state == True: # Power is on
        runtime += cfg.powerwall_poll_s
        client.publish(f"{cfg.mqtt_pool_topic}/runtime", runtime, retain=True) # Retain here as we want this for state tracking 
        if runtime >= pool_hours[now.month] * 60 * 60:
            logger.info(f"Disabling plug as runtime is {runtime}")
            turn_plug_off(client, cfg.plug_id)
            status[str(now)] = ("OFF", f"Runtime is {runtime}")
            client.publish(f"{cfg.mqtt_pool_topic}/status", str(status), retain=True) # Retain here as we want this for state tracking
        elif solar_excess < -500 and battery_level < 50 and not (now.hour >= 22 or now.hour < 7):
            counter += 2
            if counter >= 5: # we can cope with small bursts of high load
                logger.info(f"Disabling plug as solar_excess is {solar_excess} and battery_level {battery_level}")
                turn_plug_off(client, cfg.plug_id)
                status[str(now)] = ("OFF", f"solar_excess is {solar_excess} and battery_level {battery_level}")
                client.publish(f"{cfg.mqtt_pool_topic}/status", str(status), retain=True) # Retain here as we want this for state tracking
    else: # Power is off
        if runtime < pool_hours[now.month] * 60 * 60 :
            if solar_excess > 1100 and solar > 2400: # we want more than 1100 w free and more than 2400 being generated, otherwise we will probably flip-flop
                counter = 0
                logger.info(f"Enabling plug as solar_excess is {solar_excess} and solar is {solar}")
                turn_plug_on(client, cfg.plug_id)
                status[str(now)] = ("ON", f"solar_excess is {solar_excess}")
                client.publish(f"{cfg.mqtt_pool_topic}/status", str(status), retain=True) # Retain here as we want this for state tracking
            if now.hour >= 22 or now.hour < 7:
                logger.info(f"Enabling plug as it is offpeak")
                turn_plug_on(client, cfg.plug_id)
                status[str(now)] = ("ON", f"Now offpeak")
                client.publish(f"{cfg.mqtt_pool_topic}/status", str(status), retain=True) # Retain here as we want this for state tracking
                
    # publish our new power on status with retain=True
    # Offpeak is 10PM to 7AM AEST
    # reset our days run @ 7AM
    # if after offpeak and we are less than target, run

def connect_pw(cfg: DictConfig) -> pypowerwall.Powerwall:
    return pypowerwall.Powerwall(
        cfg.powerwall_host, cfg.powerwall_password, cfg.powerwall_email, cfg.powerwall_timezone
    )

def pw_poll_loop(cfg: DictConfig) -> None:
    global runtime
    global state
    global status
    global counter
    global manual

    client = mqtt.Client()
    client.enable_logger(logger)
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    if cfg.mqtt_username and cfg.mqtt_password:
        logger.info(f"Connecting to mqtt server: {cfg.mqtt_username}@{cfg.mqtt_server}:{cfg.mqtt_server_port}")
        client.username_pw_set(cfg.mqtt_username, cfg.mqtt_password)
    else:
        logger.info(f"Connecting to mqtt server: {cfg.mqtt_server}:{cfg.mqtt_server_port}")

    # Get telemetry
    client.message_callback_add(f"tele/{cfg.plug_id}/SENSOR", on_message_plug_sensor)
    client.message_callback_add(f"tele/{cfg.plug_id}/STATE", on_message_plug_state)

    # Manual Override Hook
    client.message_callback_add(f"{cfg.mqtt_pool_topic}/override", on_message_override)

    msg = subscribe.simple(f"{cfg.mqtt_pool_topic}/override", hostname=cfg.mqtt_server, port=cfg.mqtt_server_port, auth={'username':cfg.mqtt_username, 'password': cfg.mqtt_password})
    manual = (msg.payload == b"MANUAL")
    logger.info(f"Loaded initial manual value of {manual} from {msg.payload}")

    client.connect(host=cfg.mqtt_server, port=cfg.mqtt_server_port, keepalive=cfg.mqtt_keep_alive)
    client.loop_start()
    logger.info(f"Subscribing to {cfg.plug_id}")
    client.subscribe(f"tele/{cfg.plug_id}/#", 0)
    logger.info(f"Subscribing to {cfg.mqtt_pool_topic}/override")
    client.subscribe(f"{cfg.mqtt_pool_topic}/override", 0)

    pw = connect_pw(cfg)

    # Send the switch off message as we have just reset
    # read the last known status from the broker
    state = False
    set_teleperiod(client, cfg.plug_id, cfg.powerwall_poll_s)

    msg = subscribe.simple(f"{cfg.mqtt_pool_topic}/runtime", hostname=cfg.mqtt_server, port=cfg.mqtt_server_port, auth={'username':cfg.mqtt_username, 'password': cfg.mqtt_password})
    runtime = int(msg.payload)
    logger.info(f"Loaded initial runtime value of {runtime}")

    msg = subscribe.simple(f"{cfg.mqtt_pool_topic}/status", hostname=cfg.mqtt_server, port=cfg.mqtt_server_port, auth={'username':cfg.mqtt_username, 'password': cfg.mqtt_password})
    status = ast.literal_eval(msg.payload.decode('utf-8'))
    logger.info(f"Loaded initial status value of {status}")
    # Todo: How long since last hearbeat
    # if its more than 5 minutes, we should check state before proceeding and assess below

    while True:
        time.sleep(cfg.powerwall_poll_s)
        poll_pw(cfg, pw, client)
        now = datetime.datetime.now()                  
        client.publish(f"{cfg.mqtt_pool_topic}/heartbeat", str(now), retain=True) # Retain here as we want this for state tracking
        if now.hour == 7 and now.minute == 0: # Reset at 7am daily
            counter = 0
            status = {}
            status[str(now)] = ("OFF", "Daily Reset")
            runtime = 0
            client.publish(f"{cfg.mqtt_pool_topic}/runtime", runtime, retain=True) # Retain here as we want this for state tracking
            client.publish(f"{cfg.mqtt_pool_topic}/status", str(status), retain=True) # Retain here as we want this for state tracking
            logger.info(f"Disabling plug for daily reset")
    client.loop_stop()


def turn_plug_on(client, plug_id):
    global state
    global manual
    if manual:
       logger.info(f"System in manual mode, not enabling plug")
       return
    state = True
    client.publish(f"cmnd/{plug_id}/Power", 1)

def turn_plug_off(client, plug_id):
    global state
    global manual
    if manual:
       logger.info(f"System in manual mode, not enabling plug")
       return
    state = False
    client.publish(f"cmnd/{plug_id}/Power", 0)

def set_teleperiod(client, plug_id, teleperiod):
    client.publish(f"cmnd/{plug_id}/TelePeriod", teleperiod)

def script_name() -> str:
    """:returns: script name with leading paths removed"""
    return os.path.split(sys.argv[0])[1]

def main() -> int:
    logging.getLogger().setLevel(logging.INFO)
    logging.basicConfig(format="{}: %(asctime)sZ %(levelname)s %(message)s".format(script_name()))
    logging.Formatter.converter = time.gmtime
    parser = argparse.ArgumentParser(description="", epilog="")
    parser.add_argument("-f", "--foreground", action="store_true", help="dont become a daemon")
    parser.add_argument("-c", "--config", type=str, default="config.yaml", help="config file")
    args = parser.parse_args()
    cfg =  OmegaConf.load(args.config)
    if not args.foreground:
        with daemon.DaemonContext():
            while True:
                pw_poll_loop(cfg)
    else:
        while True:
            pw_poll_loop(cfg)
    return 0

if __name__ == "__main__":
    sys.exit(main())


