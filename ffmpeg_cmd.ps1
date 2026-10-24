### Segmented into 2 hours per clip
### Loops forever: if ffmpeg exits (stream disconnect), it restarts automatically.

$streamUrl = "http://orangepi.local:8080/8df78d67-af15-48d0-8deb-b26ef6555b4c.m3u8"
$retryDelaySeconds = 5

### Start three entries back from the newest one. The stream's playlist still
### lists about 8 hours of history, but the server has already deleted those old
### segments, so asking for the oldest one (0) returns "404 Not Found" forever
### and no clip file is ever written. Starting near the newest goes live at once.
$liveStartIndex = -3

while ($true) {
    $startTime = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$startTime] Starting ffmpeg..."

    ffmpeg -hide_banner -live_start_index $liveStartIndex -i $streamUrl `
        -c copy -bsf:a aac_adtstoasc `
        -f segment -segment_time 7200 -reset_timestamps 1 `
        -segment_format mp4 -segment_format_options movflags=frag_keyframe+empty_moov `
        -strftime 1 `
        "Recordings/%Y%m%d_%H%M%S.mp4"

    $exitCode = $LASTEXITCODE
    $endTime = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

    if ($exitCode -eq 0) {
        Write-Host "[$endTime] ffmpeg exited normally (you stopped it or the stream ended cleanly). Restarting anyway..."
    } else {
        Write-Host "[$endTime] ffmpeg disconnected (exit code $exitCode). Reconnecting in $retryDelaySeconds seconds..."
    }

    Start-Sleep -Seconds $retryDelaySeconds
}
