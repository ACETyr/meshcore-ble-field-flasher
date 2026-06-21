# MeshCore BLE Field Flasher

*[English version → README.md](README.md)*

Ein netzwerkfreies, per Smartphone bedienbares Werkzeug zum Flashen von **MeshCore-RAK4631-Knoten
(nRF52840)** **über Bluetooth Low Energy** — auch für Knoten hoch oben an einem Mast ohne
IP-Anbindung vor Ort (kein WLAN, kein LTE, kein SSH am Standort).

Ausgangspunkt war, das iPhone als DFU-Werkzeug abzulösen (nRF Connect for Mobile unter iOS ist
quälend langsam und scheitert bei Legacy-DFU in etwa 4 von 5 Fällen). Herausgekommen ist ein kleines,
zuverlässiges Kit, das auf dem Laptop für den Werkbank-Betrieb und auf einem **Raspberry Pi Zero 2 W**
als mitgeführter, autarker Feld-Flasher mit Web-UI im Smartphone-Browser läuft.

> **Wichtig:** Geflasht wird der **Adafruit/OTAFix-Bootloader = Legacy-DFU** (App-only-`.zip`), also
> der Bootloader, mit dem MeshCore-RAK4631-Builds ausgeliefert werden. Das ist *nicht* Nordic Secure
> DFU und braucht *keinen* Nordic-Dongle — es nutzt das eingebaute Bluetooth des Hosts.

---

## Warum es das gibt

- **iOS ist der Flaschenhals.** nRF Connect unter iOS 17+ bleibt bei zufälligen Prozentwerten hängen
  (ein PRN-Standardwert-Bug) und unterbricht das DFU still, sobald der Bildschirm schläft. Bestfall
  ~2,7 kB/s, typischerweise scheitern 4 von 5 Versuchen.
- **Entfernte Standorte haben keine Anbindung.** Ein Repeater auf einem 20-m-Mast im Nirgendwo hat
  kein Internet — man kann sich nicht über das Netz per SSH auf einen Pi verbinden. Der Flasher muss
  zum Standort *getragen* und lokal bedient werden, komplett ohne Netz.
- **BLE hat geringe Reichweite.** Egal welcher Host — man muss physisch nahe am Knoten sein. Dieses
  Kit ist um diese Einschränkung herum gebaut, statt gegen sie anzukämpfen.

## Was enthalten ist

| Komponente | Funktion |
|---|---|
| `bench_flash.py` | Flasht einen Knoten über BLE und **misst** den Durchsatz (Jump → Bootloader → Upload → kB/s). |
| `recover_flash.py` | Direkt-in-den-Bootloader-Flash für einen bereits im DFU hängenden Knoten (z. B. nach abgebrochenem Flash). Ohne „Jump"-Schritt — der Weg zurück aus dem Brick-Verdacht. |
| `ble_rssi_probe.py` | Misst RSSI / Link-Reserve zum Ziel, **bevor** man einen mehrminütigen Flash riskiert. |
| `mc_serial.py` | Minimaler MeshCore-Serial-CLI-Helfer (`start ota` auslösen, `ver` / `public.key` lesen). |
| `webflash.py` | **Web-UI im Smartphone-Browser** für den Pi — Buttons für Flash / RSSI / Scan / Recover, eine Firmware-Bibliothek und ein unbeaufsichtigter **Drohnen-Modus**. Der Flash läuft serverseitig, ein schlafendes/geschlossenes Smartphone unterbricht ihn also nicht. |
| `setup.sh` & Co. | Einmalige Pi-Einrichtung: venv, automatisch einspringender Feld-WLAN-AP, Captive Portal, USB-NCM-Gadget, Watchdog. |

## Zielhardware

- **Knoten:** RAK4631 (Nordic nRF52840) mit MeshCore und Adafruit/OTAFix-Bootloader.
- **Host (Werkbank):** beliebiger Windows-/macOS-/Linux-Rechner mit eingebautem BLE.
- **Host (Feld):** Raspberry Pi Zero 2 W (das 2 W — *nicht* das alte Zero W; man will BT 4.2).

---

## Abhängigkeit: die DFU-Engine

Die eigentliche Legacy-DFU-Protokollimplementierung ist
**[recrof/nrf_dfu_py](https://github.com/recrof/nrf_dfu_py)**, ein `bleak`-basierter Client, der den
RAK4631 + Adafruit-Bootloader ausdrücklich als getestet auflistet. Die Skripte in diesem Repository
*umhüllen* sie (Orchestrierung, Messung, Recovery, Web-UI, Pi-Einrichtung) und erwarten sie als
Geschwisterordner `nrf_dfu_py/`. Sie ist hier **nicht mitgeliefert** — selbst klonen:

```sh
cd flasher
git clone --depth 1 https://github.com/recrof/nrf_dfu_py.git
```

Auf dem Pi klont `setup.sh` sie automatisch.

---

## Schnellstart — Werkbank (Laptop)

```sh
git clone https://github.com/ACETyr/meshcore-ble-field-flasher.git
cd meshcore-ble-field-flasher/flasher
git clone --depth 1 https://github.com/recrof/nrf_dfu_py.git
python -m venv venv && . venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r ../requirements.txt
```

1. Knoten in den OTA-Modus versetzen (sendet `RAK4631_OTA`):
   ```sh
   python mc_serial.py COM12 "start ota" 3        # Linux/Mac: /dev/ttyACM0
   ```
2. Flashen und messen:
   ```sh
   python bench_flash.py /pfad/zu/firmware.zip --retry 5 --verbose
   ```
3. Wenn ein Flash abbricht und der Knoten als `4631_DFU` hängen bleibt, ohne erneuten Jump recovern:
   ```sh
   python recover_flash.py /pfad/zu/firmware.zip --retry 8
   ```

Vollständige Werkbank-Anleitung: **[docs/BENCH.md](docs/BENCH.md)** (auf Englisch).

## Schnellstart — Feld (Raspberry Pi Zero 2 W)

1. Raspberry Pi OS Lite (64-bit) flashen, SSH aktivieren, Heim-WLAN für die *einmalige* Einrichtung
   hinterlegen.
2. Den Ordner `flasher/` auf den Pi kopieren und Setup ausführen (braucht **einmalig** Internet):
   ```sh
   scp -r flasher <user>@pi-flasher.local:~/flasher
   ssh <user>@pi-flasher.local "bash ~/flasher/setup.sh"
   ```
   Damit landet alles unter `/opt/flasher`, ein Web-UI-Dienst startet, und ein Feld-WLAN-AP wird
   konfiguriert, der **automatisch einspringt, sobald das Heim-WLAN außer Reichweite ist** (so lässt
   er sich ganz ohne Netz bedienen).
3. Im Feld: Pi mit Strom versorgen, dessen WLAN `pi-flasher` beitreten, `http://10.42.0.1` öffnen,
   flashen.

Vollständige Pi-Anleitung: **[docs/PI-SETUP.md](docs/PI-SETUP.md)** (auf Englisch).

### Web-UI & Drohnen-Modus

`http://10.42.0.1` (Feld-AP), `http://pi-flasher.local/` (zuhause) oder `http://10.55.0.1` (USB-Kabel)
öffnen.

- **Firmware-Bibliothek** — `.zip`-DFU-Images im Browser hochladen / auswählen / löschen (kein
  SSH/SCP).
- **Drohnen-Modus** — unbeaufsichtigtes, RSSI-gegatetes Auto-Flashen, wenn man den Pi an einer Stange
  oder Drohne neben dem Mast montiert und niemand am UI sitzt. Neustart-fester Arm-Zustand,
  automatisches Disarm-Timeout und eine Flash-Historie, die belegt, was angekommen ist.

---

## Durchsatz & Reichweite — realistische Erwartungen

- **~1,5 kB/s sind die geräteseitige Untergrenze** für dieses Legacy-DFU bei 20-Byte-MTU. Auf
  Windows und Pi nachweislich identisch — der Bootloader gibt das Verbindungsintervall vor, es ist
  also *nicht* hostseitig tunbar. Ein ~513-KB-Image dauert ~6 Minuten. Das ist in Ordnung: es läuft
  **unbeaufsichtigt**, 6 Minuten „hinstellen und weggehen" schlagen Stunden Smartphone-Babysitting.
- **Kein `--high-mtu`** beim OTAFix-Bootloader — das bricht die Übertragung ab. Aus lassen.
- **Zuerst RSSI prüfen** mit `ble_rssi_probe.py`. Der Worst-Case-RSSI sollte deutlich besser als
  −80 dBm sein. Ist ein Mastknoten vom Boden aus grenzwertig, kann ein leistungsstarker
  Richt-BLE-Adapter (z. B. ein 20-dBm-USB-Adapter + eine 18-dBi-2,4-GHz-Panelantenne, nach oben zum
  Mast ausgerichtet) die Strecke ohne Klettern überbrücken.

## Sicherheitshinweise

- Ein fehlgeschlagener Flash ist **kein Brick** — der OTAFix-Bootloader fällt zurück und sendet
  weiter `4631_DFU`; mit `recover_flash.py` über BLE wiederherstellen. Genau dieser Zustand wurde mit
  diesem Kit ohne physischen Zugriff erfolgreich recovert.
- **App-only-DFU erhält die Identität** (das Schlüsselpaar des Knotens im InternalFS). Dieses
  Repository liefert bewusst **keine** Firmware und **keine** Erase-/Recovery-Images mit — die zu
  flashende `.zip` bringt man selbst mit.
- Die Pi-Web-UI ist **nicht authentifiziert**: Das WLAN-AP-Passwort ist die einzige Zugangskontrolle.
  **Vor jeder Tour das Standard-AP-PSK ändern** (`flashme123` in `setup.sh`) und den AP als privat
  behandeln.

---

## Danksagung

- DFU-Engine: [recrof/nrf_dfu_py](https://github.com/recrof/nrf_dfu_py)
- [MeshCore](https://github.com/meshcore-dev/MeshCore) und das RAK4631-Hardware-Ökosystem
- Gebaut und im Feld getestet für ein selbst gehostetes MeshCore-Repeater-Netz.

## Lizenz

[MIT](LICENSE) © 2026 Christoph Eder. Nutzung auf eigene Gefahr — siehe Haftungsausschluss in den
Werkbank-Dokumenten.
