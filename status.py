import datetime
import pytz
from http.server import *
import argparse
from omegaconf import OmegaConf
import paho.mqtt.subscribe as subscribe
import ast

global cfg

def make_my_tz(time_string):
    my_tz = pytz.timezone('Australia/Sydney')
    utc = datetime.datetime.strptime(time_string.split(",")[0], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    return str(utc.astimezone(my_tz))

def generate_body():
    global cfg
    out = """
    <html>
    <head><title>Skutter Pool control status</title></head>
    <body>
    """

    msg = subscribe.simple(f"{cfg.mqtt_pool_topic}/runtime", hostname=cfg.mqtt_server, port=cfg.mqtt_server_port, auth={'username':cfg.mqtt_username, 'password': cfg.mqtt_password})
    runtime = int(msg.payload)

    msg = subscribe.simple(f"{cfg.mqtt_pool_topic}/status", hostname=cfg.mqtt_server, port=cfg.mqtt_server_port, auth={'username':cfg.mqtt_username, 'password': cfg.mqtt_password})
    status = ast.literal_eval(msg.payload.decode('utf-8'))

    msg = subscribe.simple(f"{cfg.mqtt_pool_topic}/heartbeat", hostname=cfg.mqtt_server, port=cfg.mqtt_server_port, auth={'username':cfg.mqtt_username, 'password': cfg.mqtt_password})
    lastlog = msg.payload.decode('utf-8')

    msg = subscribe.simple(f"{cfg.mqtt_pool_topic}/solar_free", hostname=cfg.mqtt_server, port=cfg.mqtt_server_port, auth={'username':cfg.mqtt_username, 'password': cfg.mqtt_password})
    solar_excess = msg.payload.decode('utf-8')

    pool_hours = [0, 8, 8, 6, 6, 6, 4, 4, 4, 6, 6, 6, 8]
    now = datetime.datetime.now()
    max_run = pool_hours[now.month] *60*60

    out += "<p>" + str(status) + "</p>"
    out += f"<p>Runtime: {runtime}/{max_run} {(max_run - int(runtime))/60/60} hours remain</p>"
    out += f"<p>Current available solar watts: {solar_excess}</p>"
    out += f"<p>Last check-in time {lastlog}</p>"

    out += "</body></html>"

    return out


parser = argparse.ArgumentParser(description="", epilog="")
parser.add_argument("-f", "--foreground", action="store_true", help="dont become a daemon")
parser.add_argument("-c", "--config", type=str, default="config.yaml", help="config file")
args = parser.parse_args()
cfg =  OmegaConf.load(args.config)

class Server(BaseHTTPRequestHandler):

    def do_GET(self):

        self.send_response(200)

        self.send_header('content-type', 'text/html')
        self.end_headers()

        body = generate_body()      
        self.wfile.write(body.encode())

port = HTTPServer(('', 5555), Server)

port.serve_forever()
