# Potential Benchmark Datasets

Used or retained for Compose repair bakeoffs.

## In the current bakeoff

- **Robot Shop cart slice** — public image `robotshop/rs-cart:2.1.0` + Redis in
  `benchmarks/fixtures/robot_cart/`. Opaque fault: stop `cart`.
  Full Instana Robot Shop (commit `55292e2199f2fb00a165b1f7d3045fe7f8922038`)
  is too heavy/flaky on small arm64 VMs (shipping health); optional clone under
  `benchmarks/apps/` (gitignored).

## Future workloads

### LO2 light-oauth2

- [Zenodo record](https://zenodo.org/records/18937117)
- MD5: `19fd02bd7410759ed8d33ae078cee746`

### AWS Retail Store Sample App

- [Source](https://github.com/aws-containers/retail-store-sample-app)
- Pin: tag `v1.2.1`, commit `ff77f6e4a2453e937985abcb9ce1e0415b3ac9f8`
