"""ForgetMeNot v3 - Raspberry Pi Pico W / Pico 2 W (integriertes WLAN).

Hardware: Motor ueber externe Treiberschaltung an GP14/GP15; Taster GP17.
ESP-01S und UART-Leitungen GP0/GP1 entfallen.
Eine Spieluhr spielt ausschliesslich bei aktiver IRC-Verbindung.

Hinweis: Der Schluessel des WLAN-Tresors wird aus der Pico-UID abgeleitet.
Die Datei enthaelt keinen Klartext; gegen physische Angreifer mit Zugriff
auf den Pico ist dies OHNE Secure Element kein vollwertiger Geheimnisschutz.
"""

import os
import binascii
import machine
import utime
from machine import Pin, PWM, Timer
import network
import socket

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
IRC_CHANNEL = "#forgetmenot"
IRC_CHANNEL_KEY = ""    # Kanalpasswort, KEIN Authentifizierungsschluessel
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


def save_wifi(ssid, password):
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
              'mac': hexstr(tag)}
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
    """Bestehendes v1-Setup auf demselben Pico uebernehmen."""
    if OLD_WIFI_FILE not in os.listdir():
        return
    if WIFI_FILE not in os.listdir():
        try:
            with open(OLD_WIFI_FILE, 'r') as fh:
                lines = fh.read().splitlines()
            if len(lines) >= 2:
                # Uebernimmt ggf. alte Zugangsdaten auf DEMSELBEN Pico.
                save_wifi(lines[0], lines[1])
            else:
                print('Alte WLAN-Datei unvollstaendig; Setup erforderlich.')
                return
        except Exception as err:
            print('Migration fehlgeschlagen:', type(err).__name__)
            return
    try:
        os.remove(OLD_WIFI_FILE)
        print('Alte Klartextdatei entfernt.')
    except OSError:
        print('WARNUNG: Alte Klartextdatei konnte nicht geloescht werden!')


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


class PicoWiFi:
    """Native WLAN/TCP-Implementierung als Ersatz fuer ESP-AT."""
    def __init__(self):
        self.wlan = network.WLAN(network.WLAN.IF_STA)
        self.sock = None
        self.on_data = None
        self.tcp_connected = False
        self.wifi_connected = False

    def connect_wifi(self, ssid, password):
        self.tcp_close()
        self.wifi_connected = False
        # Neustart des WLAN-Interfaces bei jedem Verbindungsversuch.
        self.wlan.active(False)
        utime.sleep_ms(200)
        self.wlan.active(True)
        # Fuer moeglichst stabile laufende IRC-Verbindung.
        try:
            self.wlan.config(pm=network.WLAN.PM_NONE)
        except (OSError, AttributeError, ValueError):
            pass
        self.wlan.connect(ssid, password)
        started = now()
        while elapsed(started) < 25000:
            if self.wlan.isconnected():
                self.wifi_connected = True
                print('WLAN verbunden; IP:', self.wlan.ifconfig()[0])
                return True
            status = self.wlan.status()
            if status in (network.STAT_WRONG_PASSWORD, network.STAT_NO_AP_FOUND,
                          network.STAT_CONNECT_FAIL):
                break
            utime.sleep_ms(200)
        print('WLAN-Verbindung gescheitert; Status:', self.wlan.status())
        self.wlan.active(False)
        return False

    def tcp_open(self):
        self.tcp_close()
        if not self.wlan.isconnected():
            self.wifi_connected = False
            return False
        try:
            addr = socket.getaddrinfo(IRC_HOST, IRC_PORT, 0, socket.SOCK_STREAM)[0][-1]
            self.sock = socket.socket()
            self.sock.settimeout(10)
            self.sock.connect(addr)
            self.sock.setblocking(False)
            self.tcp_connected = True
            return True
        except OSError as err:
            print('TCP-Verbindung fehlgeschlagen:', type(err).__name__)
            self.tcp_close()
            return False

    def tcp_close(self):
        self.tcp_connected = False
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def send(self, data):
        if isinstance(data, str):
            data = data.encode('utf-8')
        if not self.tcp_connected or self.sock is None:
            return False
        try:
            # Im blockierenden Modus schreibt MicroPython socket.write
            # alle Daten oder meldet einen Fehler (mit Timeout).
            self.sock.settimeout(4)
            sent = self.sock.write(data)
            if sent != len(data):
                self.tcp_close()
                return False
            self.sock.setblocking(False)
            return True
        except OSError:
            self.tcp_close()
            return False

    def poll(self):
        self.wifi_connected = self.wlan.isconnected()
        if not self.wifi_connected:
            self.tcp_close()
            return
        if not self.tcp_connected or self.sock is None:
            return
        for _ in range(4):
            try:
                packet = self.sock.recv(512)
            except OSError as err:
                # EAGAIN/EWOULDBLOCK: im nichtblockierenden Modus keine Daten.
                if err.args and err.args[0] in (11, 35):
                    return
                self.tcp_close()
                return
            if not packet:
                # Gegenseite hat die Verbindung geschlossen.
                self.tcp_close()
                return
            if self.on_data:
                self.on_data(0, packet)
            if len(packet) < 512:
                break


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
    """Offenes temporaeres WLAN-Portal zur Einrichtung (ohne Passwort)."""
    def __init__(self):
        self.ap = network.WLAN(network.WLAN.IF_AP)
        self.server = None

    @staticmethod
    def page(content):
        return ('''<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ForgetMeNot WLAN-Setup</title><style>
body{font:16px Arial,sans-serif;background:#f4f4f9;color:#333;padding:20px}
main{max-width:420px;margin:10vh auto;padding:30px;background:white;
 border-radius:10px;box-shadow:0 2px 10px #ddd}
label{display:block;margin:15px 0}input{width:95%;padding:10px}
button{background:#4caf50;color:white;border:0;border-radius:5px;padding:10px}
small{color:#777}</style></head><body><main><h2>WLAN-Setup</h2><small>''' + DEVICE_NAME + '''</small>''' + content + '''</main></body></html>''')

    def reply(self, client, status, content):
        body = self.page(content).encode('utf-8')
        header = ('HTTP/1.1 ' + status + '\r\n'
                  'Content-Type: text/html; charset=utf-8\r\n'
                  'Cache-Control: no-store\r\nConnection: close\r\n'
                  'Content-Length: %d\r\n\r\n' % len(body)).encode('ascii')
        client.write(header + body)

    @staticmethod
    def request(client):
        """Liest eine kleine HTTP-Anfrage; blockiert maximal wenige Sekunden."""
        client.settimeout(3)
        buf = b''
        while b'\r\n\r\n' not in buf:
            part = client.recv(512)
            if not part:
                raise ValueError('Unvollstaendiger HTTP-Header')
            buf += part
            if len(buf) > 2048:
                raise ValueError('Zu grosser HTTP-Header')
        head, body = buf.split(b'\r\n\r\n', 1)
        lines = head.decode('ascii').split('\r\n')
        length = 0
        for line in lines[1:]:
            if line.lower().startswith('content-length:'):
                length = int(line.split(':', 1)[1].strip())
        if length < 0 or length > 500:
            raise ValueError('Ungueltige POST-Groesse')
        while len(body) < length:
            part = client.recv(min(512, length - len(body)))
            if not part:
                raise ValueError('Unvollstaendiger POST-Body')
            body += part
        return lines[0], body[:length]

    def setup(self):
        ssid = 'vergissmeinnicht'
        try:
            # Einrichtungs-WLAN bewusst OFFEN: security=0, kein Passwort.
            # Die Heim-WLAN-Zugangsdaten werden dabei unverschluesselt
            # per HTTP uebertragen. Nur in vertrauenswuerdiger Umgebung nutzen.
            self.ap.config(ssid=ssid, security=0)
            self.ap.active(True)
            ip = self.ap.ifconfig()[0]
            self.server = socket.socket()
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind(('0.0.0.0', 80))
            self.server.listen(1)
            self.server.settimeout(1)
        except Exception as err:
            print('Setup-AP konnte nicht gestartet werden:', type(err).__name__, err)
            self.close()
            return False
        print('Setup-WLAN:', ssid, '(offen, ohne Passwort)')
        print('Browser: http://' + ip)
        return True

    def close(self):
        if self.server:
            try:
                self.server.close()
            except OSError:
                pass
            self.server = None
        try:
            self.ap.active(False)
        except OSError:
            pass

    def run(self):
        try:
            while True:
                try:
                    client, _ = self.server.accept()
                except OSError:
                    continue
                restart = False
                try:
                    request_line, body = self.request(client)
                    if request_line.startswith('GET / '):
                        self.reply(client, '200 OK',
                            '<p>WLAN-Zugangsdaten eingeben.</p>'
                            '<form method="post" action="/save">'
                            '<label>WLAN-Name<input name="ssid" required maxlength="32"></label>'
                            '<label>Passwort<input type="password" name="password" '
                            'required minlength="8" maxlength="63"></label>'
                            '<button type="submit">Speichern</button></form>')
                    elif request_line.startswith('POST /save '):
                        try:
                            fields = form_data(body)
                            save_wifi(fields['ssid'], fields['password'])
                            self.reply(client, '200 OK',
                                       '<p>WLAN gespeichert. Pico startet neu.</p>')
                            restart = True
                        except (ValueError, KeyError, UnicodeError, OSError):
                            self.reply(client, '400 Bad Request',
                                       '<p>Eingaben ungueltig. Bitte pruefen.</p>')
                    else:
                        self.reply(client, '404 Not Found', '<p>Nicht gefunden.</p>')
                except (OSError, ValueError, UnicodeError) as err:
                    print('HTTP-Anfrage ungueltig:', type(err).__name__)
                finally:
                    client.close()
                if restart:
                    utime.sleep_ms(700)
                    machine.reset()
        finally:
            self.close()


def session(wifi, motor):
    irc = IRCClient(wifi, motor)
    if not irc.connect():
        motor.stop()
        wifi.tcp_close()
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
    wifi.tcp_close()
    print('IRC offline - Motor gesperrt')


def main():
    motor = Motor()
    try:
        migrate_old_wifi()
        wifi = PicoWiFi()
        creds = load_wifi()
        if creds is None:
            print('Keine WLAN-Zugangsdaten. Setup fuer', DEVICE_NAME)
            portal = SetupPortal()
            if portal.setup():
                portal.run()
            return
        print('ForgetMeNot v3:', DEVICE_NAME)
        while True:
            motor.stop()
            try:
                if wifi.connect_wifi(creds[0], creds[1]):
                    session(wifi, motor)
                else:
                    print('WLAN offline; erneuter Verbindungsversuch.')
            except Exception as err:
                print('Verbindungsfehler:', type(err).__name__, err)
            motor.stop()
            wifi.tcp_close()
            utime.sleep_ms(RETRY_MS)
    finally:
        motor.stop()


if __name__ == '__main__':
    main()

