import argparse, json, csv, math, sys

from datetime import datetime
from pathlib import Path

# Read orders and turn each items cell into a list of cargo dictionaries.
def extract_orders(filename):
    orders = []

    with open(filename, newline="", encoding="utf-8-sig") as file:
        for row in csv.DictReader(file):
            cargo = []

            for item in row["items"].split():
                parts = item.split(":")
                if len(parts) != 3 or not parts[0]:
                    raise ValueError(f"Bad item '{item}': use name:kg:ddmmyyyy")

                name, weight, deadline = parts
                weight = float(weight)
                if not math.isfinite(weight) or weight <= 0:
                    raise ValueError(f"Invalid weight for item: {name}")

                datetime.strptime(deadline, "%d%m%Y")
                cargo.append({
                    "name": name,
                    "weight": weight,
                    "deadline": deadline
                })

            if not cargo:
                raise ValueError("An order must contain at least one cargo item")

            row["items"] = cargo
            orders.append(row)

    return orders

# Read the CSV and sort cargo inside each order by earliest deadline.
def sort_cargo(filename):
   
    orders = extract_orders(filename)
    for order in orders:
        order["items"].sort(
            key=lambda item: datetime.strptime(item["deadline"], "%d%m%Y")
        )
    return orders




# Calculate daily ice conditions from the beginning of the dataset.
# FDD >= 625 is equivalent to thickness >= 50 cm.
def calculate_ice_conditions(filename):
    
    ice_conditions = {}
    fdd = 0.0
    previous_date = None

    with open(filename, newline="", encoding="utf-8-sig") as file:
        temperatures = list(csv.DictReader(file))

    temperatures.sort(
        key=lambda row: datetime.strptime(row["date"], "%d%m%Y")
    )
    for row in temperatures:
        current_date = datetime.strptime(row["date"], "%d%m%Y")
        if previous_date is not None and (current_date - previous_date).days != 1:
            raise ValueError("Temperature dates must be unique and consecutive")
        previous_date = current_date

        temperature = float(row["mean_temp_c"])
        if not math.isfinite(temperature):
            raise ValueError(f"Invalid temperature on {row['date']}")

        fdd = max(0.0, fdd - temperature)
        thickness_cm = 2.0 * math.sqrt(fdd)
        max_load_kg = 20.0 * fdd

        ice_conditions[row["date"]] = {
            "temperature_c": temperature,
            "fdd": fdd,
            "thickness_cm": thickness_cm,
            "max_load_kg": max_load_kg,
            "is_open": temperature <= 0 and fdd >= 625
        }
    return ice_conditions


# Check one ice section using fuel remaining at that section's start.
def can_cross_ice(conditions, empty_weight_kg, cargo_weight_kg, fuel_remaining_l):
    if conditions is None:
        raise ValueError("No weather data for this crossing date")

    for value in (empty_weight_kg, cargo_weight_kg, fuel_remaining_l):
        if not math.isfinite(value) or value < 0:
            raise ValueError("Truck weight, cargo, and fuel must be nonnegative")

    truck_weight = empty_weight_kg + cargo_weight_kg + fuel_remaining_l * 0.85

    return conditions["is_open"] and truck_weight <= conditions["max_load_kg"]


# Calculate the total cargo weight 
def calculate_total_cargo_weight(order):
    total_weight = 0

    for item in order["items"]:
        total_weight += item["weight"]

    return total_weight


# Estimated cost according to trip wtihout splitting across multiple days.
def estimate_trip_cost(route, cost_per_km):
    total_distance = 0
    hours_today = 0
    driving_days = 1 if route else 0
    stops = []

    cost_per_km = float(cost_per_km)
    if cost_per_km < 0:
        raise ValueError("Cost per kilometre cannot be negative")

    for section in route:
        
        distance = float(section["distance_km"])
        speed = float(section["speed_kmh"])
        hours = calculate_Driving_Hours(distance, speed)

        if hours > 12:
            raise ValueError("A road section takes more than 12 hours")

        if hours_today + hours > 12:
            stops.append(section["from"])
            driving_days += 1
            hours_today = 0

        hours_today += hours
        total_distance += distance

    return {
        "estimated_cost": round(total_distance * cost_per_km, 2),
        "driving_days": driving_days,
        "overnight_stops": stops
    }

# Assing vehicles to trips acccording to the weight they can carry
def assign_vehicles(order, truck, planes):
    truck_kg = 0
    air_kg = 0
    capacity = float(truck["max_cargo_kg"])

    for item in order["items"]:
        if truck_kg + item["weight"] <= capacity:
            truck_kg += item["weight"]
        else:
            air_kg += item["weight"]

    planes = sorted(planes, key=lambda p: float(p["capacity_kg"]))
    flights = []

    if air_kg > 0 and not planes:
        raise ValueError("No planes available")

    while air_kg > 0:
        plane = next(
            (p for p in planes if float(p["capacity_kg"]) >= air_kg),
            planes[-1]
        )
        load = min(air_kg, float(plane["capacity_kg"]))

        flights.append({
            "Plane": plane["plane_id"],
            "Cargo (kg)": load
        })
        air_kg -= load

    return truck_kg, flights


# Calculate hours for one road section. Zero distance returns zero hours.
def calculate_Driving_Hours(distance_km, speed_kmh):
    if (not math.isfinite(distance_km) or not math.isfinite(speed_kmh)
            or distance_km < 0 or speed_kmh <= 0):
        raise ValueError("Invalid distance or speed value(s)")

    return distance_km / speed_kmh


# Return whether the whole next section fits in today's 12-hour limit.
def check_Daily_Driving_Limit(hours_driven_today, hours_traveled):
    if (not math.isfinite(hours_driven_today)
            or not math.isfinite(hours_traveled)
            or hours_driven_today < 0 or hours_traveled < 0):
        raise ValueError("Invalid hour value")

    return hours_driven_today + hours_traveled <= 12


# Main
def main(orders_filename, temperatures_filename):
    sorted_orders = sort_cargo(orders_filename)
    folder = Path(orders_filename).resolve().parent

    with open(folder / "map.csv", newline="", encoding="utf-8-sig") as file:
        roads = list(csv.DictReader(file))

    with open(folder / "trucks.csv", newline="", encoding="utf-8-sig") as file:
        trucks = {
            row["truck_id"]: row
            for row in csv.DictReader(file)
        }

    with open(folder / "planes.csv", newline="", encoding="utf-8-sig") as file:
        planes = list(csv.DictReader(file))

    output = []

    for order in sorted_orders:
        # Find the route in either road direction.
        queue = [(order["start"], [])]
        visited = set()
        route = None

        while queue:
            town, path = queue.pop(0)

            if town == order["destination"]:
                route = path
                break

            if town in visited:
                continue

            visited.add(town)

            for road in roads:
                if road["from"] == town:
                    queue.append((road["to"], path + [road]))

                elif road["to"] == town:
                    reversed_road = road.copy()
                    reversed_road["from"] = road["to"]
                    reversed_road["to"] = road["from"]

                    queue.append((
                        reversed_road["to"],
                        path + [reversed_road]
                    ))

        if route is None:
            raise ValueError("No route found")

        if order["truck_id"] not in trucks:
            raise ValueError(
                f"Unknown truck type: {order['truck_id']}"
            )

        truck = trucks[order["truck_id"]]

        # Assign cargo to the truck and planes.
        truck_kg, flights = assign_vehicles(
            order, truck, planes
        )

        # No driving cost if everything goes by plane.
        estimate = estimate_trip_cost(
            route if truck_kg > 0 else [],
            truck["cost_per_km"]
        )

        output.append({
            "Order": order["order_id"],
            "Departure date": order["earliest_start"],
            "Truck": order["truck_id"] if truck_kg > 0 else None,
            "Truck cargo (kg)": truck_kg,
            "Flown cargo (kg)": sum(
                flight["Cargo (kg)"] for flight in flights
            ),
            "Driving days": estimate["driving_days"],
            "Items": [
                {
                    "Item": item["name"],
                    "Weight (kg)": item["weight"],
                    "Deadline": item["deadline"]
                }
                for item in order["items"]
            ],
            "Stops": [
                {"Town": town}
                for town in estimate["overnight_stops"]
            ],
            "Flights": flights,
            "Estimated driving cost ($)": estimate["estimated_cost"]
        })

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    folder = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Read orders and calculate ice conditions")
    parser.add_argument("--orders", type=Path, default=folder / "orders.csv")
    parser.add_argument("--temperatures", type=Path, default=folder / "temperatures.csv")
    args = parser.parse_args()

    try:
        main(args.orders, args.temperatures)
    except (OSError, ValueError, KeyError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)

        """"
        conditions = ice_conditions.get(order["earliest_start"])
        if conditions is None:
            print("No weather data for the earliest-start date.")
        else:
            print("Ice on the earliest-start date (not a crossing approval):")
            print(f"  Temperature: {conditions['temperature_c']:g} C")
            print(f"  Thickness: {conditions['thickness_cm']:.1f} cm")
            print(f"  Maximum supported weight: {conditions['max_load_kg']:.0f} kg")
            print(f"  Open: {conditions['is_open']}\n")
"""