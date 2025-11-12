from datetime import datetime

def get_electricity_price(state_list: list, entity_id: str):
    now = datetime.now().astimezone()
    # Pro 15 minutové intervaly zaokrouhlíme na nejbližší 15 minut
    quarter_minute = (now.minute // 15) * 15
    start_of_quarter = now.replace(minute=quarter_minute, second=0, microsecond=0)
    
    for e in state_list:
        if e["entity_id"] == entity_id:
            attributes = e.get("attributes", {})
            filtered = [
                (k, v) for k, v in attributes.items()
                if k.startswith("202")
            ]
            filtered = [
                (k, v) for k, v in filtered
                if datetime.fromisoformat(k).astimezone(start_of_quarter.tzinfo) >= start_of_quarter
            ]
            sorted_series = sorted(filtered, key=lambda x: x[0])
            return [
                (datetime.fromisoformat(k), float(v))
                for k, v in sorted_series
            ]
    return []