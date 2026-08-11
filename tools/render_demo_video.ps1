[CmdletBinding()]
param(
    [string]$Voice = "Microsoft David Desktop",
    [ValidateRange(-10, 10)]
    [int]$Rate = 0,
    [string]$OutputFile = "outputs/shiftzero-demo-4min.mp4"
)

$ErrorActionPreference = "Stop"
$workspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$timelinePath = Join-Path $workspace "docs/video-narration.json"
$timeline = Get-Content -LiteralPath $timelinePath -Raw | ConvertFrom-Json
$buildDir = Join-Path $workspace "tmp/video-build"
$outputPath = [System.IO.Path]::GetFullPath((Join-Path $workspace $OutputFile))
New-Item -ItemType Directory -Path $buildDir -Force | Out-Null
New-Item -ItemType Directory -Path ([System.IO.Path]::GetDirectoryName($outputPath)) -Force | Out-Null

$ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source
$ffprobe = (Get-Command ffprobe -ErrorAction Stop).Source
Add-Type -AssemblyName System.Speech
$synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
$availableVoices = @($synth.GetInstalledVoices() | Where-Object Enabled | ForEach-Object { $_.VoiceInfo.Name })
if ($Voice -notin $availableVoices) {
    throw "Voice '$Voice' is unavailable. Available voices: $($availableVoices -join ', ')"
}
$synth.SelectVoice($Voice)
$synth.Rate = $Rate
$synth.Volume = 100

$segments = [System.Collections.Generic.List[string]]::new()
try {
    for ($index = 0; $index -lt $timeline.Count; $index++) {
        $scene = $timeline[$index]
        $number = $index + 1
        $imagePath = [System.IO.Path]::GetFullPath((Join-Path $workspace $scene.image))
        if (-not (Test-Path -LiteralPath $imagePath)) {
            throw "Missing scene image: $imagePath"
        }
        $audioPath = Join-Path $buildDir ("{0:D2}-{1}.wav" -f $number, $scene.label)
        $segmentPath = Join-Path $buildDir ("{0:D2}-{1}.mp4" -f $number, $scene.label)
        $synth.SetOutputToWaveFile($audioPath)
        $synth.Speak([string]$scene.narration)
        $synth.SetOutputToNull()

        $filter = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x06110f,zoompan=z='min(zoom+0.00012,1.035)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30,format=yuv420p"
        & $ffmpeg -y -hide_banner -loglevel error -loop 1 -framerate 30 -i $imagePath -i $audioPath -vf $filter -c:v libx264 -preset veryfast -crf 21 -c:a aac -b:a 160k -shortest -movflags +faststart $segmentPath
        if ($LASTEXITCODE -ne 0) {
            throw "ffmpeg failed for scene $number"
        }
        $segments.Add($segmentPath)
    }
}
finally {
    $synth.Dispose()
}

$concatPath = Join-Path $buildDir "segments.txt"
$concatLines = $segments | ForEach-Object { "file '$($_.Replace('\', '/').Replace("'", "'\''"))'" }
[System.IO.File]::WriteAllLines($concatPath, $concatLines, [System.Text.UTF8Encoding]::new($false))
$rawPath = Join-Path $buildDir "concatenated-raw.mp4"
& $ffmpeg -y -hide_banner -loglevel error -f concat -safe 0 -i $concatPath -c copy -movflags +faststart $rawPath
if ($LASTEXITCODE -ne 0) {
    throw "ffmpeg failed while concatenating scenes"
}
& $ffmpeg -y -hide_banner -loglevel error -i $rawPath -c:v copy -af "loudnorm=I=-16:TP=-1.5:LRA=11" -c:a aac -b:a 160k -movflags +faststart $outputPath
if ($LASTEXITCODE -ne 0) {
    throw "ffmpeg failed while normalizing narration audio"
}

$probe = & $ffprobe -v error -show_entries format=duration,size -show_entries stream=index,codec_name,width,height,r_frame_rate -of json $outputPath | ConvertFrom-Json
[pscustomobject]@{
    Output = $outputPath
    DurationSeconds = [math]::Round([double]$probe.format.duration, 2)
    SizeBytes = [int64]$probe.format.size
    Streams = $probe.streams
}
