from datetime import datetime

def get_fve_forecast(state_list, entity_id):
    # Pro 15 minutové intervaly zaokrouhlíme na nejbližší 15 minut
    now = datetime.now().replace(second=0, microsecond=0).astimezone()
    quarter_minute = (now.minute // 15) * 15
    now = now.replace(minute=quarter_minute)
    
    values = []
    for e in state_list:
        if e["entity_id"] == entity_id:
            attributes = e.get("attributes", {})
            detailed = attributes.get("detailedForecast", [])
            # Filtrovat jen aktuální a budoucí intervaly (15 min)
            filtered = [
                (datetime.fromisoformat(x["period_start"]), x["pv_estimate"])
                for x in detailed
                if datetime.fromisoformat(x["period_start"]).astimezone(now.tzinfo) >= now
            ]
            # Setřídit pro jistotu (mělo by být, ale ...)
            sorted_series = sorted(filtered, key=lambda x: x[0])
            # Vrátit pole dvojic (čas, odhad výroby)
            return [
                (dt, float(val))
                for dt, val in sorted_series
            ]
    return values

def fve_forecast_for_slots(fve_raw, slots):
    """
    Připraví předpověď FVE pro dané časové sloty (interpolace nebo opakování).
    """
    fve_dict = dict(fve_raw)
    fve_pred = []
    for slot in slots:
        if slot in fve_dict:
            fve_pred.append(fve_dict[slot])
        else:
            # Pokud není přesný čas, najdeme nejbližší předchozí hodnotu
            previous_times = [t for t in fve_dict.keys() if t <= slot]
            if previous_times:
                nearest_time = max(previous_times)
                fve_pred.append(fve_dict[nearest_time])
            else:
                fve_pred.append(0.0)  # Nebo jiná výchozí hodnota
    return fve_pred