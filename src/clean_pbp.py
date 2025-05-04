import os
import re

# Define file paths
input_file = "data/pbp_fields"
output_file = "data/pbp_fields_cleaned"

# Ensure the data directory exists
os.makedirs("data", exist_ok=True)

# Define regex pattern to clean lines
pattern = re.compile(r"^.*?response(.*?):.*$")

# Process the file
with open(input_file, "r", encoding="utf-8") as infile, open(output_file, "w", encoding="utf-8") as outfile:
    for line in infile:
        match = pattern.match(line)
        if match:
            cleaned_line = "response" + match.group(1).strip()
            outfile.write(cleaned_line + "\n")

print(f"✅ Process completed. Cleaned data saved to: {output_file}")
