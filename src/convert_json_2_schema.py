import json
import os
import logging
import subprocess

# Setup logging
logging.basicConfig(level=logging.INFO)


def flatten_json(nested_json):
    """
    Recursively flattens a JSON object, converting all nested dicts/lists to JSON strings.
    """
    if isinstance(nested_json, dict):
        return {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in nested_json.items()}
    elif isinstance(nested_json, list):
        return [flatten_json(item) for item in nested_json]
    return nested_json  # Return as-is if not dict or list


def preprocess_json(input_file, output_file):
    """ Read JSON, flatten nested fields, and write to a new file. """
    with open(input_file, "r", encoding="utf-8") as infile:
        data = json.load(infile)

    # If the JSON root is an array, flatten each item in the array
    if isinstance(data, list):
        flattened_data = [flatten_json(item) for item in data]
    else:
        flattened_data = flatten_json(data)

    with open(output_file, "w", encoding="utf-8") as outfile:
        json.dump(flattened_data, outfile, indent=4)

    logging.info(f"✅ Preprocessed JSON saved to: {output_file}")


def convert_json_to_sql(json_file):
    """ Convert JSON to SQLite using sqlitebiter. """
    preprocessed_file = json_file.replace(".json", "_flat.json")
    preprocess_json(json_file, preprocessed_file)

    try:
        logging.info(f"Converting {preprocessed_file} to SQLite database.")
        subprocess.run(["sqlitebiter", "file", preprocessed_file], check=True, text=True)
        logging.info("✅ JSON successfully converted to SQLite.")
    except subprocess.CalledProcessError as e:
        logging.error(f"Error during conversion: {e}")
        return


# Set your JSON file path
json_file_path = "data/pbp_sample.json"
convert_json_to_sql(json_file_path)
