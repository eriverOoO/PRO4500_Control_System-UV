param(
    [string]$SourceDirectory = (Join-Path $PSScriptRoot "generated_patterns1"),
    [string]$OutputDirectory = (Join-Path $PSScriptRoot "generated_patterns_centered"),
    [ValidateRange(0.000001, 1.0)]
    [double]$Scale = 0.1
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
    public static void Save(string sourcePath, string outputPath, double scale, bool invert)
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
            int activeHeight = Math.Max(
                1,
                (int)Math.Round(source.Height * scale, MidpointRounding.AwayFromZero));
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
                            int sourceX = Math.Min(
                                source.Width - 1,
                                (x - offsetX) * source.Width / activeWidth);
                            Color color = source.GetPixel(sourceX, sourceY);
                            int gray = (
                                299 * color.R +
                                587 * color.G +
                                114 * color.B +
                                500) / 1000;
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
        $true)
    Write-Host "[generated] $destination"
}

Write-Host (
    "[ok] Centered patterns use {0:P0} of the original width and height: {1}" -f
        $Scale,
        $outputPath)
