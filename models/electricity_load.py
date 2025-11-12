base = 0.5  # kW – nepřetržitá zátěž

def get_electricity_load(time):
    """
    Vrací očekávanou spotřebu elektřiny (kW) pro 15 minutové intervaly.
    Hodnoty jsou pro kontinuální výkon během 15 minut.
    """
    if time.hour < 6:
        extra = 0.0          # noc
    elif time.hour < 9:
        extra = 0.75         # snídaně / příprava do práce
    elif time.hour < 12:
        extra = 0.5          # dopolední provoz
    elif time.hour < 18:
        extra = 0.25         # oběd + běžný chod domácnosti
    elif time.hour < 21:
        extra = 0.25         # večerní vaření / pračka
    else:
        extra = 0.0          # pozdní večer

    return base + extra