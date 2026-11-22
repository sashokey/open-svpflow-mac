Native Apple Silicon interpolation plugins with a SwiftUI launcher and settings interface for external mpv. Requires macOS 26 or later and compatible Homebrew playback components. Based on [open-svpflow 0.1.4](https://github.com/Z1xus/open-svpflow/tree/badc0de73b274d73bb8dad4affece877261338ce) with a fix for signed motion-vector decoding and targeted Apple Silicon optimizations.

![Open SVPflow main window](assets/OpenSVPflow-screenshot.png)

## Installation

Download the application ZIP from [Releases](https://github.com/sashokey/open-svpflow/releases), extract it and open **Open SVPflow.app**. The archive contains only the application. mpv, Python and VapourSynth are external dependencies, not bundled runtimes.

The app checks local components before showing playback controls. If required, **Install Homebrew…** or **Install Dependencies…** opens the official preparation commands in Terminal. Installation requires network access and may update related Homebrew packages; it starts only after an explicit click. Return to the app and choose **Retry** after preparation. Ready environments do not trigger installation or update checks.

Components are ad-hoc signed, not notarized. macOS may block the application or individual plugins. No quarantine removal or system-security changes are performed automatically. See [Apple's instructions for opening an app](https://support.apple.com/guide/mac-help/open-a-mac-app-from-an-unknown-developer-mh40616/mac).

## Usage notes

Multiple files form one ordered playlist. **New files during playback** chooses **Add to Queue** (default), **Replace Playlist** or **Ask Every Time**. Cancelling a question leaves current playback unchanged. Playback controls, subtitles and the playlist remain in mpv.

**After Playback Starts** chooses **Quit App** (default), **Move to Menu Bar** or **Keep Window Open**. Quit exits the interface, not the independent playback session. Reopening the app reconnects to the existing session. Closing mpv ends playback and cleans session resources. No login item or permanent background service is installed.

**Settings…** applies interpolation changes to the next file, including automatic playlist advances. The current video is not restarted. Settings are saved outside the application in Application Support and survive application replacement. Frame rate can follow the display, a fraction of its refresh, a source-rate multiplier or an explicit rational value. A requested target is not a guarantee of sustainable performance. Interpolation follows the rendered video size after resizing settles, preserving aspect and avoiding upscaling. Native size is used with `video-unscaled=yes`, an explicit `video-crop`, or an enabled video filter after the input normalizer.

Controls use independently implemented SVP parameter mappings. The fixed film preset is an application preset, not a recovered SVP Automatic profile. Unsupported official duplicate-cadence handling is disabled rather than represented as native dedup. Settings describe remaining compatibility limitations; matching parameter names does not establish identical engine output. No new HDR tone-mapping algorithm is added.

Existing schema-1 settings and source-tree CLI use remain supported. To use the packaged libraries from a source checkout, pass `--plugin-dir "/path/to/Open SVPflow.app/Contents/Frameworks"` to `player/launch.py`. Ordinary mpv configuration and scripts remain enabled. GUI queue/drop handling requires mpv 0.42 or later with Lua, libavfilter and VapourSynth support.

Readiness checks and playback control add no internet requests, telemetry or updater. Unix-domain IPC is local. Explicit installation uses external network-capable tools; mpv and user scripts retain their existing capabilities. The application is not App Sandboxed, so code signing does not enforce a network ban.
