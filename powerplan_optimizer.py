#!/usr/bin/env python3

"""Linear model predictive optimizer for household energy management

Arguments are grouped into three dictionaries to keep the call‑site tidy and
self‑explanatory:

* ``series`` – every *vector* that spans the optimisation horizon (one value
  per timestep):

  - ``tuv_demand`` (\[kWh\])
  - ``heating_demand`` (\[kWh\])
  - ``fve`` – photovoltaic production (\[kW\])
  - ``buy`` – grid purchase prices (\[Kč / kWh\])
  - ``sell`` – feed‑in tariffs (\[Kč / kWh\])
  - ``load`` – household base load (\[kW\])

* ``initials`` – *scalars* that describe the initial state of storage systems:

  - ``soc_bat`` – battery state‑of‑charge at *t = 0* (\[kWh\])
  - ``soc_boiler`` – boiler energy content at *t = 0* (\[kWh\])

* ``options`` – miscellaneous switches **and** parameter overrides.  Unknown
  keys are ignored.

  Boolean knobs:
  ~~~~~~~~~~~~~~
  - ``heating_enabled`` – include space‑heating demand if **True** (default
    **False**)

  Numeric parameter overrides (all optional – defaults shown):
  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
  - ``B_CAP``          = 17.4   – battery capacity (kWh)
  - ``B_MIN``          = B_CAP*0.15 – minimum SOC (kWh)
  - ``B_MAX``          = B_CAP  – maximum SOC (kWh)
  - ``B_POWER``        = 9      – battery (dis)charge limit (kW)
  - ``B_EFF_IN``       = 0.94   – charging efficiency (–)
  - ``B_EFF_OUT``      = 0.94   – discharging efficiency (–)
  - ``H_CAP``          = 81.0   – boiler capacity (kWh)
  - ``H_POWER``        = 12     – electric heater limit (kW)
  - ``GRID_LIMIT``     = 18     – main breaker (kW)
  - ``INVERTER_LIMIT`` = 15     – PV inverter export limit (kW)

* ``dt`` – optional list of time step lengths [h], one per timestep. Default: 0.25 (15 minutes).

This structure eliminates a long positional parameter list and makes it clear
which values belong in which category – future options can be added without
breaking the API.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence, Mapping, Any, List, Dict
from pulp import LpProblem, LpMinimize, LpVariable, lpSum, LpStatusOptimal
import logging

from models.tank_losses import estimate_heating_losses
from options import VARIABLES_SPEC, get_option

# Nastavení základního logování
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

def debug(msg):
    logging.info(msg)

def clamp(value, min_value, max_value):
    """Omezí hodnotu na zadaný rozsah."""
    return max(min_value, min(value, max_value))

def temp_to_energy(temp: float, volume: float, ref_temp: float) -> float:
    """Převádí teplotu vody na energii v kWh.

    Voda má tepelnou kapacitu přibližně 4.181 kJ/(kg·K) a hustotu 1000 kg/m³.
    """
    # Přepočet teploty na energii (kWh)
    # Zahrnutí hustoty vody 1000 kg/m³
    return (temp - ref_temp) * volume * 1000 * 4.181 / 3600  # Převod z kJ na kWh

def energy_to_temp(energy: float, volume: float, ref_temp: float) -> float:
    """Převádí energii v kWh na teplotu vody.

    Voda má tepelnou kapacitu přibližně 4.181 kJ/(kg·K) a hustotu 1000 kg/m³.
    """
    # Přepočet energie na teplotu (°C)
    # Zahrnutí hustoty vody 1000 kg/m³
    return energy * 3600 / (volume * 1000 * 4.181) + ref_temp  # Převod z kWh na °C

def run_mpc_optimizer(
    series: Mapping[str, Sequence[float]],
    initials: Mapping[str, float],
    slots: Sequence[datetime],
    options: Mapping[str, Any] | None = None,
    dt: Sequence[float] | None = None,
) -> Dict[str, Dict[str, List[float]]]:
    debug(f"run_mpc_optimizer called with options: {options}")
    debug(f"series keys: {list(series.keys())}")
    debug(f"initials: {initials}")

    options = options or {}
    indexes = range(len(slots))
    if dt is None:
        dt = [0.25] * len(slots)  # Default: 15 minutové kroky

    # Kontext pro odvozené hodnoty
    context = {}
    context["buy_price"] = series["buy_price"]

    heating_enabled = get_option(options, "heating_enabled")
    charge_bat_min = get_option(options, "charge_bat_min")
    b_cap = get_option(options, "b_cap")
    b_min = get_option(options, "b_min", context=context)
    b_max = get_option(options, "b_max", context=context)
    b_power_max = get_option(options, "b_power")
    b_eff_in = get_option(options, "b_eff_in")
    b_eff_out = get_option(options, "b_eff_out")
    grid_limit = get_option(options, "grid_limit")
    inverter_limit = get_option(options, "inverter_limit")
    final_boiler_price = get_option(options, "final_boiler_price", context=context)
    bat_threshold_pct = get_option(options, "bat_threshold_pct")
    bat_price_below = get_option(options, "bat_price_below", context=context)
    bat_price_above = get_option(options, "bat_price_above", context=context)
    battery_penalty = get_option(options, "battery_penalty")
    fve_unused_penalty = get_option(options, "fve_unused_penalty")
    water_priority_bonus = get_option(options, "water_priority_bonus")
    upper_zone_priority = get_option(options, "upper_zone_priority")

    # Dvou-zónový model nádrže: dolní (700L, 8kW) a horní (300L, 4kW)
    h_lower_power = get_option(options, "h_lower_power", context={})  # 8
    h_upper_power = get_option(options, "h_upper_power", context={})  # 4
    # Pasivní přenos tepla mezi zónami: koeficient alpha
    alpha = get_option(options, "alpha", context=context)

    h_lower_min_t = get_option(options, "h_lower_min_t", context={})  # minimální teplota dolní zóny [°C]
    h_lower_max_t = get_option(options, "h_lower_max_t", context={})  # maximální teplota dolní zóny [°C]
    h_upper_min_t = get_option(options, "h_upper_min_t", context={})  # minimální teplota horní zóny [°C]
    h_upper_max_t = get_option(options, "h_upper_max_t", context={})  # maximální teplota horní zóny [°C]

    h_upper_vol = get_option(options, "h_upper_vol", context={})  # objem horní zóny [m³]
    h_lower_vol = get_option(options, "h_lower_vol", context={})  # objem dolní zóny [m³]

    h_lower_cap = temp_to_energy(h_lower_max_t, h_lower_vol, h_lower_min_t)  # kapacita dolní zóny [kWh]
    h_upper_cap = temp_to_energy(h_upper_max_t, h_upper_vol, h_upper_min_t)  # kapacita horní zóny [kWh]

    tuv_demand = series["tuv_demand"]
    heating_demand = series["heating_demand"]
    fve_pred = series["fve_pred"]
    buy_price = series["buy_price"]
    sell_price = series["sell_price"]
    load_pred = series["load_pred"]

    soc_bat_init = clamp(initials["bat_soc"] / 100 * b_cap, b_min, b_max)
    
    # Správná inicializace SOC pro zóny - pokud je teplota pod minimem, použijeme 0
    # Pokud je nad minimem, spočítáme energii relativně k minimu
    if initials["temp_lower"] < h_lower_min_t:
        soc_lower_init = 0
    else:
        soc_lower_init = clamp(temp_to_energy(initials["temp_lower"], h_lower_vol, h_lower_min_t), 0, h_lower_cap)
    
    if initials["temp_upper"] < h_upper_min_t:
        soc_upper_init = 0
    else:
        soc_upper_init = clamp(temp_to_energy(initials["temp_upper"], h_upper_vol, h_upper_min_t), 0, h_upper_cap)
    
    debug(f"Initial temps: lower={initials['temp_lower']}°C, upper={initials['temp_upper']}°C")
    debug(f"Min temps: lower={h_lower_min_t}°C, upper={h_upper_min_t}°C")
    debug(f"soc_bat_init={soc_bat_init}, soc_lower_init={soc_lower_init}, soc_upper_init={soc_upper_init}")
    debug(f"h_lower_cap={h_lower_cap}, h_upper_cap={h_upper_cap}, h_lower_vol={h_lower_vol}, h_upper_vol={h_upper_vol}")

    prob = LpProblem("EnergyMPC", LpMinimize)

    # Přejmenování všech stavových proměnných a výstupů na lower_snake_case
    b_power = LpVariable.dicts("b_power", indexes, -b_power_max, b_power_max)
    b_soc = LpVariable.dicts("b_soc", indexes, b_min, b_max)
    b_charge = LpVariable.dicts("b_charge", indexes, 0, b_power_max)
    b_discharge = LpVariable.dicts("b_discharge", indexes, 0, b_power_max)
    h_in_lower = LpVariable.dicts("h_in_lower", indexes, 0, h_lower_power)
    h_in_upper = LpVariable.dicts("h_in_upper", indexes, 0, h_upper_power)
    h_soc_lower = LpVariable.dicts("h_soc_lower", indexes, 0, h_lower_cap)
    h_soc_upper = LpVariable.dicts("h_soc_upper", indexes, 0, h_upper_cap)
    h_out_lower = LpVariable.dicts("h_out_lower", indexes, 0)
    h_out_upper = LpVariable.dicts("h_out_upper", indexes, 0)
    # Heat flow from lower to upper (can be negative for reverse flow)
    h_to_upper = LpVariable.dicts("h_to_upper", indexes)
    # Pomocná proměnná pro kladnou část rozdílu teplot mezi zónami
    h_delta_temp = LpVariable.dicts("h_delta_temp", indexes, 0)

    # Definice proměnných pro nákup, prodej a nevyužitou PV
    g_buy = LpVariable.dicts("g_buy", indexes, 0)
    g_sell = LpVariable.dicts("g_sell", indexes, 0)
    fve_unused = LpVariable.dicts("fve_unused", indexes, 0)

    # Penalizace za SOC pod prahem v každém kroku
    bat_under_penalty = get_option(options, "bat_under_penalty")
    bat_threshold = bat_threshold_pct * b_cap
    b_soc_under = LpVariable.dicts("b_soc_under", indexes, 0)
    for t in indexes:
        prob += b_soc_under[t] >= bat_threshold - b_soc[t]
        prob += b_soc_under[t] >= 0

    threshold = bat_threshold_pct * b_cap
    t_end = max(indexes)
    b_short = LpVariable("b_short", 0, threshold)
    b_surplus = LpVariable("b_surplus", 0, b_cap - threshold)
    prob += b_short + b_surplus == b_soc[t_end]

    for t in indexes:
        prob += b_power[t] == b_charge[t] - b_discharge[t]

    # Parametry pro ocenění energie v nádrži v konkrétní hodinu
    tank_value_hour = get_option(options, "tank_value_hour")
    tank_value_bonus = get_option(options, "tank_value_bonus")  # Kč/kWh
    tank_value_indexes = [i for i, slot in enumerate(slots) if slot.hour == tank_value_hour]

    prob += (
        lpSum(
            (g_buy[t] * buy_price[t]
             - g_sell[t] * sell_price[t]
             + battery_penalty * b_discharge[t]
             + fve_unused_penalty * fve_unused[t]
             - water_priority_bonus * (h_in_lower[t] + h_in_upper[t])
             - upper_zone_priority * h_in_upper[t]
             + bat_under_penalty * b_soc_under[t]) * dt[t]
            for t in indexes
        )
        - bat_price_above * b_surplus
        + bat_price_below * (threshold - b_short)
        - final_boiler_price * (h_soc_lower[t_end] + h_soc_upper[t_end])
        - upper_zone_priority * h_soc_upper[t_end]
        - tank_value_bonus * lpSum(h_soc_upper[t] for t in tank_value_indexes)
    )

    print(
        len(fve_pred),
        len(g_buy),
        len(b_discharge),
        len(load_pred),
        len(b_charge),
        len(h_in_lower),
        len(h_in_upper),
        len(g_sell),
        len(fve_unused)
    )


    # Unified two-zone boiler constraints
    for t in indexes:
        # Battery SOC dynamics
        if t == 0:
            prob += b_soc[t] == soc_bat_init + (b_charge[t] * b_eff_in - b_discharge[t] / b_eff_out) * dt[t]
        else:
            prob += b_soc[t] == b_soc[t-1] + (b_charge[t] * b_eff_in - b_discharge[t] / b_eff_out) * dt[t]

        # Outputs from boiler zones
        prob += h_out_upper[t] == tuv_demand[t]
        prob += h_out_lower[t] == (heating_demand[t] if heating_enabled else 0)

        # Parazitní ztráty nyní z obou patron
        parasitic_water_heating = get_option(options, "parasitic_water_heating")
        parasitic_energy = parasitic_water_heating * (h_in_lower[t] + h_in_upper[t])

        # Energetická bilance s oběma patronami
        prob += (
            fve_pred[t] + g_buy[t] + b_discharge[t] * b_eff_out ==
            load_pred[t] + b_charge[t] / b_eff_in + (h_in_lower[t] + h_in_upper[t] + parasitic_energy) + g_sell[t] + fve_unused[t]
        )

        # Heat transfer lower->upper based on energy difference (linearized approach)
        # Since temperature is proportional to energy/volume, we can use SOC directly
        # Convert alpha from temperature-based to energy-based coefficient
        
        if t == 0:
            lower_soc_available = soc_lower_init
            upper_soc_available = soc_upper_init
            # Use previous SOC for heat transfer calculation
            lower_soc_for_transfer = soc_lower_init
            upper_soc_for_transfer = soc_upper_init
        else:
            lower_soc_available = h_soc_lower[t-1]
            upper_soc_available = h_soc_upper[t-1]
            # Use previous SOC for heat transfer calculation
            lower_soc_for_transfer = h_soc_lower[t-1]
            upper_soc_for_transfer = h_soc_upper[t-1]
        
        # Linearized heat transfer based on energy difference
        # Convert temperature difference to energy difference for linear model
        # alpha_energy adjusts the original alpha coefficient for energy-based calculation
        alpha_energy = alpha * 3600 / 4181  # Convert from temperature-based to energy-based
        
        # Normalize by volume to account for different zone sizes
        lower_energy_density = lower_soc_for_transfer / h_lower_vol if h_lower_vol > 0 else 0
        upper_energy_density = upper_soc_for_transfer / h_upper_vol if h_upper_vol > 0 else 0
        
        # Heat transfer proportional to energy density difference
        prob += h_to_upper[t] == alpha_energy * (lower_energy_density - upper_energy_density)
        
        # Physical constraints: heat transfer cannot exceed available energy in source zone
        # When heat flows from lower to upper (h_to_upper[t] > 0)
        prob += h_to_upper[t] <= lower_soc_available
        # When heat flows from upper to lower (h_to_upper[t] < 0)
        prob += h_to_upper[t] >= -upper_soc_available
        

        # Lower zone SOC dynamics
        loss_lower = estimate_heating_losses(
            (h_soc_lower[t - 1] if t > 0 else soc_lower_init ),
            h_lower_cap, T_ambient=20, cirk_time=0.3
        )

        loss_upper = estimate_heating_losses(
            (h_soc_upper[t - 1] if t > 0 else soc_upper_init),
             h_upper_cap, T_ambient=20, cirk_time=0.3
        )

        # Zone SOC dynamics - physically correct model
        # Lower zone: heated by lower heater, loses heat through transfer to upper zone and direct output
        # Upper zone: heated by upper heater, gains heat from lower zone, loses heat through output
        
        if t == 0:
            prob += h_soc_lower[t] == soc_lower_init + (h_in_lower[t] - h_to_upper[t] - h_out_lower[t] - loss_lower) * dt[t]
            prob += h_soc_upper[t] == soc_upper_init + (h_in_upper[t] + h_to_upper[t] - h_out_upper[t] - loss_upper) * dt[t]
        else:
            prob += h_soc_lower[t] == h_soc_lower[t - 1] + (h_in_lower[t] - h_to_upper[t] - h_out_lower[t] - loss_lower) * dt[t]
            prob += h_soc_upper[t] == h_soc_upper[t - 1] + (h_in_upper[t] + h_to_upper[t] - h_out_upper[t] - loss_upper) * dt[t]

        # Heater power limits and grid/inverter constraints
        prob += h_in_lower[t] <= h_lower_power
        prob += h_in_upper[t] <= h_upper_power
        prob += g_buy[t] + b_charge[t] + (h_in_lower[t] + h_in_upper[t]) <= grid_limit
        prob += b_discharge[t] + (h_in_lower[t] + h_in_upper[t]) + g_sell[t] <= inverter_limit

    # Dynamické omezení SOC baterie podle plánu ohřevu nádrže
    for t in indexes:
        if heating_demand[t] > 0 or tuv_demand[t] > 0:
            prob += b_soc[t] <= b_cap * 0.9
        else:
            prob += b_soc[t] <= b_cap
        prob += b_soc[t] >= b_min
        # BEGIN CLEANUP: uprava pro dvouzónovy model
        # Pokud je baterie pod 60 %, neohříváme vodu
        if charge_bat_min:
            prob += (h_in_lower[t] + h_in_upper[t]) <= (h_lower_power + h_upper_power) * (b_soc[t] >= b_cap * 0.6)
        # END CLEANUP

    prob.solve()

    if prob.status != LpStatusOptimal:
        raise RuntimeError("Optimal solution not found – model infeasible")

    outputs = {
        "b_power": [b_power[t].varValue for t in indexes],
        "b_charge": [b_charge[t].varValue for t in indexes],
        "b_discharge": [b_discharge[t].varValue for t in indexes],
        "b_soc": [b_soc[t].varValue for t in indexes],
        "b_soc_percent": [int(100 * b_soc[t].varValue / b_cap) for t in indexes],
        "g_buy": [g_buy[t].varValue for t in indexes],
        "g_sell": [g_sell[t].varValue for t in indexes],
        "buy_cost": [g_buy[t].varValue * buy_price[t] for t in indexes],
        "sell_income": [g_sell[t].varValue * sell_price[t] for t in indexes],
        "net_step_cost": [g_buy[t].varValue * buy_price[t] - g_sell[t].varValue * sell_price[t] for t in indexes],
        "fve_unused": [fve_unused[t].varValue for t in indexes],
        # nové průběhy dvou-zónové nádrže
        "h_in_lower": [h_in_lower[t].varValue for t in indexes],
        "h_in_upper": [h_in_upper[t].varValue for t in indexes],
        "h_out_lower": [h_out_lower[t].varValue for t in indexes],
        "h_out_upper": [h_out_upper[t].varValue for t in indexes],
        "h_soc_lower": [h_soc_lower[t].varValue for t in indexes],
        "h_soc_upper": [h_soc_upper[t].varValue for t in indexes],
        "h_soc_upper_percent": [100*h_soc_upper[t].varValue/h_upper_cap for t in indexes],
        "h_soc_lower_percent": [100*h_soc_lower[t].varValue/h_lower_cap for t in indexes],
        "h_to_upper": [h_to_upper[t].varValue for t in indexes],
        # Teploty dolní a horní zóny [°C]
        "temp_lower": [energy_to_temp(h_soc_lower[t].varValue, h_lower_vol, h_lower_min_t) for t in indexes],
        "temp_upper": [energy_to_temp(h_soc_upper[t].varValue, h_upper_vol, h_upper_min_t) for t in indexes],
    }

    results = {}
    results["grid_consumption"] = sum(outputs["g_buy"])  # Celková spotřeba z gridu
    results["grid_injection"] = sum(outputs["g_sell"])  # Celková
    results["total_buy_cost"] = sum(outputs["buy_cost"])
    results["total_sell_income"] = sum(outputs["sell_income"])
    results["net_bilance"] = sum(outputs["net_step_cost"])
    results["total_charged"] = sum(outputs["b_charge"])  # Total energy charged to the battery
    results["total_discharged"] = sum(outputs["b_discharge"])  # Total energy discharged from the battery
    results["total_battery_penalty"] = sum(b_discharge[t].varValue * battery_penalty * dt[t] for t in indexes)
    results["total_fve_unused_penalty"] = sum(fve_unused[t].varValue * fve_unused_penalty * dt[t] for t in indexes)
    results["total_bat_price_above"] = bat_price_above * (b_surplus.varValue if hasattr(b_surplus, 'varValue') else 0)
    results["total_bat_price_below"] = bat_price_below * (threshold - (b_short.varValue if hasattr(b_short, 'varValue') else 0))
    # Celková hodnota energii v obou zónách na konci
    results["total_final_boiler_value"] = final_boiler_price * (
        (h_soc_lower[t_end].varValue + h_soc_upper[t_end].varValue) if hasattr(h_soc_lower[t_end], 'varValue') and hasattr(h_soc_upper[t_end], 'varValue') else 0
    )
    # Bonus za energii v horní zóně na konci
    results["final_upper_zone_bonus"] = upper_zone_priority * (
        h_soc_upper[t_end].varValue if hasattr(h_soc_upper[t_end], 'varValue') else 0
    )
    results["total_fve_unused"] = sum(fve_unused[t].varValue * dt[t] for t in indexes)
    # Bonifikace za ohřev v obou zónách
    results["total_water_priority_bonus"] = sum(
        water_priority_bonus * (h_in_lower[t].varValue + h_in_upper[t].varValue) * dt[t]
        for t in indexes
    )
    # Bonus za prioritní ohřev horní zóny
    results["total_upper_zone_priority"] = sum(
        upper_zone_priority * h_in_upper[t].varValue * dt[t]
        for t in indexes
    )
    results["total_battery_under_penalty"] = sum(b_soc_under[t].varValue * bat_under_penalty * dt[t] for t in indexes)
    # Bonus hodnoty tepla v obou zónách ve vybraných hodinách
    results["tank_value_bonus"] = sum(
        (h_soc_lower[t].varValue + h_soc_upper[t].varValue) * tank_value_bonus for t in tank_value_indexes
    )
    results["objective_value"] = prob.objective.value() if prob.objective is not None else None

    debug(f"b_cap: {b_cap}, b_min: {b_min}, b_max: {b_max}, h_lower_cap: {h_lower_cap}, h_upper_cap: {h_upper_cap}")
    debug(f"outputs keys: {list(outputs.keys())}")
    debug(f"results: {results}")

    # Výpočet celkové parazitní energie při ohřevu vody a její rozdělení podle SOC baterie (ex-post)
    total_parasitic_energy = 0.0
    total_parasitic_to_battery = 0.0
    total_parasitic_to_grid = 0.0
    for t in indexes:
        parasitic_water_heating = get_option(options, "parasitic_water_heating")
        pe = parasitic_water_heating * (outputs["h_in_lower"][t] + outputs["h_in_upper"][t]) * dt[t]
        total_parasitic_energy += pe
        # Rozdělení podle skutečného SOC baterie
        if outputs["b_soc"][t] < b_cap:
            total_parasitic_to_battery += pe
        else:
            total_parasitic_to_grid += pe
    results["total_parasitic_energy"] = total_parasitic_energy
    results["total_parasitic_to_battery"] = total_parasitic_to_battery
    results["total_parasitic_to_grid"] = total_parasitic_to_grid

    return {
        "generated_at": datetime.now().isoformat(),
        "times": [slot.isoformat() for slot in slots],
        "inputs": series,
        "outputs": outputs,
        "results": results,
        "options": options,
    }
