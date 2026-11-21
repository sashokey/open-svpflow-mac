# Guidelines

## Purpose

Open SVPflow brings motion-compensated frame interpolation to everyday movie playback on Apple Silicon. This fork builds on [upstream open-svpflow 0.1.3](https://github.com/Z1xus/open-svpflow/tree/badc0de2282d2300303f5b9461919a2bc4dbcf65) and adds a macOS launcher for the user's installed mpv. The goal is to make interpolation easy to configure while preserving normal mpv playback, scaling, subtitles, scripts and user settings.

## What the fork adds

- A fix for packed motion vectors with a negative vertical component. Both readers restore the horizontal component affected by the signed vertical value.
- ARM64 NEON kernels for SAD and SATD in motion estimation. SAD sums absolute pixel differences. SATD sums absolute differences after a Hadamard transform. The optimized kernels preserve the integer costs used to choose motion vectors.
- ARM64 NEON for six-tap cubic downsampling in Super. Both passes preserve the integer weights, rounding and edge handling. Remaining samples use the scalar path.
- Precomputed cubic weights for 8-bit OpenCL shader 23 on Apple Silicon. The zero-origin 16-pixel field grid uses the same texture reads and blend order with shared coordinates and weights.
- Shared GPU motion-field sampling for compatible 8-bit YUV420 chroma planes with shader 23. A paired U/V dispatch uses the existing pixel sampling and rounding helpers. Eligibility checks cover packed source offsets, matching plane parameters, and compatibility with optional effects.
- Batched OpenCL uploads and direct readback for 8-bit video on Apple Silicon. The blocking V upload depends on the queued Y and U uploads. GPU output goes directly into the destination planes. On error, wait for accepted transfers before returning frame buffers. If OpenCL cannot confirm completion, abort the host process to protect that memory.
- A Python launcher and VapourSynth graph that validate settings, prepare input frames, and apply interpolation at the requested rate. The mpv input normalizer can fit the processing size to the rendered video while preserving its aspect ratio. The graph crops any padding added during frame preparation.
- A SwiftUI launcher with file drop, playlist management, localized settings and a compact playback-quality view. An independent mpv session keeps playing when the launcher quits.
- A GitHub Actions build that tests the engine and launcher, exercises the extracted application with external mpv, and checks the published ARM64 ZIP. The ZIP contains one ad-hoc signed app bundle with the launcher and plugins.

## Decisions and ideas explored

- **Reuse the engine.** A separate interpolation engine was considered early. The upstream engine was already viable on Apple Silicon, so the fork concentrated on the verified vector fix and the playback integration.
- **Keep playback dependencies external.** Bundling the entire player stack was considered for easier installation. The app bundles the launcher and plugins and checks for Homebrew mpv, Python and VapourSynth. This lets users keep their installed player and its configuration.
- **Build the GUI on the existing launcher.** The CLI established playback and settings behavior. SwiftUI made those controls accessible without hand-editing JSON and uses the same settings contract as the CLI.
- **Use the playback status to inform decisions.** The interface shows recent playback quality and suggests a lower target rate or lighter settings when suitable. Users choose the settings, and interpolation changes apply starting with the next file.

## Guidelines for future changes

- **Update these guidelines alongside every change.** Record the affected behavior, its scope, and how to verify it.
- **Keep the fork focused.** Establish a reproducible issue before changing the engine. Put playback policy and compatibility handling in the launcher or graph when the issue originates there.
- **Keep settings consistent.** Derive interface controls and defaults from the Python settings contract. Preserve existing settings-file semantics and apply interpolation changes at the next file boundary.
- **Explain compatibility precisely.** Use the SVPflow parameter documentation as a reference. Describe the preset and supported settings in terms of what this implementation does, and identify options it cannot reproduce.
- **Preserve the playback boundary.** Use local Unix-domain IPC for session control. Keep the launcher free of internet requests, telemetry and automatic updates. Dependency installation starts at the user's request, and external mpv retains its own capabilities.
- **Verify the behavior users see.** Measure interpolation throughput separately from decoding and display. For window changes, check the native app on an isolated runner, including restored window sizes and scrolling in long settings panes. Publish packages after the automated build and extracted-app checks pass.

## Interpolation presets

`Smooth` uses uniform interpolation, quarter-pixel motion vectors, 8-bit synthesis, and a 16-pixel motion-vector grid step. With the default native options this means 16×16 blocks with zero overlap. Imported native options can override the generated engine parameters. The target frame rate and performance settings are selected separately from the interpolation preset.

## Verifying engine changes

For a performance-only change, compare with a known engine build using the same input frames, processing dimensions, output format, target rate, preset, and backend. Check the affected stage directly. Compare every byte of the resulting Y, U, and V planes, including synthesized frames. For motion-search optimizations, also compare the vector payloads. A result from one backend applies to that backend and its settings.

Measure the kernel and the stage that calls it. Include preparation and cache costs in the stage measurement, and alternate repeated runs of the reference and candidate under the same conditions. For each run, record the test process's CPU Time, GPU Time, and elapsed time. Record CPU Time and GPU Time for other processes separately.

Test both builds at 120 and 240 FPS with the reference profile. Report elapsed time and byte equality for each fragment and rate.

Record the scope of the measured speedup and keep exploratory results separate from adopted optimizations.

For playback regression checks, use the pinned build manifest. Run cumulative commits in chronological order and keep the runtime settings fixed across engine changes. Use the reference 16-pixel motion-vector grid and the same baseline for isolated changes. Measure decoder and video-output drops separately in the same media interval at 120 and 240 FPS. Save elapsed playback time and foreground and background CPU Time and GPU Time for each run. Use hidden, silent mpv with `vo=null` for this benchmark.

To recheck an interrupted comparison, repeat the selected runs with their original settings and retain the retest measurements alongside the original series. Reuse completed runs with matching binaries, launcher and runtime settings. Record which runs were reused.

To assess an optimization in the current release, build both variants from the same commit and vary only the optimization under test. Use the same frame reference for both variants.

For changes to the interpolation calculation, use known intermediate source frames held out from the input. Compare how closely each engine reconstructs those frames using the same profile. Once the result is chosen, freeze it as the pixel reference for subsequent performance-only changes.

See the [README](README.md) for installation, usage and compatibility details.
