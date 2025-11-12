from typing import Any, Dict
import yaml
import json
import requests
import os
from models import (
    get_electricity_price,
    get_electricity_load,
    get_tuv_demand,
    get_fve_forecast,
    get_estimate_heating_losses,
    get_temperature_forecast,
    fve_forecast_for_slots,
)

from powerplan_environment import CREDENTIALS_FILE, OPTIONS_FILE

TOKEN = ""

try:
    with open(OPTIONS_FILE, "r") as f:
        print(f"Using credentials from {OPTIONS_FILE}")
        opts = json.load(f)
        TOKEN = opts.get("token", "")
        HA_URL = opts.get("ha_url", "")
except:
    pass


if not TOKEN:
    HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
    TOKEN = os.environ.get("HASSIO_TOKEN", None)


if not TOKEN:
    try:
        print(f"Using credentials from {CREDENTIALS_FILE}")
        with open(CREDENTIALS_FILE, "r") as f:
            credentials = yaml.safe_load(f)
            HA_URL = credentials["url"]
            TOKEN = credentials["token"]
    except (FileNotFoundError, yaml.YAMLError, ValueError, KeyError) as e:
        print(f"Error reading {CREDENTIALS_FILE}:", e)
        with open(CREDENTIALS_FILE, "w") as f:
            yaml.dump({"url": HA_URL, "token": "your_token"}, f)
            print(f"Created {CREDENTIALS_FILE} with default values. Please update it.")
            exit(1)


HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
}

# --- Pomocné funkce -------------------------------------------------------

def get_ha_states():
    response = requests.get(f"{HA_URL}/api/states", headers=HEADERS, timeout=10)
    response.raise_for_status()
    return response.json()

def get_entity(state_list, entity_id, default=0.0):
    for e in state_list:
        if e["entity_id"] == entity_id:
            try:
                return float(e["state"])
            except ValueError:
                return default
    return default

def prepare_data():
    states = get_ha_states()

    # --- předpovědi a ceny -------------------------------------------------
    buy_raw = get_electricity_price(states, "sensor.current_buy_electricity_price_15min")
    sell_raw = get_electricity_price(states, "sensor.current_sell_electricity_price_15min")

    slots = [h for h, _ in buy_raw]

    fve_raw = get_fve_forecast(states, "sensor.solcast_pv_forecast_forecast_today")
    fve_raw.extend(
        get_fve_forecast(states, "sensor.solcast_pv_forecast_forecast_tomorrow")
    )

    horizon = len(slots)

    fve_pred = fve_forecast_for_slots(fve_raw, slots)
    buy_price = [v for _, v in buy_raw][:horizon]
    sell_price = [v for _, v in sell_raw][:horizon]

    outdoor_forecast = get_temperature_forecast(slots)
    outdoor_temps = [temp for _, temp in outdoor_forecast]
    print(f'Outdoor temps: {outdoor_temps}')
    print(f"Preparing data for horizon of {horizon} slots., len(outdoor_temps)={len(outdoor_temps)}, len(fve_pred)={len(fve_pred)}")

    bat_soc = get_entity(states, "sensor.solax_battery_capacity", 50)
    boiler_E = get_entity(states, "sensor.tepelnaakumulace_energie_n_dr_e", 25.0)

    boiler_top = get_entity(states, "sensor.tepelnaakumulace_horn_senzor", 45.0)
    boiler_middle = get_entity(states, "sensor.tepelnaakumulace_st_edn_senzor", 45.0)
    boiler_bottom = get_entity(states, "sensor.tepelnaakumulace_spodn_senzor", 30.0)

    temp_upper = boiler_top * 0.5 + boiler_middle * 0.5
    temp_lower = boiler_middle * 0.25 + boiler_bottom * 0.75

    tuv_demand = [get_tuv_demand(h) for h in slots]
    heating_demand = [get_estimate_heating_losses(t) for t in outdoor_temps]
    lod_pred = [get_electricity_load(h) for h in slots]

    return {
        "slots": slots,
        "tuv_demand": tuv_demand,
        "heating_demand": heating_demand,
        "fve_pred": fve_pred,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "load_pred": lod_pred,
        "bat_soc": bat_soc,
        "boiler_E": boiler_E,
        "outdoor_temps": outdoor_temps,
        "temp_upper": temp_upper,
        "temp_lower": temp_lower,
    }

def publish_to_ha(payload: Dict[str, Any], prefix: str = "powerplan_", attributes = None, extra = None) -> None:
    """
    Publikuje všechny dvojice {key: value} do Home Assistantu jako entity
    'sensor.<prefix><key>' (Stringify výsledek kvůli state API).

    Parameters
    ----------
    payload : dict
        Slovník s klíči a hodnotami (např. výstup z powerplan_to_actions()).
    prefix : str, optional
        Předpona názvu entity, defaultně 'powerplan_'.

    Raises
    ------
    requests.HTTPError
        Když HA vrátí stavový kód >= 400.
    """
    for key, value in payload.items():
        entity_id = f"sensor.{prefix}{key}"
        attr = attributes.get(key, {}) if attributes else {}
        if extra:
            attr.update(extra)
        resp = requests.post(
            f"{HA_URL}/api/states/{entity_id}",
            headers=HEADERS,
            data=json.dumps({
                "state": str(value),
                "attributes": attr
            })
        )
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            print(f"[ERR] {entity_id} → {resp.status_code}: {resp.text}")
            raise e
        else:
            print(f"[OK ] {entity_id} = {value}")

# ---------------------------------------------------------------------------
# Příklad použití:
#
# from powerplan_to_actions import powerplan_to_actions
# solution = ...  # výstup z run_powerplan_optimizer
# actions   = powerplan_to_actions(solution)
# publish_to_ha(actions)        # pošle vše jako sensor.powerplan_<key>