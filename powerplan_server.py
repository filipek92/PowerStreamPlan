#!/usr/bin/env python3

import os
import json
import csv
from datetime import datetime, timedelta

from flask import Flask, render_template, redirect, url_for, request, send_from_directory
from flask_apscheduler import APScheduler

from powerplan_environment import PORT, HA_ADDON, RESULTS_DIR, LATEST_LINK, LATEST_CSV
from powerplan_optimizer import run_mpc_optimizer
from data_connector import prepare_data, publish_to_ha
from presentation import presentation
from actions import powerplan_to_actions, powerplan_to_actions_timeline, ACTION_ATTRIBUTES
from powerplan_settings import settings_bp, load_settings
from publish_version import get_current_version

ENABLE_PUBLISH = bool(HA_ADDON)

if ENABLE_PUBLISH:
    print("Publishing to Home Assistant is enabled.")
else:
    print("Publishing to Home Assistant is disabled. Set HA_ADDON environment variable to enable it.")


app = Flask(__name__)

class Config:                                    # <-- nový blok
    # spustí miniaturní REST rozhraní na /scheduler (můžeš vypnout)
    SCHEDULER_API_ENABLED = False

app.config.from_object(Config())

app.register_blueprint(settings_bp)

# --- Výpočet a cache ------------------------------------------------------

def compute_and_cache():
    data = prepare_data()
    settings = load_settings()

    series_keys = [
        "tuv_demand",
        "heating_demand",
        "fve_pred",
        "buy_price",
        "sell_price",
        "load_pred",
        "outdoor_temps",
    ]

    initials_keys = ["bat_soc", "temp_upper", "temp_lower"]

    dt = [0.25] * len(data["slots"])  # předpokládáme 15 minutový krok
    remain_slot_part = data["slots"][1].astimezone(None) - datetime.now().astimezone(None)
    dt[0] = remain_slot_part.total_seconds() / 3600.0  # zbytek aktuálního slotu v hodinách

    solution = run_mpc_optimizer(
        {k: data[k] for k in series_keys},
        {k: data[k] for k in initials_keys},
        data["slots"],
        settings,
        dt
    )
    # Tag solution with current app version
    solution["version"] = get_current_version()

    actions = powerplan_to_actions(solution)
    actions_timeline = powerplan_to_actions_timeline(solution)

    solution["actions"] = actions
    solution["actions_timeline"] = actions_timeline

    extra = {
        "generated_at": solution["generated_at"],
        # parse the first timestamp string back to datetime for further use
        "current_slot": solution["times"][0],
    }

    print("Solution results", json.dumps(solution["results"], indent=2))

    if ENABLE_PUBLISH:
        publish_to_ha(actions, "powerplan_", ACTION_ATTRIBUTES, extra)

        publish_to_ha({
            "debug": extra["current_slot"]
        }, "powerplan_", {
            "debug": solution["results"]
        })
    
    # Ensure results directory exists
    os.makedirs(RESULTS_DIR, exist_ok=True)

    # Save the solution to a timestamped file using current time
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_file = os.path.join(RESULTS_DIR, f"result_{timestamp}.json")
    # Use absolute path for symlink target to avoid relative resolution issues
    abs_result_file = os.path.abspath(result_file)
    with open(result_file, "w") as f:
        json.dump(solution, f, indent=4)

    # Create CSV export
    csv_file = os.path.join(RESULTS_DIR, f"result_{timestamp}.csv")
    abs_csv_file = os.path.abspath(csv_file)
    create_csv_export(solution, csv_file)

    print(f"Solution saved to {result_file} and {csv_file}")

    # Update the latest symlinks
    latest_json_link = LATEST_LINK
    latest_csv_link = LATEST_CSV
    
    # JSON symlink
    if os.path.islink(latest_json_link) or os.path.exists(latest_json_link):
        os.remove(latest_json_link)
    os.symlink(abs_result_file, latest_json_link)
    
    # CSV symlink
    if os.path.islink(latest_csv_link) or os.path.exists(latest_csv_link):
        os.remove(latest_csv_link)
    os.symlink(abs_csv_file, latest_csv_link)

    return solution

def load_cache(filename=None):
    try:
        if filename is None:
            filename = LATEST_LINK
        else:
            filename = os.path.join(RESULTS_DIR, filename)
        with open(filename, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        # If the file does not exist or is corrupted, return None
        print("Cache file not found or corrupted, recomputing...")
        return None

def create_csv_export(solution, filename):
    """
    Vytvoří CSV soubor s přehlednými daty z optimalizace.
    Zahrnuje vstupy, výstupy optimalizace, akce a klíčové metriky.
    """
    times = solution["times"]
    inputs = solution["inputs"]
    outputs = solution["outputs"]
    actions_timeline = solution["actions_timeline"]
    results = solution.get("results", {})
    
    # Příprava CSV dat
    csv_data = []
    for i, time_str in enumerate(times):
        row = {
            # Čas
            "Cas": time_str,
            
            # ==== VSTUPY OPTIMALIZACE ====
            "FVE_vyroba_kW": inputs["fve_pred"][i],
            "Spotreba_kW": inputs["load_pred"][i],
            "Cena_nakup_Kc_kWh": inputs["buy_price"][i],
            "Cena_prodej_Kc_kWh": inputs["sell_price"][i],
            "Pozadavek_TUV_kW": inputs["tuv_demand"][i],
            "Pozadavek_topeni_kW": inputs["heating_demand"][i],
            "Venkovni_teplota_C": inputs["outdoor_temps"][i],
            
            # ==== VÝSTUPY OPTIMALIZACE - BATERIE ====
            "Baterie_vykon_kW": outputs["b_power"][i],
            "Baterie_nabijeni_kW": outputs["b_charge"][i],
            "Baterie_vybijeni_kW": outputs["b_discharge"][i],
            "Baterie_SOC_kWh": outputs["b_soc"][i],
            "Baterie_SOC_procenta": outputs["b_soc_percent"][i],
            
            # ==== VÝSTUPY OPTIMALIZACE - SÍŤ ====
            "Sit_nakup_kW": outputs["g_buy"][i],
            "Sit_prodej_kW": outputs["g_sell"][i],
            "Naklady_nakup_Kc": outputs["buy_cost"][i],
            "Prijmy_prodej_Kc": outputs["sell_income"][i],
            "Celkove_naklady_Kc": outputs["net_step_cost"][i],
            
            # ==== VÝSTUPY OPTIMALIZACE - OHŘEV ====
            "Ohrev_dolni_kW": outputs["h_in_lower"][i],
            "Ohrev_horni_kW": outputs["h_in_upper"][i],
            "Ohrev_celkem_kW": outputs["h_in_lower"][i] + outputs["h_in_upper"][i],
            "Odber_dolni_kW": outputs["h_out_lower"][i],
            "Odber_horni_kW": outputs["h_out_upper"][i],
            "Prenos_tepla_kW": outputs.get("h_to_upper", [0] * len(times))[i],
            
            # ==== VÝSTUPY OPTIMALIZACE - AKUMULACE ====
            "Akumulace_dolni_kWh": outputs["h_soc_lower"][i],
            "Akumulace_horni_kWh": outputs["h_soc_upper"][i],
            "Akumulace_dolni_procenta": outputs["h_soc_lower_percent"][i],
            "Akumulace_horni_procenta": outputs["h_soc_upper_percent"][i],
            
            # ==== TEPLOTY ====
            "Teplota_dolni_C": outputs["temp_lower"][i],
            "Teplota_horni_C": outputs["temp_upper"][i],
            
            # ==== AKCE PRO HOME ASSISTANT ====
            "Rezim_menic": actions_timeline["charger_mode"][i],
            "Horni_akumulace": actions_timeline["upper_accumulation"][i],
            "Dolni_akumulace": actions_timeline["lower_accumulation"][i],
            "Maximalni_ohrev": actions_timeline["max_heat"][i],
            "Blokovani_ohrevu": actions_timeline["heating_blocked"][i],
            "Komfortni_ohrev": actions_timeline.get("comfort_heating_grid", [False] * len(times))[i],
            "Cilovy_SOC_procenta": actions_timeline["battery_target_soc"][i],
            "Rezervovany_vykon_W": actions_timeline["reserve_power"][i],
            "Minimalni_SOC_procenta": actions_timeline["minimum_soc"][i],
            
            # ==== VYPOČÍTANÉ HODNOTY ====
            "FVE_prebytek_kW": max(0, inputs["fve_pred"][i] - inputs["load_pred"][i]),
            "Nevyuzita_FVE_kW": outputs.get("fve_unused", [0] * len(times))[i],
            "Energeticka_bilance_kW": (
                inputs["fve_pred"][i] + outputs["b_discharge"][i] + outputs["g_buy"][i] -
                inputs["load_pred"][i] - outputs["b_charge"][i] - outputs["g_sell"][i] -
                outputs["h_in_lower"][i] - outputs["h_in_upper"][i]
            ),
            
            # ==== METRIKY OPTIMALIZACE (časově závislé) ====
            "Penalty_baterie_Kc": outputs["b_discharge"][i] * solution.get("options", {}).get("battery_penalty", 0),
            "Bonus_ohrev_vody_Kc": -(outputs["h_in_lower"][i] + outputs["h_in_upper"][i]) * solution.get("options", {}).get("water_priority_bonus", 0),
            "Bonus_horni_zona_Kc": -outputs["h_in_upper"][i] * solution.get("options", {}).get("upper_zone_priority", 0),
            "Penalty_nevyuzita_FVE_Kc": outputs.get("fve_unused", [0] * len(times))[i] * solution.get("options", {}).get("fve_unused_penalty", 0),
        }
        csv_data.append(row)
    
    # Zápis do CSV
    if csv_data:
        with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
            fieldnames = csv_data[0].keys()
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(csv_data)
        
        # Přidání souhrnu optimalizace jako komentář na konec souboru
        with open(filename, 'a', encoding='utf-8') as f:
            f.write("\n# ==== SOUHRN OPTIMALIZACE ====\n")
            f.write(f"# Vygenerováno: {solution.get('generated_at', 'neznámé')}\n")
            f.write(f"# Status řešení: {solution.get('status', 'neznámý')}\n")
            f.write(f"# Doba výpočtu: {solution.get('solve_time', 0):.2f}s\n")
            f.write(f"# Hodnota účelové funkce: {results.get('objective_value', 'neznámá')}\n")
            f.write("# \n")
            f.write("# CELKOVÉ METRIKY:\n")
            f.write(f"# Celkové náklady: {results.get('net_bilance', 0):.2f} Kč\n")
            f.write(f"# Odběr ze sítě: {results.get('grid_consumption', 0):.2f} kWh\n")
            f.write(f"# Dodávka do sítě: {results.get('grid_injection', 0):.2f} kWh\n")
            f.write(f"# Nabíjení baterie: {results.get('total_charged', 0):.2f} kWh\n")
            f.write(f"# Vybíjení baterie: {results.get('total_discharged', 0):.2f} kWh\n")
            f.write(f"# Nevyužitá FVE: {results.get('total_fve_unused', 0):.2f} kWh\n")
            f.write("# \n")
            f.write("# OPTIMALIZAČNÍ SLOŽKY:\n")
            f.write(f"# Penalty baterie: {results.get('total_battery_penalty', 0):.2f} Kč\n")
            f.write(f"# Bonus ohřev vody: {results.get('total_water_priority_bonus', 0):.2f} Kč\n")
            f.write(f"# Bonus horní zóna: {results.get('total_upper_zone_priority', 0):.2f} Kč\n")
            f.write(f"# Penalty nízký SOC: {results.get('total_battery_under_penalty', 0):.2f} Kč\n")
            f.write(f"# Penalty nevyužitá FVE: {results.get('total_fve_unused_penalty', 0):.2f} Kč\n")
            f.write(f"# Hodnota energie v nádrži: {results.get('total_final_boiler_value', 0):.2f} Kč\n")
            f.write(f"# Bonus konečné horní zóny: {results.get('final_upper_zone_bonus', 0):.2f} Kč\n")
            f.write(f"# Bonus hodnoty tepla: {results.get('tank_value_bonus', 0):.2f} Kč\n")
            f.write("# \n")
            f.write("# PARAZITNÍ ENERGIE:\n")
            f.write(f"# Celková parazitní energie: {results.get('total_parasitic_energy', 0):.2f} kWh\n")
            f.write(f"# Parazitní energie do baterie: {results.get('total_parasitic_to_battery', 0):.2f} kWh\n")
            f.write(f"# Parazitní energie ze sítě: {results.get('total_parasitic_to_grid', 0):.2f} kWh\n")

# --- Web routes -----------------------------------------------------------

@app.route("/regenerate", methods=["POST"])
def regenerate():
    compute_and_cache()
    return redirect('./')

@app.route("/")
def index():
    # List result files in descending order (newest first)
    all_files = sorted(
        (f for f in os.listdir(RESULTS_DIR)
         if f.startswith('result_') and f.endswith('.json')),
        reverse=True
    )
    # Seskupení podle data (YYYYMMDD)
    from collections import defaultdict
    grouped_files = defaultdict(list)
    for fname in all_files:
        date_part = fname.split('_')[1]  # např. '20250707'
        time_part = fname.split('_')[2].split('.')[0]  # např. '140000'
        # Převod času na čitelnější formát HH:MM:SS
        formatted_time = f"{time_part[:2]}:{time_part[2:4]}:{time_part[4:]}"
        grouped_files[date_part].append((time_part, formatted_time, fname))
    grouped_files = dict(sorted(grouped_files.items(), reverse=True))

    # Připravit seznam dnů a časů
    available_days = list(grouped_files.keys())
    compare_day = request.args.get('day')
    compare_time = request.args.get('time')
    # Pokud ještě nebyl vybrán žádný den, automaticky použij nejnovější dostupný den
    if not compare_day and available_days:
        compare_day = available_days[0]
    selected_file = None
    available_times = []
    available_times_display = []
    if compare_day and compare_day in grouped_files:
        # Prepare list of times and their display labels for the selected day
        available_times = [t for t, _, _ in grouped_files[compare_day]]
        available_times_display = [display_t for _, display_t, _ in grouped_files[compare_day]]
        # If no specific time requested, default to the first (latest) entry
        if not compare_time and available_times:
            compare_time = available_times[0]
            # Select the corresponding file for the default time
            selected_file = grouped_files[compare_day][0][2]
        # If a valid time is provided, find its file
        elif compare_time and compare_time in available_times:
            for t, _, f in grouped_files[compare_day]:
                if t == compare_time:
                    selected_file = f
                    break
    # Pokud není vybrán konkrétní soubor, použij latest
    if not selected_file:
        solution = load_cache()
        if solution is None:
            solution = compute_and_cache()
    else:
        solution = load_cache(selected_file)
        print(f"Loaded solution from {selected_file} {solution['version']}")

    generated_at = datetime.fromisoformat(solution.get("generated_at"))
    graphs = presentation(solution)
    
    # Připravit dodatečná data pro template
    slots = solution.get("slots", [])
    first_slot_data = slots[0] if slots else {}
    current_results = solution.get("results", {})
    timeline = solution.get("actions_timeline", {})
    
    # Přidat časy do timeline
    if timeline and "times" not in timeline:
        solution_times = solution.get("times", [])
        formatted_times = []
        for t in solution_times:
            # Pokud je časový údaj jako řetězec v ISO formátu, parse a formátuj
            if isinstance(t, str):
                try:
                    dt = datetime.fromisoformat(t)
                    formatted_times.append(dt.strftime("%H:%M"))
                except Exception:
                    formatted_times.append(t)
            # Pokud je datetime objekt
            elif hasattr(t, 'strftime'):
                formatted_times.append(t.strftime("%H:%M"))
            else:
                formatted_times.append(str(t))
        timeline["times"] = formatted_times

    # Determine whether to expand filter controls only when user provided filter parameters
    expand_filters = bool(request.args.get('day') or request.args.get('time'))
    return render_template(
        'index.html',
        graphs=graphs,
        generated_at=generated_at.strftime("%Y-%m-%d %H:%M:%S"),
        solution=solution,
        available_days=available_days,
        available_times=available_times,
        available_times_display=available_times_display,
        compare_day=compare_day,
        compare_time=compare_time,
        day=compare_day,  # Pro zpětnou kompatibilitu
        first_slot_data=first_slot_data,
        current_results=current_results,
        timeline=timeline,
        version=get_current_version(),
        selected_file=selected_file,  # Pro možnost stažení specifického CSV
    )

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(app.root_path, 'icon.png', mimetype='image/png')

@app.route('/download_csv')
def download_csv():
    """Stáhne CSV soubor s aktuálními optimalizačními daty"""
    try:
        # Zkontroluj, zda je specifikován konkrétní den/čas
        day = request.args.get('day')
        time = request.args.get('time')
        
        csv_filename = None
        solution = None
        
        if day and time:
            # Najdi konkrétní soubor
            all_files = sorted(
                (f for f in os.listdir(RESULTS_DIR)
                 if f.startswith('result_') and f.endswith('.json')),
                reverse=True
            )
            # Seskup podle data
            from collections import defaultdict
            grouped_files = defaultdict(list)
            for fname in all_files:
                date_part = fname.split('_')[1]
                time_part = fname.split('_')[2].split('.')[0]
                grouped_files[date_part].append((time_part, fname))
            
            if day in grouped_files:
                for t, json_file in grouped_files[day]:
                    if t == time:
                        # Název odpovídajícího CSV souboru
                        csv_filename = json_file.replace('.json', '.csv')
                        break
        
        # Pokud máme specifický soubor, zkus ho použít
        if csv_filename and os.path.exists(os.path.join(RESULTS_DIR, csv_filename)):
            download_name = f"powerplan_export_{day}_{time}.csv"
            return send_from_directory(RESULTS_DIR, csv_filename, as_attachment=True, 
                                     download_name=download_name)
        
        # Jinak použij latest.csv
        latest_csv = os.path.join(RESULTS_DIR, "latest.csv")
        if os.path.exists(latest_csv):
            download_name = f"powerplan_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            return send_from_directory(RESULTS_DIR, "latest.csv", as_attachment=True, 
                                     download_name=download_name)
        else:
            # Pokud latest.csv neexistuje, vygeneruj nový
            solution = load_cache()
            if solution:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                csv_filename = f"export_{timestamp}.csv"
                csv_path = os.path.join(RESULTS_DIR, csv_filename)
                create_csv_export(solution, csv_path)
                return send_from_directory(RESULTS_DIR, csv_filename, as_attachment=True,
                                         download_name=f"powerplan_export_{timestamp}.csv")
            else:
                return "CSV data nejsou dostupná", 404
    except Exception as e:
        return f"Chyba při generování CSV: {str(e)}", 500

@app.route('/download_csv/<filename>')
def download_csv_specific(filename):
    """Stáhne specifický CSV soubor podle názvu"""
    try:
        # Ověř, že soubor existuje a je to CSV
        if not filename.endswith('.csv') or not filename.startswith('result_'):
            return "Neplatný soubor", 400
        
        csv_path = os.path.join(RESULTS_DIR, filename)
        if not os.path.exists(csv_path):
            return "Soubor nenalezen", 404
            
        return send_from_directory(RESULTS_DIR, filename, as_attachment=True,
                                 download_name=f"powerplan_{filename}")
    except Exception as e:
        return f"Chyba při stahování souboru: {str(e)}", 500

# Inicializace scheduleru pokud běží v produkci
if HA_ADDON:
    # pokud běží v Dockeru, použij přepočítávej pravielně model
    scheduler = APScheduler()                        # <-- nový objekt
    scheduler.init_app(app)

    # registrace úlohy – každé 3 minuty pro 15 minutové intervaly
    scheduler.add_job(
        id="mpc_refresh",
        func=compute_and_cache,
        trigger="cron",
        minute="*/3",
    )

    scheduler.start()

def create_app():
    """Factory function pro vytvoření Flask aplikace."""
    return app

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True)

