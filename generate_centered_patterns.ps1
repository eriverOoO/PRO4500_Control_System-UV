param(
    [string]$SourceDirectory = (Join-Path $PSScriptRoot "generated_patterns1"),
    [string]$OutputDirectory = (Join-Path $PSScriptRoot "generated_patterns_centered"),
    [ValidateRange(0.000001, 1.0)]
    [double]$Scale = 0.1,
    [ValidateRange(12, 256)]
    [int]$StripePeriodPixels = 12
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Drawing
Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

public static class CenteredPatternWriter
{
    private static byte StructuredLightValue(int patternId, int localX, int periodPixels)
    {
        if (patternId == 0) return 255;
        if (patternId == 1) return 0;

        int stripeIndex = localX / periodPixels;
        if (patternId >= 2 && patternId <= 9)
        {
            int grayValue = stripeIndex ^ (stripeIndex >> 1);
            int bitShift = 7 - (patternId - 2);
            return ((grayValue >> bitShift) & 1) != 0 ? (byte)255 : (byte)0;
        }

        if (patternId >= 10 && patternId <= 13)
        {
            double phaseShift = (patternId - 10) * Math.PI / 2.0;
            double phase = 2.0 * Math.PI * localX / periodPixels + phaseShift;
            return (byte)Math.Round(
                127.5 * (1.0 + Math.Cos(phase)),
                MidpointRounding.AwayFromZero);
        }

        throw new ArgumentOutOfRangeException("patternId");
    }

    public static void Save(
        string sourcePath,
        string outputPath,
        double scale,
        int stripePeriodPixels,
        int patternId,
        bool invert)
    {
        using (var source = new Bitmap(sourcePath))
        using (var output = new Bitmap(source.Width, source.Height, PixelFormat.Format8bppIndexed))
        {
            ColorPalette palette = output.Palette;
            for (int value = 0; value < palette.Entries.Length; value++)
            {
                palette.Entries[value] = Color.FromArgb(value, value, value);
            }
            output.Palette = palette;

            int activeWidth = Math.Max(
                1,
                (int)Math.Round(source.Width * scale, MidpointRounding.AwayFromZero));
            int cycleCount = (activeWidth + stripePeriodPixels - 1) / stripePeriodPixels;
            if (cycleCount > 256)
            {
                throw new ArgumentOutOfRangeException(
                    "stripePeriodPixels",
                    "The active width may contain at most 256 Gray-code cycles.");
            }
            // The UI scale controls projected width only.  Preserve the full
            // source height so a 70% setting produces a 70% x 100% pattern.
            int activeHeight = source.Height;
            int offsetX = (source.Width - activeWidth) / 2;
            int offsetY = (source.Height - activeHeight) / 2;

            var bounds = new Rectangle(0, 0, output.Width, output.Height);
            BitmapData data = output.LockBits(
                bounds,
                ImageLockMode.WriteOnly,
                PixelFormat.Format8bppIndexed);

            try
            {
                int rowLength = Math.Abs(data.Stride);
                for (int y = 0; y < output.Height; y++)
                {
                    var row = new byte[rowLength];
                    if (y >= offsetY && y < offsetY + activeHeight)
                    {
                        int sourceY = Math.Min(
                            source.Height - 1,
                            (y - offsetY) * source.Height / activeHeight);

                        for (int x = offsetX; x < offsetX + activeWidth; x++)
                        {
                            int localX = x - offsetX;
                            int gray;
                            if (patternId >= 0 && patternId <= 13)
                            {
                                gray = StructuredLightValue(
                                    patternId,
                                    localX,
                                    stripePeriodPixels);
                            }
                            else
                            {
                                int sourceX = Math.Min(
                                    source.Width - 1,
                                    localX * source.Width / activeWidth);
                                Color color = source.GetPixel(sourceX, sourceY);
                                gray = (
                                    299 * color.R +
                                    587 * color.G +
                                    114 * color.B +
                                    500) / 1000;
                            }
                            row[x] = (byte)(invert ? 255 - gray : gray);
                        }
                    }

                    Marshal.Copy(
                        row,
                        0,
                        IntPtr.Add(data.Scan0, y * data.Stride),
                        row.Length);
                }
            }
            finally
            {
                output.UnlockBits(data);
            }

            output.Save(outputPath, ImageFormat.Bmp);
        }
    }
}
'@

$sourcePath = [System.IO.Path]::GetFullPath($SourceDirectory)
$outputPath = [System.IO.Path]::GetFullPath($OutputDirectory)

if (-not (Test-Path -LiteralPath $sourcePath -PathType Container)) {
    throw "Source pattern directory does not exist: $sourcePath"
}
if ($sourcePath -eq $outputPath) {
    throw "Source and output directories must be different."
}

$supportedExtensions = @(".bmp", ".png", ".jpg", ".jpeg", ".tif", ".tiff")
$sourceFiles = @(
    Get-ChildItem -LiteralPath $sourcePath -File |
        Where-Object { $supportedExtensions -contains $_.Extension.ToLowerInvariant() } |
        Sort-Object Name
)
if ($sourceFiles.Count -eq 0) {
    throw "No supported pattern images were found in: $sourcePath"
}

function Get-PatternId([string]$FileName) {
    $stem = [System.IO.Path]::GetFileNameWithoutExtension($FileName)
    if ($stem -match "^(?:pattern[_-])?(\d{1,3})(?:\D|$)") {
        return [int]$Matches[1]
    }
    return $null
}

New-Item -ItemType Directory -Force -Path $outputPath | Out-Null
$filesById = @{}

foreach ($sourceFile in $sourceFiles) {
    $patternId = Get-PatternId $sourceFile.Name
    if ($null -ne $patternId -and -not $filesById.ContainsKey($patternId)) {
        $filesById[$patternId] = $sourceFile
    }

    $destination = Join-Path $outputPath (
        [System.IO.Path]::GetFileNameWithoutExtension($sourceFile.Name) + ".bmp")
    [CenteredPatternWriter]::Save(
        $sourceFile.FullName,
        $destination,
        $Scale,
        $StripePeriodPixels,
        $(if ($null -eq $patternId) { -1 } else { $patternId }),
        $false)
    Write-Host "[generated] $destination"
}

$inverseLabels = @(
    "Gray0_inv",
    "Gray1_inv",
    "Gray2_inv",
    "Gray3_inv",
    "Gray4_inv",
    "Gray5_inv",
    "Gray6_inv",
    "Gray7_inv"
)

for ($grayIndex = 0; $grayIndex -lt $inverseLabels.Count; $grayIndex++) {
    $sourceId = 2 + $grayIndex
    $inverseId = 14 + $grayIndex
    if ($filesById.ContainsKey($inverseId)) {
        continue
    }
    if (-not $filesById.ContainsKey($sourceId)) {
        throw "Cannot generate inverse pattern $inverseId because source pattern $sourceId is missing."
    }

    $destination = Join-Path $outputPath (
        "{0:D2}_{1}.bmp" -f $inverseId, $inverseLabels[$grayIndex])
    [CenteredPatternWriter]::Save(
        $filesById[$sourceId].FullName,
        $destination,
        $Scale,
        $StripePeriodPixels,
        $sourceId,
        $true)
    Write-Host "[generated] $destination"
}

$profileImage = [System.Drawing.Image]::FromFile($sourceFiles[0].FullName)
try {
    $activeWidthPixels = [Math]::Max(
        1,
        [int][Math]::Round(
            $profileImage.Width * $Scale,
            [MidpointRounding]::AwayFromZero))
    $profile = [ordered]@{
        schema_version = 1
        phase_axis = "x"
        image_width_px = $profileImage.Width
        image_height_px = $profileImage.Height
        active_width_fraction = $Scale
        active_width_px = $activeWidthPixels
        stripe_period_px = $StripePeriodPixels
        stripe_cycle_count = [int][Math]::Ceiling(
            $activeWidthPixels / [double]$StripePeriodPixels)
        gray_bits = 8
        pattern_ids = @(0..21)
    }
    $profileJson = $profile | ConvertTo-Json -Depth 3
    $profilePath = Join-Path $outputPath "pattern_profile.json"
    [System.IO.File]::WriteAllText(
        $profilePath,
        $profileJson + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false))
    Write-Host "[generated] $profilePath"
}
finally {
    $profileImage.Dispose()
}

Write-Host (
    "[ok] Centered patterns use {0:P0} width, 100% height, and a {1}px stripe period: {2}" -f
        $Scale,
        $StripePeriodPixels,
        $outputPath)
