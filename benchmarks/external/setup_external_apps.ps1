# Run this once from the repo root: .\benchmarks\external\setup_external_apps.ps1
# Creates the external-app directories and downloads all three benchmark applications,
# pinning exact versions/commits so trials are reproducible later.

$ErrorActionPreference = "Stop"

$ExternalDir = "benchmarks\external"
New-Item -ItemType Directory -Force "$ExternalDir\downloads" | Out-Null
New-Item -ItemType Directory -Force "$ExternalDir\results" | Out-Null
New-Item -ItemType Directory -Force "$ExternalDir\manifests" | Out-Null

# ---------------------------------------------------------------------------
# 1. LO2 light-oauth2 (Zenodo replication package -- NOT the 65 GB raw dataset)
# ---------------------------------------------------------------------------
Write-Host "Downloading light-oauth2.zip (19.7 MB replication package)..."
Invoke-WebRequest `
  "https://zenodo.org/records/18937117/files/light-oauth2.zip?download=1" `
  -OutFile "$ExternalDir\downloads\light-oauth2.zip"

$hash = (Get-FileHash "$ExternalDir\downloads\light-oauth2.zip" -Algorithm MD5).Hash
if ($hash -ne "19FD02BD7410759ED8D33AE078CEE746") {
    throw "light-oauth2.zip MD5 mismatch. Expected 19fd02bd7410759ed8d33ae078cee746, got $hash. Do not proceed with a corrupted download."
}
Write-Host "MD5 verified: $hash"

Expand-Archive -Force `
  "$ExternalDir\downloads\light-oauth2.zip" `
  "$ExternalDir\light-oauth2"

# ---------------------------------------------------------------------------
# 2. AWS retail-store-sample-app -- pin a specific release, not "latest"
# ---------------------------------------------------------------------------
# Check https://github.com/aws-containers/retail-store-sample-app/releases
# for the current release tag and update $RetailStoreTag before running.
$RetailStoreTag = "v1.2.1"

Write-Host "Cloning retail-store-sample-app at tag $RetailStoreTag..."
git clone --branch $RetailStoreTag --depth 1 `
  https://github.com/aws-containers/retail-store-sample-app `
  "$ExternalDir\retail-store"

git -C "$ExternalDir\retail-store" rev-parse HEAD | Tee-Object -FilePath "$ExternalDir\retail-store-commit.txt"

Write-Host "Downloading pinned docker-compose.yaml for retail-store (release asset, not repo file)..."
Invoke-WebRequest `
  "https://github.com/aws-containers/retail-store-sample-app/releases/download/$RetailStoreTag/docker-compose.yaml" `
  -OutFile "$ExternalDir\retail-store\docker-compose.yaml"

# ---------------------------------------------------------------------------
# 3. Stan's Robot Shop
# ---------------------------------------------------------------------------
Write-Host "Cloning robot-shop..."
git clone https://github.com/instana/robot-shop "$ExternalDir\robot-shop"
git -C "$ExternalDir\robot-shop" rev-parse HEAD | Tee-Object -FilePath "$ExternalDir\robot-shop-commit.txt"

Write-Host ""
Write-Host "Done. Recorded commit hashes:"
Get-Content "$ExternalDir\retail-store-commit.txt"
Get-Content "$ExternalDir\robot-shop-commit.txt"
Write-Host "Record these in your paper/README so trials are reproducible."