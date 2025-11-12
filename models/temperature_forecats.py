#!/usr/bin/env python3

import requests
from datetime import datetime, timezone

def get_temperature_forecast(slots=None):
    """
    Získá teplotní předpověď a vrátí nejbližší teploty pro zadané 15minutové intervaly.
    
    Args:
        slots: Seznam časových razítek v 15minutových intervalech (ISO formát)
    
    Returns:
        Seznam tuple (čas, teplota) pro každý slot
    """
    lat, lon = 50.76415, 15.16004
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=temperature_2m"
    import json, urllib.request
    
    data = json.loads(urllib.request.urlopen(url).read())
    forecast_times = data["hourly"]["time"]
    forecast_temps = data["hourly"]["temperature_2m"]
    
    # Pokud nejsou zadané sloty, vrať celou předpověď
    if slots is None:
        return list(zip(forecast_times, forecast_temps))
    
    # Převod časových razítek na datetime objekty pro snadnější porovnání
    # Zajistit, aby všechny datetime objekty měly timezone
    forecast_datetimes = []
    for t in forecast_times:
        dt = datetime.fromisoformat(t.replace('T', ' '))
        # Pokud nemá timezone, přidat UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        forecast_datetimes.append(dt)
    
    result = []
    for slot in slots:
        # slot je už datetime objekt, ne string
        if isinstance(slot, str):
            slot_dt = datetime.fromisoformat(slot.replace('T', ' '))
        else:
            slot_dt = slot
            
        # Zajistit, aby slot_dt měl timezone
        if slot_dt.tzinfo is None:
            slot_dt = slot_dt.replace(tzinfo=timezone.utc)
        elif hasattr(slot_dt, 'astimezone'):
            # Převést na UTC pro konzistentní porovnání
            slot_dt = slot_dt.astimezone(timezone.utc)
        
        # Převést všechny forecast_datetimes na UTC
        forecast_datetimes_utc = []
        for fdt in forecast_datetimes:
            if hasattr(fdt, 'astimezone'):
                forecast_datetimes_utc.append(fdt.astimezone(timezone.utc))
            else:
                forecast_datetimes_utc.append(fdt)
        
        # Najdi nejbližší čas v předpovědi
        closest_idx = min(range(len(forecast_datetimes_utc)), 
                         key=lambda i: abs((forecast_datetimes_utc[i] - slot_dt).total_seconds()))
        
        result.append((slot, forecast_temps[closest_idx]))
    
    return result

if __name__ == "__main__":
    print(get_temperature_forecast())