#!/usr/bin/env python3
"""
Ukázka rozšíření MPC optimalizátoru o penalty funkce pro teploty

Toto je návrh, jak by se měly komfortní teploty řešit přímo v optimalizátoru
místo složité logiky v actions.py.
"""

# Přidání do run_mpc_optimizer funkce (kolem řádku 110):

def add_temperature_penalties_to_optimizer():
    """
    Návrh rozšíření optimalizátoru o penalty funkce pro teploty.
    
    Tato funkce ukazuje, jak by se měly přidat penalty za nedostatečné teploty
    přímo do účelové funkce optimalizátoru.
    """
    
    # === NOVÉ PARAMETRY (přidat kolem řádku 110 v powerplan_optimizer.py) ===
    
    # Penalty parametry pro teploty
    temp_comfort_penalty = get_option(options, "temp_comfort_penalty")
    temp_bath_penalty = get_option(options, "temp_bath_penalty") 
    temp_critical_penalty = get_option(options, "temp_critical_penalty")
    
    # Cílové teploty
    temp_comfort_target = get_option(options, "temp_comfort_target")
    temp_bath_target = get_option(options, "temp_bath_target")
    temp_bath_reduced = get_option(options, "temp_bath_reduced")
    temp_critical_min = get_option(options, "temp_critical_min")
    temp_lower_warm = get_option(options, "temp_lower_warm")
    
    # Časové okno pro koupání
    bath_time_start = get_option(options, "bath_time_start")
    bath_time_end = get_option(options, "bath_time_end")
    
    # === NOVÉ PROMĚNNÉ (přidat kolem řádku 140) ===
    
    # Penalty proměnné pro nedostatečné teploty
    temp_comfort_deficit = LpVariable.dicts("temp_comfort_deficit", indexes, 0)
    temp_bath_deficit = LpVariable.dicts("temp_bath_deficit", indexes, 0)
    temp_critical_deficit = LpVariable.dicts("temp_critical_deficit", indexes, 0)
    
    # === ROZŠÍŘENÍ ÚČELOVÉ FUNKCE (kolem řádku 180) ===
    
    # Přidat do účelové funkce (lpSum):
    """
    + temp_comfort_penalty * lpSum(temp_comfort_deficit[t] * dt[t] for t in indexes)
    + temp_bath_penalty * lpSum(temp_bath_deficit[t] * dt[t] for t in indexes)  
    + temp_critical_penalty * lpSum(temp_critical_deficit[t] * dt[t] for t in indexes)
    """
    
    # === NOVÁ OMEZENÍ (přidat do hlavní smyčky kolem řádku 200) ===
    
    # Pro každý časový slot t:
    for t in indexes:
        # Výpočet aktuální teploty z energie
        current_temp_upper = energy_to_temp(h_soc_upper[t], h_upper_vol, h_upper_min_t)
        current_temp_lower = energy_to_temp(h_soc_lower[t], h_lower_vol, h_lower_min_t)
        
        # Určení aktuální hodiny (z slots[t])
        current_hour = slots[t].hour
        is_bath_time = bath_time_start <= current_hour <= bath_time_end
        
        # Kritická teplota - vždy penalizovat
        prob += temp_critical_deficit[t] >= temp_critical_min - current_temp_upper
        prob += temp_critical_deficit[t] >= 0
        
        # Komfortní teplota - penalizovat celý den
        prob += temp_comfort_deficit[t] >= temp_comfort_target - current_temp_upper
        prob += temp_comfort_deficit[t] >= 0
        
        # Teplota pro koupání - penalizovat pouze v době 18-21h
        if is_bath_time:
            # Určit cílovou teplotu podle spodní zóny
            lower_is_warm = current_temp_lower > temp_lower_warm
            bath_target = temp_bath_reduced if lower_is_warm else temp_bath_target
            
            prob += temp_bath_deficit[t] >= bath_target - current_temp_upper
            prob += temp_bath_deficit[t] >= 0
        else:
            # Mimo dobu koupání žádná penalizace
            prob += temp_bath_deficit[t] == 0
    
    # === VÝSTUPY (přidat do outputs dictionary) ===
    
    # Přidat do outputs:
    """
    "temp_comfort_deficit": [temp_comfort_deficit[t].varValue for t in indexes],
    "temp_bath_deficit": [temp_bath_deficit[t].varValue for t in indexes],
    "temp_critical_deficit": [temp_critical_deficit[t].varValue for t in indexes],
    """
    
    # === VÝSLEDKY (přidat do results dictionary) ===
    
    # Přidat do results:
    """
    "total_temp_comfort_penalty": sum(temp_comfort_deficit[t].varValue * temp_comfort_penalty * dt[t] for t in indexes),
    "total_temp_bath_penalty": sum(temp_bath_deficit[t].varValue * temp_bath_penalty * dt[t] for t in indexes),
    "total_temp_critical_penalty": sum(temp_critical_deficit[t].varValue * temp_critical_penalty * dt[t] for t in indexes),
    """

# === VÝHODY TOHOTO PŘÍSTUPU ===

"""
1. JEDNODUCHÁ LOGIKA v actions.py:
   - upper_on/lower_on: pouze FVE akumulace
   - comfort_heating_grid: pouze kritické situace + MPC signál
   - max_heat_on: pouze velké přebytky
   - block_heating: pouze extrémní situace

2. INTELIGENTNÍ OPTIMALIZACE:
   - MPC automaticky plánuje ohřev podle budoucích podmínek
   - Penalty funkce zajistí komfortní teploty
   - Optimalizátor najde nejlevnější způsob dosažení cílů

3. LEPŠÍ PŘEDVÍDATELNOST:
   - Jasně definované cíle a penalty
   - Méně hardcoded pravidel
   - Snadnější ladění parametrů

4. FLEXIBILITA:
   - Různé penalty pro různé situace
   - Časově závislé požadavky
   - Snadné přidání nových pravidel
"""

if __name__ == "__main__":
    print("Toto je pouze ukázka návrhu rozšíření optimalizátoru.")
    print("Skutečná implementace by se měla přidat do powerplan_optimizer.py")
