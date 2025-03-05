import os
import csv
import json
from datetime import datetime

EXPORT_DIR = "exports"
os.makedirs(EXPORT_DIR, exist_ok=True)

def serialize_data(data):
    """
    Converts non-serializable objects (like datetime) into JSON-friendly formats.
    """
    if isinstance(data, datetime):
        return data.isoformat()  # Convert datetime to string
    if isinstance(data, dict):
        return {key: serialize_data(value) for key, value in data.items()}
    if isinstance(data, list):
        return [serialize_data(item) for item in data]
    return data

def export_data(data, filename="exported_data"):
    """
    Exports data to CSV and JSON files.
    
    :param data: List of dictionaries or tuples representing query results.
    :param filename: Base filename for exported files (without extension).
    """
    csv_file = os.path.join(EXPORT_DIR, f"{filename}.csv")
    json_file = os.path.join(EXPORT_DIR, f"{filename}.json")

    # Convert tuple results to list of dicts if needed
    if data and isinstance(data[0], tuple):
        keys = [f"column_{i}" for i in range(len(data[0]))]  # Generate generic column names
        data = [dict(zip(keys, row)) for row in data]

    # Convert non-serializable types
    data_serialized = serialize_data(data)

    # Export to CSV
    if data_serialized:
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=data_serialized[0].keys())
            writer.writeheader()
            writer.writerows(data_serialized)

    # Export to JSON
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(data_serialized, f, indent=4)

    print(f"✅ Data exported to {csv_file} and {json_file}")
