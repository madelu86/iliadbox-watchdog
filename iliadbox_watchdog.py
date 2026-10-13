
import asyncio
import os
import ipaddress
import logging
import logging.handlers
import urllib.request
import json
import sys
from freebox_api import Freepybox

# ============================================================
# CONFIGURAZIONI
# ============================================================

# APP ILIADBOX 
APP_DESC = {
    'app_id': 'it.madelu86.iliadbox_watchdog',
    'app_name': 'Iliadbox Watchdog',
    'app_version': '1.0.0',
    'device_name': 'Synology NAS'
}

# DISPOSITIVO DA MONITORARE (Robot apirapolvere Ecovacs)
TARGET_IP = os.getenv("TARGET_IP", "192.168.1.170")
TARGET_MAC = os.getenv("TARGET_MAC", "AA:BB:CC:DD:EE:FF")

# NOTIFICHE TELEGRAM 
TELEGRAM_TOKEN = "IL_TUO_TELEGRAM_TOKEN"
TELEGRAM_CHAT_ID = "IL_TUO_TELEGRAM_CHAT_ID"
# ======================================================================

LOG_FILE = "iliadbox_wifi_monitor.log.txt"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.TimedRotatingFileHandler(
            filename=LOG_FILE,
            when="midnight",
            interval=1,
            backupCount=7,
            encoding="utf-8"
        )
    ],
)
log = logging.getLogger("iliadbox-monitor")

async def send_telegram_notification(text):
    """Invia una notifica Telegram in modo asincrono sfruttando aiohttp."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or "IL_TUO_" in TELEGRAM_TOKEN:
        log.error("TELEGRAM_TOKEN o TELEGRAM_CHAT_ID invalidi")
        return
    
    import aiohttp
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": f"[Iliadbox Watchdog] {text}"
    }
    
    try:
        # Usa un timeout di 5 secondi per non bloccare lo script se Telegram non risponde
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    log.info("Notifica inviata con successo via Telegram.")
                else:
                    log.error(f"Errore risposta Telegram: {response.status}")
    except Exception as e:
        log.exception(f"Impossibile inviare la notifica Telegram: {e}")

def discover_iliadbox():
    """Interroga il router per ottenere il dominio HTTPS locale corretto e la porta."""
    try:
        # Questo endpoint risponde sempre in HTTP senza SSL ed e accessibile liberamente
        with urllib.request.urlopen("http://myiliadbox.iliad.it/api_version", timeout=3) as response:
            data = json.loads(response.read().decode())
            # Estrae il dominio protetto da certificato e la porta HTTPS configurata
            api_domain = data.get("api_domain")
            https_port = data.get("https_port", 443)
            return api_domain, https_port
    except Exception as e:
        log.exception(f"Impossibile rilevare i dettagli del router via HTTP: {e}")
        return None, None


def normalize_mac(value):
    """Normalizza un MAC address nel formato AA:BB:CC:DD:EE:FF."""
    if not value:
        return ""
    return value.strip().replace("-", ":").upper()


def validate_config():
    """Controlla i parametri prima di accedere alla Freebox (Iliadbox)."""
    try:
        ipaddress.ip_address(TARGET_IP)
    except ValueError as exc:
        raise ValueError(f"TARGET_IP non valido: {TARGET_IP}") from exc

    mac = normalize_mac(TARGET_MAC)
    parts = mac.split(":")
    if len(parts) != 6:
        raise ValueError(f"TARGET_MAC non valido: {TARGET_MAC}")

    try:
        if any(len(p) != 2 for p in parts):
            raise ValueError
        if any(not 0 <= int(p, 16) <= 255 for p in parts):
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"TARGET_MAC non valido: {TARGET_MAC}") from exc

    if mac == "AA:BB:CC:DD:EE:FF":
        raise ValueError("Configura TARGET_MAC con il MAC reale del dispositivo.")

    return mac


def host_ips(host):
    """Estrae gli indirizzi IP dai dati host restituiti dall'API."""
    result = set()

    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in ("ip", "addr", "address") and isinstance(value, str):
                    try:
                        result.add(str(ipaddress.ip_address(value)))
                    except ValueError:
                        pass
                else:
                    walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(host)
    return result

async def get_wifi_data(fbx, target_ip: str, target_mac: str) -> dict:
    """
    Legge lo stato Wi-Fi senza modificare la configurazione.

    Restituisce:
      - ap_id: ID dell'access point a 2,4 GHz
      - target_connected: True solo se IP e MAC coincidono
      - other_clients: altri client associati alla stessa banda
      - station_count: numero di stazioni rilevate
      - safe_to_consider_restart: True solo se non ci sono client

    In caso di dati incompleti o ambigui, solleva un'eccezione:
    il chiamante deve quindi evitare qualsiasi modifica.
    """

    target_ip = target_ip.strip()
    target_mac = target_mac.strip().upper()

    if not target_ip or not target_mac:
        raise ValueError("TARGET_IP e TARGET_MAC devono essere configurati.")

    # 1. Recupera gli access point disponibili.
    aps = await fbx.wifi.get_ap_list()

    if isinstance(aps, dict):
        aps = aps.get("result")

    if not isinstance(aps, list):
        raise RuntimeError("Risposta non valida da get_ap_list().")

    # 2. Identifica l'access point a 2,4 GHz.
    wifi_24ghz = []

    for ap in aps:
        config = ap.get("config") or {}
        band = str(config.get("band", ap.get("band", ""))).lower()
        name = str(ap.get("name", "")).lower()

        if band in {"2d4g", "2.4g", "2g4"} or name in {
            "2.4g", "2.4 ghz", "2,4g", "2,4 ghz"
        }:
            wifi_24ghz.append(ap)

    if len(wifi_24ghz) != 1:
        raise RuntimeError(
            "Impossibile identificare univocamente l'access point a 2,4 GHz."
        )

    ap = wifi_24ghz[0]
    ap_id = ap.get("id")
    config = ap.get("config") or {}
    status = ap.get("status") or {}

    if ap_id is None:
        raise RuntimeError("L'access point a 2,4 GHz non ha un ID valido.")

    if config.get("enabled") is not True:
        raise RuntimeError("L'access point a 2,4 GHz risulta disabilitato.")

    if status.get("state") != "active":
        raise RuntimeError("Lo stato dell'access point a 2,4 GHz non è attivo.")

    # 3. Legge i client associati esclusivamente a questo access point.
    stations = await fbx.wifi.get_station_list(ap_id)

    if isinstance(stations, dict):
        stations = stations.get("result")

    if not isinstance(stations, list):
        raise RuntimeError("Risposta non valida da get_station_list().")

    target_connected = False
    other_clients = []

    for station in stations:
        if not isinstance(station, dict):
            raise RuntimeError("Record client Wi-Fi non valido.")

        host = station.get("host") or {}
        l2ident = host.get("l2ident") or {}

        mac = str(
            station.get("mac")
            or l2ident.get("id")
            or ""
        ).strip().upper()

        # Gli indirizzi IP possono essere annidati in host.l3connectivities.
        ip_addresses = set()

        for connection in host.get("l3connectivities") or []:
            if not isinstance(connection, dict):
                continue

            address = connection.get("addr")
            if address and connection.get("active") is True:
                ip_addresses.add(str(address))

        # Alcune risposte possono esporre un IP direttamente nel record host.
        for key in ("ip", "ipv4"):
            address = host.get(key)
            if address:
                ip_addresses.add(str(address))

        # Il target è riconosciuto solo se IP E MAC corrispondono.
        if mac == target_mac and target_ip in ip_addresses:
            target_connected = True
        else:
            # Qualsiasi altro record conta come client presente.
            # In caso di dati incompleti, non consideriamo la banda libera.
            other_clients.append({
                "mac": mac or "unknown",
                "hostname": (
                    station.get("hostname")
                    or host.get("primary_name")
                    or host.get("id")
                    or "unknown"
                ),
            })

    return {
        "ap_id": ap_id,
        "target_connected": target_connected,
        "other_clients": other_clients,
        "station_count": len(stations),
        "safe_to_consider_restart": (
            not target_connected and len(other_clients) == 0
        ),
    }

async def main():
    fbx = Freepybox(app_desc=APP_DESC, token_file="/app/iliadbox_watchdog.conf", api_version="latest")
    try:
        # Rilevamento dinamico delle credenziali di rete locale del router
        host, port = discover_iliadbox()
        if not host or not port:
            log.error("Errore: Impossibile contattare l'Iliadbox sulla rete locale. Controllo annullato.")
            await send_telegram_notification("Errore - Impossibile contattare l'Iliadbox sulla rete locale.")
            return
        
        log.info("Connessione all'Iliadbox: %s, porta: %s", host, port)
        await fbx.open(host=host, port=port)

        log.info("Autenticazione completata.")
        target_mac = validate_config()
        log.info("Dispositivo monitorato: IP=%s MAC=%s", TARGET_IP, target_mac)

        # Lettura della configurazione del router.
        system = await fbx.system.get_config()
        log.info("Router rilevato: %s . Firmware: %s", system.get("model_info", {}).get("pretty_name", "sconosciuto"), system.get("firmware_version", "sconosciuto"))

        wifi_data = await get_wifi_data(fbx, TARGET_IP, target_mac,)
        
        if wifi_data["target_connected"]:
            logging.info("Dispositivo target connesso a 2,4 GHz: nessuna azione.")
        
        elif wifi_data["other_clients"]:
            logging.info(
                "Target assente, ma sono presenti %d altri client a 2,4 GHz: "
                "nessuna azione.",
                len(wifi_data["other_clients"]),
            )
            await send_telegram_notification(f"Attenzione, Ecovacs risulta disconnesso, ma ci sono altri dispositivi connessi.")
        
        else:
            logging.warning(
                "Nessun client rilevato a 2,4 GHz. "
                "Verificare la procedura di ripristino prima di eseguirla."
            )
            # Riavvio automatico del router disabilitato, controlliamo la notifica Telegram prima
            #await fbx.system.reboot()
            await send_telegram_notification(f"Controllare la banda Wi-Fi 2.4 GHz, nessun dispositivo risulta collegato.")

    except NotImplementedError as exc:
        log.error("%s", exc)
        sys.exit_code = 2

    except (OSError, TimeoutError) as exc:
        log.exception("Errore di rete: %s", exc)
        sys.exit_code = 1

    except Exception as exc:
        log.exception("Errore durante il monitoraggio: %s", exc)
        sys.exit_code = 1

    finally:
        try:
            await fbx.close()
            log.info("Sessione Iliadbox chiusa.")
        except Exception:
            log.warning("Non è stato possibile chiudere la sessione.")

if __name__ == "__main__":
    asyncio.run(main())
