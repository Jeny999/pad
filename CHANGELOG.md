# Changelog

All notable changes to Pad are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning: [SemVer](https://semver.org/).

## [Unreleased]

- Bilingual RU/EN interface with a language switch in the header and on the connection screen.
- Scrollable connection overlay on short screens.
- New four-step connection guide with annotated screenshots (RU/EN).
- Rebuilt Android APK with the bilingual interface.
- Repository mockup: clean Pad branding, README (RU/EN), connection diagram, UI mockups.
- Repository published as `Jeny999/pad` (private).
- Connection verified over LAN: access code, sticks, buttons, rumble, 1 ms ping.
- Troubleshooting note for the "server is not running" case.

## [0.1.0] - 2026-10-30

- Initial Pad scaffold based on the working phone-as-Xbox-controller prototype.
- Virtual Xbox 360 controller through ViGEmBus.
- Touch gamepad: sticks, ABXY, D-pad, LB/RB, LT/RT, Back/Start, stick clicks.
- Rumble passthrough, layout editor, button size/shape, stick sensitivity.
- PC screen streaming with quality/FPS settings.
- LAN access code, Windows firewall helper.
- Tauri mobile shell for Android (arm64).
