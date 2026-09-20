# Capacity notes for the existing Hetzner host

Measured on 2026-09-20 from the running private pilot, not from authenticated heavy websites.

## Host baseline

- Physical RAM: 7.6 GiB; available at measurement: about 4.1 GiB.
- Swap: 4.0 GiB, about 1.8 GiB already in use.
- Root filesystem: 75 GiB, 29 GiB free.
- Host also runs storefront, Dokploy, database, Hermes, gateway, and other production services.
- Browser egress observed from container: `204.168.150.160`.

## One browser, one lightweight public page

Measured from Docker after opening `https://example.com` for five seconds and then closing the session:

| Component | Resting RAM | With example.com session |
| --- | ---: | ---: |
| Browser node | 412.5 MiB | 456.3 MiB |
| Controller | 169 MiB | 169 MiB |
| Approval broker | 37.4 MiB | 37.4 MiB |
| Total | ~619 MiB | ~663 MiB |

CPU samples after the page had settled were below 1% per component; these are **not startup/navigation peaks**. The page is intentionally trivial; social apps, mail, dashboards, video, and many tabs may be much heavier. A separate temporary browser-node probe with a 1.2 GiB memory limit started cleanly, measured ~214 MiB before workload, and was stopped/removed. It is not evidence that the limit suffices for real sites.

## Disk

- Current `/opt/auto-browser/data` is 1.2 MiB, including 36 KiB under saved auth state. This is only synthetic/non-sensitive pilot data, not a realistic user profile.
- The browser-node image is about 539 MiB and the controller image about 545 MiB virtual size. Docker shares identical image layers across containers; this is not a per-user image charge.
- Browser profile/storage-state size depends on the websites; downloads, screenshots, traces, and cache can dominate. For planning only, reserve at least 1–2 GiB of persistent space per human user **plus any expected downloads**, apply retention/quotas, and measure after real use. This is an allowance, not a measured consumption.

## Admission recommendation

- Keep one active browser on this shared 8 GiB host now. A second must be tested with representative sites and a hard memory/CPU/disk budget before allowing production concurrency.
- Five full, always-running isolated browser+controller stacks would add roughly 3 GiB even at the measured light steady state, leaving around 1 GiB of the currently available memory for five users' real pages and the host's other workloads. That is not safe to promise. Do not start all five on this host.
- Five *provisioned* user slots with one active at a time is a separate product mode and can be considered after the phone gate and per-user routing exist. If five concurrent sessions become necessary, revisit machine size or add browser workers. Benchmark site mix first; no exact sessions-per-GiB rule exists.
- Memory caps protect the host from unbounded growth but can kill a browser mid-task. Swap is not a substitute for RAM and may make interactive sessions unusably slow.
