### Segmented into 2 hours per clip
### Loops forever: if ffmpeg exits (stream disconnect), it restarts automatically.

$streamUrl = "http://orangepi.local:8080/8df78d67-af15-48d0-8deb-b26ef6555b4c.m3u8"
$retryDelaySeconds = 5

while ($true) {
    $startTime = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$startTime] Starting ffmpeg..."

    ffmpeg -hide_banner -live_start_index 0 -i $streamUrl `
        -c copy -bsf:a aac_adtstoasc `
        -f segment -segment_time 7200 -reset_timestamps 1 `
        -movflags frag_keyframe+empty_moov -strftime 1 `
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
