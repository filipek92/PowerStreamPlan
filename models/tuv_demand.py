def get_tuv_demand(time):
    """
    Vrací očekávanou spotřebu TUV (kWh) v daném 15 minutovém intervalu.
    Přiřazuje spotřebu podle scénáře (rozděleno na 15min úseky):
      - 6:00–6:59 sprcha (1,2 kWh) → 0,3 kWh na čtvrthodinu
      - 7:00–7:59 čištění zubů (0,05 kWh) → 0,0125 kWh na čtvrthodinu
      - 15:00–15:59 sprcha (1,0 kWh) → 0,25 kWh na čtvrthodinu
      - 18:00–18:59 dvě vany (5,0 kWh) → 1,25 kWh na čtvrthodinu
    Jinak 0.0
    """
    hour = time.hour

    if hour == 6:
        return 0.3   # sprcha (1,2 kWh / 4)
    elif hour == 7:
        return 0.0125  # čištění zubů (0,05 kWh / 4)
    elif hour == 15:
        return 0.25   # sprcha (1,0 kWh / 4)
    elif hour == 18:
        return 1.25   # dvě vany (5,0 kWh / 4)
    else:
        return 0.0