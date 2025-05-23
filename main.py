import os 
import utime
from machine import Pin, UART, PWM
import binascii
import sys

# Wi-Fi and IRC initialization
irc_channel = "#forgetmenot"
irc_password = "tdtgo.im"
motorspeed = 0.089

class Motor:
    def __init__(self, in1_pin=14, in2_pin=15, pwm_freq=20):
        self.in1_pin = in1_pin
        self.in2_pin = in2_pin
        self.pwm_freq = pwm_freq
        self.setup_pwm()
        self.is_running = False
        self.start_time = None

    def setup_pwm(self):
        # PWM-Instanzen einrichten
        self.in1 = PWM(Pin(self.in1_pin))
        self.in2 = PWM(Pin(self.in2_pin))
        self.in1.freq(self.pwm_freq)
        self.in2.freq(self.pwm_freq)

    def forward(self, speed):
        if not self.is_running:
            print("Motor läuft vorwärts")
            pwm_value = min(max(int(65535 * speed), 0), 65535)
            self.in1.duty_u16(pwm_value)
            self.in2.duty_u16(0)
            self.is_running = True
            self.start_time = utime.ticks_ms()

    def stop(self):
        if self.is_running:
            print("Motor gestoppt")
            self.in1.duty_u16(0)
            self.in2.duty_u16(0)
            self.is_running = False
            self.start_time = None

    def check_and_stop(self):
        # Überprüfen, ob der Motor länger als 20 Sekunden läuft
        if self.is_running and self.start_time is not None:
            elapsed_time = utime.ticks_diff(utime.ticks_ms(), self.start_time)
            if elapsed_time >= 20000:
                self.stop()

class WiFiIRCClientESP01S:
    def __init__(self, uart_id, rx_pin, tx_pin, baudrate=115200):
        self.uart = UART(uart_id, rx=Pin(rx_pin), tx=Pin(tx_pin), baudrate=baudrate)
        self.flush_uart_buffer()
        self.last_alive_time = utime.ticks_ms()
        self.irc_nick = ''
        self.ssid = ''
        self.password = ''

    def flush_uart_buffer(self):
        while self.uart.any():
            self.uart.read(self.uart.any())
    
    def read_response(self, expected, timeout=3000):
        start_time = utime.ticks_ms()
        buffer = ''
        while utime.ticks_diff(utime.ticks_ms(), start_time) < timeout:
            if self.uart.any():
                buffer += self.uart.read(self.uart.any()).decode('utf-8')
                if expected in buffer:
                    return buffer
        return buffer

    def send_command(self, cmd, expected_response='OK', timeout=9000):
        self.flush_uart_buffer()
        self.uart.write((cmd + '\r\n').encode('utf-8'))
        response = self.read_response(expected_response, timeout)
        print(f"Response to '{cmd}': {response.strip()}")
        return response

    def send_tcp_data(self, data):
        try:
            length = len(data)
            self.flush_uart_buffer()
            self.uart.write(f'AT+CIPSEND={length}\r\n'.encode('utf-8'))
            if '>' not in self.read_response('>', timeout=10000):
                print("No '>' prompt received. Cannot send data.")
                return False
            self.uart.write(data.encode('utf-8'))
            if 'SEND OK' not in self.read_response('SEND OK', timeout=15000):
                print("No 'SEND OK' received after sending data.")
                return False
            print("Data sent successfully.")
            return True
        except Exception as e:
            print("Exception in send_tcp_data:", e)
            return False

    def connect_to_wifi(self, ssid, password):
        self.ssid = ssid  # Speichern für spätere Verwendung
        self.password = password
        self.send_command('ATE0')
        if 'OK' not in self.send_command('AT', timeout=2000):
            print("ESP-01S antwortet nicht.")
            return False
        self.send_command('AT+CWMODE=1')
        self.send_command('AT+CIPMUX=0')
        self.send_command('AT+CIPDINFO=1')

        max_attempts = 3  # Insgesamt 3 Versuche
        for attempt in range(1, max_attempts + 1):
            print(f"Versuche, eine Verbindung zum Wi-Fi herzustellen (Versuch {attempt} von {max_attempts})...")
            response = self.send_command(f'AT+CWJAP="{ssid}","{password}"', timeout=15000)
            if 'WIFI GOT IP' in response or 'OK' in response:
                print("Mit Wi-Fi verbunden.")
                utime.sleep(2)
                return True
            else:
                print("Verbindung zum Wi-Fi fehlgeschlagen.")
                if attempt < max_attempts:
                    print("Erneuter Versuch in 5 Sekunden...")
                    utime.sleep(5)
                else:
                    print("Maximale Anzahl an Versuchen erreicht. Verbindung konnte nicht hergestellt werden.")
                    return False

    def is_wifi_connected(self):
        response = self.send_command('AT+CWJAP?', expected_response='OK', timeout=5000)
        connected = '+CWJAP:' in response and 'No AP' not in response
        print(f"is_wifi_connected: {connected}")
        return connected

    def connect_to_irc(self, server, port, irc_nick):
        self.irc_nick = irc_nick  # Nickname speichern
        self.send_command('AT+CIPCLOSE', timeout=5000)
        utime.sleep(0.5)
        while True:
            response = self.send_command(f'AT+CIPSTART="TCP","{server}",{port}', 'CONNECT', 10000)
            if 'CONNECT' in response:
                print("Connected to IRC server.")
                irc_commands = (
                    f"NICK {irc_nick}\r\n"
                    f"USER {irc_nick} 0 * :{irc_nick}\r\n"
                    f"JOIN {irc_channel} {irc_password}\r\n"
                    f"PRIVMSG {irc_channel} :{irc_nick} ist online!\r\n"
                )
                if self.send_tcp_data(irc_commands):
                    self.flush_uart_buffer()  # UART-Puffer leeren
                    return True  # Erfolgreich verbunden
                else:
                    print("Failed to send IRC commands.")
            else:
                print("Failed to connect to IRC server.")
            # Warte und versuche erneut
            print("Retrying to connect to IRC server in 5 Sekunden...")
            utime.sleep(5)

    def handle_server_messages(self):
        global motorspeed
        buffer = ''
        last_data_time = utime.ticks_ms()  # Timer für empfangene Daten
        timeout_interval = 300000  # 5 Minuten in Millisekunden
        while True:
            try:
                if self.uart.any():
                    data = self.uart.read(self.uart.any()).decode('utf-8')
                    buffer += data  # Daten zum Puffer hinzufügen
                    last_data_time = utime.ticks_ms()  # Timer zurücksetzen
                    # Nach vollständigen Zeilen suchen
                    while '\r\n' in buffer:
                        line, buffer = buffer.split('\r\n', 1)
                        line = line.strip()
                        if line:
                            print("Empfangene Zeile:", line)
                            # Hier die empfangene Zeile verarbeiten
                            if 'PRIVMSG' in line:
                                if ':play' in line:
                                    print("play")
                                    motor.forward(motorspeed)
                                elif ':stop' in line:
                                    print("stop")
                                    motor.stop()
                            if "PING :" in line:
                                server_id = line.split("PING :", 1)[1].strip()
                                pong_command = f"PONG :{server_id}\r\n"
                                print("Sende PONG:", pong_command)
                                if not self.send_tcp_data(pong_command):
                                    print("Fehler beim Senden der PONG-Nachricht.")
                                    break  # Schleife verlassen, um die Verbindung neu aufzubauen

                # Überprüfen, ob seit langem keine Daten empfangen wurden
                current_time = utime.ticks_ms()
                if utime.ticks_diff(current_time, last_data_time) >= timeout_interval:
                    print("Timeout: Keine Daten vom IRC-Server empfangen. Verbindung wird neu aufgebaut.")
                    break  # Schleife verlassen, um die Verbindung neu aufzubauen

                # Überprüfen, ob 5 Minuten seit der letzten Alive-Nachricht vergangen sind
                if utime.ticks_diff(current_time, self.last_alive_time) >= 300000:
                    # 'Alive'-Nachricht senden
                    alive_message = f"PRIVMSG {irc_channel} :{self.irc_nick} ist noch aktiv.\r\n"
                    if not self.send_tcp_data(alive_message):
                        print("Fehler beim Senden der Alive-Nachricht.")
                        break  # Schleife verlassen, um die Verbindung neu aufzubauen
                    print("Alive-Nachricht gesendet.")
                    self.last_alive_time = current_time  # Timer zurücksetzen

            except Exception as e:
                print("Exception occurred:", e)
                break  # Schleife verlassen, um die Verbindung neu aufzubauen

            # Überprüfen des Tasters und der Motorsteuerung
            if not button.value():
                if motor.is_running:
                    print("Taster gedrückt, Motor wird gestoppt")
                    motor.stop()
                    self.send_tcp_data(f"PRIVMSG {irc_channel} :stop\r\n")
                else:
                    print("Taster gedrückt, Motor läuft vorwärts")
                    motor.forward(motorspeed)
                    self.send_tcp_data(f"PRIVMSG {irc_channel} :play\r\n")
                utime.sleep(1)
            motor.check_and_stop()
            utime.sleep(0.1)

    def start_hotspot(self):
        self.send_command('ATE0')
        self.send_command('AT+CWMODE=2')
        self.send_command('AT+CWDHCP=0,1')
        self.send_command('AT+CWSAP="PicoSetup","",1,0')
        self.send_command('AT+CIPMUX=1')
        self.send_command('AT+CIPSERVER=1,80')

    def serve_page(self):
        while True:
            if self.uart.any():
                data = self.uart.read(self.uart.any()).decode('utf-8')
                self.process_request(data)
            utime.sleep(0.1)

    def process_request(self, data):
        print("Vollständige Anfrage empfangen.")
        if '+IPD' in data:
            try:
                ipd_index = data.find('+IPD,')
                start = ipd_index + 5
                comma_index = data.find(',', start)
                channel = data[start:comma_index].strip()
                colon_index = data.find(':', comma_index)
                http_request = data[colon_index+1:]
                request_line = http_request.split('\r\n')[0]
                if 'GET / ' in request_line and '?' not in request_line:
                    html = """HTTP/1.1 200 OK\r
Content-Type: text/html\r
Connection: close\r
\r
<!DOCTYPE html>
<html>
<head>
    <title>WLAN Setup</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            background-color: #f4f4f9;
            color: #333;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
        }
        .container {
            background-color: #fff;
            padding: 20px 40px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
            text-align: center;
            max-width: 400px;
            width: 100%;
        }
        h2 {
            color: #4CAF50;
            margin-bottom: 20px;
        }
        form {
            display: flex;
            flex-direction: column;
            gap: 15px;
        }
        input[type="text"],
        input[type="password"] {
            padding: 10px;
            border: 1px solid #ccc;
            border-radius: 5px;
            font-size: 14px;
            width: 100%;
        }
        input[type="submit"] {
            background-color: #4CAF50;
            color: white;
            border: none;
            border-radius: 5px;
            padding: 10px;
            font-size: 16px;
            cursor: pointer;
            transition: background-color 0.3s;
        }
        input[type="submit"]:hover {
            background-color: #45a049;
        }
    </style>
</head>
<body>
    <div class="container">
        <h2>WLAN-Zugangsdaten eingeben und anschliessend neu starten</h2>
        <form action="/set" method="get">
            SSID: <input type="text" name="ssid" required><br>
            Passwort: <input type="password" name="password" required><br>
            <input type="submit" value="Speichern">
        </form>
    </div>
</body>
</html>
"""
                    self.send_data(channel, html)
                elif 'GET /set?' in request_line:
                    query_string = request_line.split('GET /set?', 1)[1].split(' ', 1)[0]
                    params = query_string.split('&')
                    ssid, password = '', ''
                    for param in params:
                        key, value = param.split('=')
                        if key == 'ssid':
                            ssid = value
                        elif key == 'password':
                            password = value
                    ssid = self.url_decode(ssid)
                    password = self.url_decode(password)
                    with open('credentials.txt', 'w') as f:
                        f.write(f"{ssid}\n{password}\n")
                    response = """HTTP/1.1 200 OK\r
Content-Type: text/html\r
Connection: close\r
\r
<!DOCTYPE html>
<html>
<head>
    <title>Bestätigung</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            background-color: #f4f4f9;
            color: #333;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
        }
        .container {
            background-color: #fff;
            padding: 20px 40px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
            text-align: center;
            max-width: 400px;
            width: 100%;
        }
        p {
            font-size: 16px;
            color: #4CAF50;
        }
    </style>
</head>
<body>
    <div class="container">
        <p>WLAN-Zugangsdaten wurden gespeichert. Bitte starten Sie das Geraet neu.</p>
    </div>
</body>
</html>
"""
                    self.send_data(channel, response)
                    self.send_command('AT+CIPSERVER=0')
                    utime.sleep(1)
                    sys.exit()
            except Exception as e:
                print("Fehler beim Verarbeiten der Anfrage:", e)

    def send_data(self, channel, data):
        length = len(data)
        send_cmd = f'AT+CIPSEND={channel},{length}'
        if '>' in self.send_command(send_cmd, '>'):
            self.uart.write(data.encode('utf-8'))
            if 'SEND OK' not in self.read_response('SEND OK', timeout=5000):
                print("Fehler beim Senden der Daten.")
        self.send_command(f'AT+CIPCLOSE={channel}')

    def url_decode(self, s):
        res, i = '', 0
        while i < len(s):
            if s[i] == '+':
                res += ' '
            elif s[i] == '%' and i+2 < len(s):
                res += chr(int(s[i+1:i+3], 16))
                i += 2
            else:
                res += s[i]
            i += 1
        return res

# Global Instances
button = Pin(17, Pin.IN, Pin.PULL_UP)
motor = Motor(in1_pin=14, in2_pin=15)

# Main Function
def main():
    if 'credentials.txt' in os.listdir():
        print("credentials.txt gefunden. Starte Hauptprogramm...")
        with open('credentials.txt', 'r') as f:
            ssid, password = [line.strip() for line in f.readlines()]
        wifi_irc_client = WiFiIRCClientESP01S(uart_id=0, rx_pin=1, tx_pin=0)
        unique_string = binascii.hexlify(os.urandom(4)).decode('utf-8')[:7]
        irc_nick = f"Pico{unique_string}"
        if wifi_irc_client.connect_to_wifi(ssid, password):
            while True:
                if wifi_irc_client.connect_to_irc("irc.oftc.net", 6667, irc_nick):
                    wifi_irc_client.handle_server_messages()
                else:
                    print("Verbindung zum IRC-Server fehlgeschlagen. Neuer Versuch in 5 Sekunden.")
                    utime.sleep(5)
        else:
            print("Lösche credentials.txt aufgrund fehlgeschlagener Verbindung. Bitte neu starten!")
            os.remove('credentials.txt')
    else:
        print("credentials.txt nicht gefunden. Starte Hotspot...")
        wifi_setup = WiFiIRCClientESP01S(uart_id=0, rx_pin=1, tx_pin=0)
        wifi_setup.start_hotspot()
        wifi_setup.serve_page()

main()
