"""ForgetMeNot v2 - Raspberry Pi Pico + ESP-01S (ESP-AT).

Hardware unveraendert: UART0 TX GP0 / RX GP1; Motor GP14/GP15; Taster GP17.
Eine Spieluhr spielt ausschliesslich bei aktiver IRC-Verbindung.

Hinweis: Der Schluessel des WLAN-Tresors wird aus der Pico-UID abgeleitet.
Die Datei enthaelt keinen Klartext; gegen physische Angreifer mit Zugriff
auf den Pico ist dies OHNE Secure Element kein vollwertiger Geheimnisschutz.
"""

import os
import binascii
import machine
import utime
from machine import Pin, UART, PWM, Timer

try:
    import ujson as json
except ImportError:
    import json
try:
    import uhashlib as hashlib
except ImportError:
    import hashlib


IRC_HOST = 'irc.oftc.net'
IRC_PORT = 6667                  # Kompatibel mit vorhandenen Spieluhren
IRC_CHANNEL = '#forgetmenot'
IRC_CHANNEL_KEY = 'sehrgeheim'     # Kanalpasswort, KEIN Authentifizierungsschluessel
MOTOR_SPEED = 0.089            # unveraenderter Motor-Tastgrad
MOTOR_TIME_MS = 20000
WIFI_FILE = 'wifi.sec'
OLD_WIFI_FILE = 'credentials.txt'
RETRY_MS = 5000


def now():
    return utime.ticks_ms()


def elapsed(start):
    return utime.ticks_diff(now(), start)


def hexstr(data):
    return binascii.hexlify(data).decode('ascii')


# Nur die Pico-UID ist dauerhaft vorhanden (kein neues Hardware-Bauteil).
# Der Name bleibt nach Neustarts unveraendert und hat 48 Bit UID-Anteil.
DEVICE_NAME = 'FM' + hexstr(machine.unique_id())[-12:].upper()


def sha256(data):
    return hashlib.sha256(data).digest()


def hmac256(key, message):
    if len(key) > 64:
        key = sha256(key)
    key = key + b'\0' * (64 - len(key))
    inner = bytes(x ^ 0x36 for x in key)
    outer = bytes(x ^ 0x5c for x in key)
    return sha256(outer + sha256(inner + message))


def same_bytes(a, b):
    if len(a) != len(b):
        return False
    different = 0
    for x, y in zip(a, b):
        different |= x ^ y
    return different == 0


def vault_key():
    # Geraetegebunden, aber UID ist keine geheime Hardware-Root-of-Trust.
    return sha256(b'ForgetMeNot wifi vault v2\x00' + machine.unique_id())


def crypt_bytes(key, nonce, data):
    """HMAC-SHA256-basierter XOR-Keystream mit zufaelligem Nonce."""
    result = bytearray(len(data))
    offset = 0
    block = 0
    while offset < len(data):
        stream = hmac256(key, b'ENC\x00' + nonce + block.to_bytes(4, 'big'))
        count = min(32, len(data) - offset)
        for i in range(count):
            result[offset + i] = data[offset + i] ^ stream[i]
        offset += count
        block += 1
    return bytes(result)


def save_wifi(ssid, password, reset_esp=False):
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in ssid + password):
        raise ValueError('Steuerzeichen in Zugangsdaten nicht erlaubt')
    if not (1 <= len(ssid.encode('utf-8')) <= 32):
        raise ValueError('SSID muss 1-32 UTF-8-Bytes lang sein')
    if not (8 <= len(password.encode('utf-8')) <= 63):
        raise ValueError('WLAN-Passwort muss 8-63 Bytes lang sein')
    nonce = os.urandom(16)
    key = vault_key()
    plain = json.dumps({'ssid': ssid, 'password': password}).encode('utf-8')
    cipher = crypt_bytes(key, nonce, plain)
    tag = hmac256(key, b'MAC\x00' + nonce + cipher)
    record = {'v': 2, 'nonce': hexstr(nonce), 'data': hexstr(cipher),
              'mac': hexstr(tag), 'reset_esp': bool(reset_esp)}
    with open(WIFI_FILE + '.tmp', 'w') as fh:
        fh.write(json.dumps(record))
    # Auf MicroPython unterstuetzt os.rename nicht immer das Ueberschreiben.
    try:
        os.rename(WIFI_FILE + '.tmp', WIFI_FILE)
    except OSError:
        try:
            os.remove(WIFI_FILE)
        except OSError:
            pass
        os.rename(WIFI_FILE + '.tmp', WIFI_FILE)


def load_wifi():
    try:
        with open(WIFI_FILE, 'r') as fh:
            record = json.loads(fh.read())
        if record.get('v') != 2:
            raise ValueError('Unbekanntes Format')
        nonce = binascii.unhexlify(record['nonce'])
        cipher = binascii.unhexlify(record['data'])
        tag = binascii.unhexlify(record['mac'])
        if len(nonce) != 16 or len(tag) != 32 or len(cipher) > 512:
            raise ValueError('Ungueltige Datensatzgroesse')
        key = vault_key()
        if not same_bytes(hmac256(key, b'MAC\x00' + nonce + cipher), tag):
            raise ValueError('Integritaetsfehler oder anderer Pico')
        result = json.loads(crypt_bytes(key, nonce, cipher).decode('utf-8'))
        return result['ssid'], result['password']
    except (OSError, ValueError, KeyError, TypeError) as err:
        print('Kein gueltiger WLAN-Tresor:', type(err).__name__)
        return None


def migrate_old_wifi():
    """Bestehendes v1-Setup uebernehmen, alte Klartextdatei entfernen."""
    if OLD_WIFI_FILE not in os.listdir():
        return
    if WIFI_FILE not in os.listdir():
        try:
            with open(OLD_WIFI_FILE, 'r') as fh:
                lines = fh.read().splitlines()
            if len(lines) >= 2:
                # V1 konnte das WLAN-Passwort auch im ESP8266-Flash ablegen.
                save_wifi(lines[0], lines[1], reset_esp=True)
            else:
                print('Alte WLAN-Datei unvollstaendig; Setup erforderlich.')
                return
        except Exception as err:
            print('Migration fehlgeschlagen:', type(err).__name__)
            return
    try:
        os.remove(OLD_WIFI_FILE)
        print('Alte Klartextdatei entfernt (alte Flash-Reste evtl. vorhanden).')
    except OSError:
        print('WARNUNG: Alte Klartextdatei konnte nicht geloescht werden!')


def esp_reset_pending():
    try:
        with open(WIFI_FILE, 'r') as fh:
            return bool(json.loads(fh.read()).get('reset_esp', False))
    except (OSError, ValueError):
        return False


class Motor:
    def __init__(self):
        self.in1 = PWM(Pin(14))
        self.in2 = PWM(Pin(15))
        # Unveraendert gegenueber v1, um Motorverhalten nicht zu veraendern.
        self.in1.freq(20)
        self.in2.freq(20)
        self.in1.duty_u16(0)
        self.in2.duty_u16(0)
        self.timer = Timer()
        self.running = False

    def _timeout(self, _timer):
        # Kein Print/keine Speicherallokation in Timer-Callback.
        self.in1.duty_u16(0)
        self.in2.duty_u16(0)
        self.running = False

    def start(self):
        if self.running:
            return
        self.in2.duty_u16(0)
        self.timer.init(period=MOTOR_TIME_MS, mode=Timer.ONE_SHOT,
                        callback=self._timeout)
        self.in1.duty_u16(int(65535 * MOTOR_SPEED))
        self.running = True
        print('Motor an:', DEVICE_NAME)

    def stop(self):
        self.in1.duty_u16(0)
        self.in2.duty_u16(0)
        self.running = False
        self.timer.deinit()


class DebouncedButton:
    def __init__(self):
        self.pin = Pin(17, Pin.IN, Pin.PULL_UP)
        self.raw = self.pin.value()
        self.stable = self.raw
        self.changed = now()

    def pressed(self):
        value = self.pin.value()
        if value != self.raw:
            self.raw = value
            self.changed = now()
        if value != self.stable and elapsed(self.changed) >= 50:
            self.stable = value
            return value == 0
        return False


class ESPAT:
    """Einziger UART-Leser; trennt AT-Antworten und TCP-Nutzdaten."""
    def __init__(self):
        self.uart = UART(0, baudrate=115200, tx=Pin(0), rx=Pin(1))
        self.buffer = bytearray()
        self.responses = []
        self.on_data = None
        self.tcp_connected = False
        self.wifi_connected = False
        self.mux = False

    def _line(self, line):
        try:
            text = line.decode('utf-8').strip()
        except UnicodeError:
            return  # Beschadigte AT-Zeile ignorieren statt abzustuerzen.
        if not text:
            return
        if len(self.responses) > 35:
            self.responses.pop(0)
        self.responses.append(text)
        if text == 'CONNECT' or text.endswith(',CONNECT'):
            self.tcp_connected = True
        if text == 'CLOSED' or text.endswith(',CLOSED') or text == 'WIFI DISCONNECT':
            self.tcp_connected = False
        if text == 'WIFI DISCONNECT':
            self.wifi_connected = False
        if text == 'WIFI GOT IP':
            self.wifi_connected = True

    def poll(self):
        count = self.uart.any()
        if count:
            data = self.uart.read(min(count, 2048))
            if data:
                self.buffer.extend(data)
        while self.buffer:
            if self.buffer.startswith(b'\r\n'):
                self.buffer = self.buffer[2:]
                continue
            if self.buffer[:1] in (b'\r', b'\n', b' '):
                self.buffer = self.buffer[1:]
                continue
            if self.buffer.startswith(b'+IPD,'):
                colon = self.buffer.find(b':')
                if colon < 0:
                    if len(self.buffer) > 100:
                        self.buffer = self.buffer[5:]
                    break
                header = bytes(self.buffer[5:colon]).split(b',')
                try:
                    if self.mux:
                        link = int(header[0])
                        size = int(header[1])
                    else:
                        link = 0
                        size = int(header[0])
                except (ValueError, IndexError):
                    self.buffer = self.buffer[colon + 1:]
                    continue
                if size < 0 or size > 8192:
                    self.buffer = self.buffer[colon + 1:]
                    continue
                if len(self.buffer) < colon + 1 + size:
                    break
                payload = bytes(self.buffer[colon + 1:colon + 1 + size])
                self.buffer = self.buffer[colon + 1 + size:]
                if self.on_data:
                    self.on_data(link, payload)
                continue
            if self.buffer.startswith(b'>'):
                self.buffer = self.buffer[1:]
                self._line(b'>')
                continue
            newline = self.buffer.find(b'\n')
            if newline < 0:
                if len(self.buffer) > 1024:
                    self.buffer = bytearray()
                break
            line = bytes(self.buffer[:newline]).strip()
            self.buffer = self.buffer[newline + 1:]
            self._line(line)

    def _wait(self, expected, timeout_ms):
        started = now()
        while elapsed(started) < timeout_ms:
            self.poll()
            for line in self.responses:
                if line == expected or (expected == 'CONNECT' and line == 'ALREADY CONNECTED'):
                    return True
                if line in ('ERROR', 'FAIL', 'SEND FAIL', 'busy p...', 'busy s...'):
                    return False
                if (line.startswith('+CWJAP:') and expected == 'OK'
                        and line[7:].strip().isdigit()):
                    return False
            utime.sleep_ms(10)
        return False

    def cmd(self, command, expected='OK', timeout_ms=3000):
        # Nie AT-Befehle drucken: CWJAP enthaelt das WLAN-Passwort.
        self.poll()
        self.responses = []
        self.uart.write((command + '\r\n').encode('utf-8'))
        return self._wait(expected, timeout_ms)

    def send(self, payload, link=None):
        if isinstance(payload, str):
            payload = payload.encode('utf-8')
        arg = str(len(payload)) if link is None else '%d,%d' % (link, len(payload))
        if not self.cmd('AT+CIPSEND=' + arg, expected='>', timeout_ms=3500):
            return False
        self.responses = []
        self.uart.write(payload)
        return self._wait('SEND OK', 4500)

    def init_at(self):
        if not self.cmd('AT', timeout_ms=1500):
            return False
        if not self.cmd('ATE0', timeout_ms=1500):
            return False
        # SYSSTORE=0 verhindert das Ablegen von WLAN-Daten im ESP-Flash.
        self.no_store = self.cmd('AT+SYSSTORE=0', timeout_ms=1500)
        return True

    def connect_wifi(self, ssid, password):
        self.wifi_connected = False
        self.tcp_connected = False
        if not self.init_at():
            return False
        # Bei alter AT-Firmware verhindert _CUR ebenfalls Flash-Schreibzugriffe.
        mode_cmd = 'AT+CWMODE=1' if self.no_store else 'AT+CWMODE_CUR=1'
        if not self.cmd(mode_cmd):
            return False
        self.cmd('AT+CIPSERVER=0', timeout_ms=1500)
        if not self.cmd('AT+CIPMUX=0'):
            return False
        self.mux = False
        if not self.cmd('AT+CIPDINFO=0'):
            return False
        # Fuer aeltere ESP-AT-Versionen ist _CUR explizit RAM-only.
        # Bei neuerer Firmware verhindert SYSSTORE=0 Flash-Persistenz.
        join_base = 'AT+CWJAP' if self.no_store else 'AT+CWJAP_CUR'
        esc = lambda s: s.replace('\\', '\\\\').replace('"', '\\"')
        join = '%s="%s","%s"' % (join_base, esc(ssid), esc(password))
        # Keine Zugangsdaten auf der Konsole ausgeben.
        if not self.cmd(join, timeout_ms=20000):
            return False
        self.responses = []
        if not self.cmd('AT+CWJAP?', timeout_ms=4000):
            return False
        connected = any(line.startswith('+CWJAP:') for line in self.responses)
        self.wifi_connected = connected
        return connected

    def tcp_open(self):
        self.tcp_connected = False
        return self.cmd('AT+CIPSTART="TCP","%s",%d' % (IRC_HOST, IRC_PORT),
                        expected='CONNECT', timeout_ms=10000) and self.tcp_connected

    def tcp_close(self):
        self.cmd('AT+CIPCLOSE', timeout_ms=2000)
        self.tcp_connected = False


class IRCClient:
    def __init__(self, esp, motor):
        self.esp = esp
        self.motor = motor
        self.nick = DEVICE_NAME
        self.buf = bytearray()
        self.lines = []
        self.joined = False
        self.verified = False
        self.welcomed = False
        self.nickname_used = False
        self.denied = False
        self.ping_token = None
        self.pong_seen = False
        self.verified = False
        self.last_pong = now()
        self.esp.on_data = self._receive

    def _receive(self, link, data):
        if link != 0:
            return
        self.buf.extend(data)
        if len(self.buf) > 2048:
            self.buf = bytearray()
            return
        while b'\r\n' in self.buf:
            end = self.buf.find(b'\r\n')
            raw = bytes(self.buf[:end])
            self.buf = self.buf[end + 2:]
            if len(self.lines) < 32:
                try:
                    self.lines.append(raw.decode('utf-8'))
                except UnicodeError:
                    pass  # Beschaedigte IRC-Nachrichten ignorieren.

    def send_line(self, line):
        if not self.esp.tcp_connected:
            return False
        if not self.esp.send(line + '\r\n'):
            self.joined = False
            self.motor.stop()
            return False
        return True

    def process(self):
        self.esp.poll()
        if not self.esp.tcp_connected or not self.esp.wifi_connected:
            self.joined = False
            self.motor.stop()
        while self.lines:
            line = self.lines.pop(0)
            if line.startswith('PING :'):
                if not self.send_line('PONG :' + line[6:]):
                    return
                continue
            parts = line.split(' ', 3)
            cmd = parts[1].upper() if line.startswith(':') and len(parts) > 1 else parts[0].upper()
            if cmd == '001':
                self.welcomed = True
            elif cmd == '433':
                self.nickname_used = True
            elif cmd in ('471', '473', '474', '475', '403', '405'):
                self.denied = True
            elif cmd == 'JOIN' and line.startswith(':' + self.nick + '!'):
                if IRC_CHANNEL.lower() in line.lower():
                    self.joined = True
            elif cmd == 'PART' and line.startswith(':' + self.nick + '!'):
                self.joined = False
                self.verified = False
                self.motor.stop()
            elif cmd == 'KICK' and len(parts) == 4:
                # :server KICK #channel nickname :reason
                words = parts[3].split(' ', 1)
                if parts[2].lower() == IRC_CHANNEL.lower() and words[0].lower() == self.nick.lower():
                    self.joined = False
                    self.verified = False
                    self.motor.stop()
            elif cmd == 'PONG':
                if self.ping_token and line.endswith(' :' + self.ping_token):
                    self.pong_seen = True
                    self.verified = True
                    self.last_pong = now()
            elif cmd == 'PRIVMSG' and len(parts) == 4:
                sender = line[1:].split('!', 1)[0] if line.startswith(':') else ''
                target = parts[2]
                payload = parts[3]
                if payload.startswith(':'):
                    payload = payload[1:]
                if self.online() and sender and sender.lower() != self.nick.lower() and target.lower() == IRC_CHANNEL.lower():
                    if payload == 'play':
                        self.motor.start()
                    elif payload == 'stop':
                        self.motor.stop()

    def online(self):
        return (self.verified and self.joined and self.esp.tcp_connected and self.esp.wifi_connected
                and elapsed(self.last_pong) <= 45000)

    def wait_for(self, predicate, timeout_ms):
        started = now()
        while elapsed(started) < timeout_ms:
            self.process()
            if predicate():
                return True
            if self.denied or not self.esp.tcp_connected or not self.esp.wifi_connected:
                return False
            utime.sleep_ms(20)
        return False

    def probe(self):
        if not self.joined or not self.esp.tcp_connected or not self.esp.wifi_connected:
            self.motor.stop()
            return False
        self.ping_token = self.nick + '-' + str(now())
        self.pong_seen = False
        if not self.send_line('PING :' + self.ping_token):
            return False
        if self.wait_for(lambda: self.pong_seen, 4500):
            return True
        self.joined = False
        self.verified = False
        self.motor.stop()
        return False

    def connect(self):
        if not self.esp.tcp_open():
            return False
        # Nach TCP-Connect ist IRC noch nicht online!
        self.joined = False
        self.verified = False
        self.welcomed = False
        self.nickname_used = False
        self.denied = False
        if not self.send_line('NICK ' + self.nick):
            return False
        if not self.send_line('USER %s 0 * :ForgetMeNot' % self.nick):
            return False
        if not self.wait_for(lambda: self.welcomed or self.nickname_used, 10000):
            return False
        if self.nickname_used:
            # Kollision eines Nicks (z.B. alte IRC-Verbindung noch aktiv).
            for candidate in range(1, 10):
                self.nickname_used = False
                self.nick = DEVICE_NAME[:-1] + str(candidate)
                if not self.send_line('NICK ' + self.nick):
                    return False
                if self.wait_for(lambda: self.welcomed or self.nickname_used, 3500) and self.welcomed:
                    break
            if not self.welcomed:
                return False
        if not self.send_line('JOIN %s %s' % (IRC_CHANNEL, IRC_CHANNEL_KEY)):
            return False
        if not self.wait_for(lambda: self.joined, 10000):
            return False
        if not self.probe():
            return False
        print('IRC online als', self.nick)
        return True


def percent_decode(value):
    src = value.encode('ascii')
    out = bytearray()
    index = 0
    while index < len(src):
        if src[index] == 43:  # +
            out.append(32)
        elif src[index] == 37:  # %XX
            if index + 2 >= len(src):
                raise ValueError('Fehlerhafte URL-Kodierung')
            out.append(int(src[index + 1:index + 3], 16))
            index += 2
        else:
            out.append(src[index])
        index += 1
    return bytes(out).decode('utf-8')


def form_data(body):
    fields = {}
    for item in body.decode('ascii').split('&'):
        if '=' in item:
            key, value = item.split('=', 1)
            fields[percent_decode(key)] = percent_decode(value)
    return fields


class SetupPortal:
    def __init__(self, esp):
        self.esp = esp
        self.pending = {}
        self.ready = []
        self.esp.on_data = self.on_data

    def on_data(self, link, data):
        buf = self.pending.get(link, b'') + data
        if len(buf) > 1800:
            self.pending.pop(link, None)
            self.ready.append((link, None))
            return
        sep = buf.find(b'\r\n\r\n')
        if sep < 0:
            self.pending[link] = buf
            return
        try:
            header = buf[:sep].decode('ascii')
        except UnicodeError:
            self.pending.pop(link, None)
            self.ready.append((link, None))
            return
        length = 0
        for line in header.split('\r\n'):
            if line.lower().startswith('content-length:'):
                try:
                    length = int(line.split(':', 1)[1].strip())
                except ValueError:
                    length = -1
        if length < 0 or length > 500:
            self.pending.pop(link, None)
            self.ready.append((link, None))
            return
        if len(buf) < sep + 4 + length:
            self.pending[link] = buf
            return
        self.pending.pop(link, None)
        self.ready.append((link, (header.split('\r\n')[0], buf[sep + 4:sep + 4 + length])))

    def reply(self, link, status, content):
        # Die Gestaltung stammt aus der urspruenglichen Setupseite.
        # CSS bleibt eingebettet: Auf dem Pico wird keine weitere Datei gebraucht.
        page = ('''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ForgetMeNot WLAN-Setup</title>
<style>
*{box-sizing:border-box}
body{font-family:Arial,sans-serif;background:#f4f4f9;color:#333;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;padding:16px}
.container{background:#fff;padding:20px 40px;border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,.1);text-align:center;max-width:400px;width:100%}
h2{color:#4CAF50;margin:0 0 20px}
form{display:flex;flex-direction:column;gap:15px;text-align:left}
input{display:block;padding:10px;margin-top:5px;border:1px solid #ccc;border-radius:5px;font-size:14px;width:100%}
button{background:#4CAF50;color:#fff;border:0;border-radius:5px;padding:10px;font-size:16px;cursor:pointer}
button:hover{background:#45a049}
.device{font-size:12px;color:#777;word-break:break-word}
@media(max-width:420px){.container{padding:20px}}
</style>
</head>
<body><div class="container"><h2>WLAN-Setup</h2><p class="device">'''
                + DEVICE_NAME + '''</p>'''
                + content + '''</div></body></html>''')
        body = page.encode('utf-8')
        response = ('HTTP/1.1 ' + status + '\r\nContent-Type: text/html; charset=utf-8\r\n'
                    'Cache-Control: no-store\r\nConnection: close\r\nContent-Length: %d\r\n\r\n' % len(body))
        self.esp.send(response.encode('ascii') + body, link=link)
        self.esp.cmd('AT+CIPCLOSE=%d' % link, timeout_ms=1500)

    def setup(self):
        if not self.esp.init_at():
            print('ESP8266 antwortet nicht. Setup nicht moeglich.')
            return False
        # Neue Firmware: SYSSTORE=0. Alte Firmware: _CUR-Befehle.
        mode_cmd = 'AT+CWMODE=2' if self.esp.no_store else 'AT+CWMODE_CUR=2'
        ap_cmd = 'AT+CWSAP' if self.esp.no_store else 'AT+CWSAP_CUR'
        ssid = 'vergissmeinnicht'
        # Offenes Setup-WLAN (ENC=0): kein WLAN-Passwort erforderlich.
        if not self.esp.cmd(mode_cmd):
            return False
        if not self.esp.cmd(ap_cmd + '="%s","",1,0' % ssid):
            return False
        if not self.esp.cmd('AT+CIPMUX=1'):
            return False
        self.esp.mux = True
        if not self.esp.cmd('AT+CIPDINFO=0'):
            return False
        if not self.esp.cmd('AT+CIPSERVER=1,80'):
            return False
        print('Setup-WLAN:', ssid, '(offen, ohne Passwort)')
        print('Browser: http://192.168.4.1')
        return True

    def run(self):
        while True:
            self.esp.poll()
            while self.ready:
                link, request = self.ready.pop(0)
                if request is None:
                    self.reply(link, '413 Payload Too Large', '<p>Anfrage zu gross.</p>')
                    continue
                request_line, body = request
                if request_line.startswith('GET / '):
                    self.reply(link, '200 OK',
                               '<p>SSID und WLAN-Passwort eingeben.</p>'
                               '<form method="post" action="/save">'
                               '<label>WLAN-Name <input name="ssid" required maxlength="32"></label><p></p>'
                               '<label>Passwort <input type="password" name="password" required '
                               'minlength="8" maxlength="63"></label><p></p>'
                               '<button type="submit">Speichern und neu starten</button></form>')
                elif request_line.startswith('POST /save '):
                    try:
                        params = form_data(body)
                        save_wifi(params['ssid'], params['password'])
                        self.reply(link, '200 OK',
                                   '<p>WLAN-Daten verschluesselt gespeichert. Neustart.</p>')
                        utime.sleep_ms(800)
                        machine.reset()
                    except (ValueError, KeyError, UnicodeError) as err:
                        self.reply(link, '400 Bad Request',
                                   '<p>Ungueltige Eingabe. Bitte WLAN-Daten pruefen.</p>')
                else:
                    self.reply(link, '404 Not Found', '<p>Seite nicht vorhanden.</p>')
            utime.sleep_ms(15)


def session(esp, motor):
    irc = IRCClient(esp, motor)
    if not irc.connect():
        motor.stop()
        esp.tcp_close()
        return
    button = DebouncedButton()
    last_probe = now()
    while irc.online():
        irc.process()
        if not irc.online():
            break
        if button.pressed():
            # Jede lokale Aktion braucht zuerst eine FRISCHE Server-Bestaetigung.
            if irc.probe():
                if motor.running:
                    if irc.send_line('PRIVMSG %s :stop' % IRC_CHANNEL):
                        motor.stop()
                else:
                    if irc.send_line('PRIVMSG %s :play' % IRC_CHANNEL) and irc.online():
                        motor.start()
                last_probe = now()
        if elapsed(last_probe) >= 30000:
            if not irc.probe():
                break
            last_probe = now()
        utime.sleep_ms(15)
    motor.stop()
    esp.tcp_close()
    print('IRC offline - Motor gesperrt')


def main():
    motor = Motor()
    try:
        migrate_old_wifi()
        esp = ESPAT()
        creds = load_wifi()
        if creds is not None and esp_reset_pending():
            print('Entferne WLAN-Altlasten aus dem ESP8266-Flash...')
            if not esp.init_at() or not esp.cmd('AT+RESTORE', timeout_ms=5000):
                print('ESP-Flash konnte nicht zurueckgesetzt werden. Motor bleibt aus.')
                return
            utime.sleep_ms(2500)  # AT+RESTORE startet den ESP8266 neu
            save_wifi(creds[0], creds[1])  # Reset-Kennzeichen erst nach Erfolg entfernen
        if creds is None:
            print('Keine WLAN-Zugangsdaten. Setup fuer', DEVICE_NAME)
            portal = SetupPortal(esp)
            if portal.setup():
                portal.run()
            return
        print('ForgetMeNot v2:', DEVICE_NAME)
        while True:
            motor.stop()
            try:
                if esp.connect_wifi(creds[0], creds[1]):
                    session(esp, motor)
                else:
                    print('WLAN offline; erneuter Verbindungsversuch.')
            except Exception as err:
                print('Verbindungsfehler:', type(err).__name__)
            motor.stop()
            utime.sleep_ms(RETRY_MS)
    finally:
        motor.stop()


if __name__ == '__main__':
    main()
