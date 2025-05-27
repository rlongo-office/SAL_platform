# Define start and end
$start = Get-Date '2024-07-01'
$end   = Get-Date '2024-09-29'

# Loop day-by-day
for ($d = $start; $d -le $end; $d = $d.AddDays(1)) {
    $day = $d.ToString('yyyy-MM-dd')
    Write-Host "Processing $day..."
    python .\combined.py $day
}
