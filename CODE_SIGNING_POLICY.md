# Code signing policy

Registratore Azioni (Action Recorder) is open source software released under the MIT license.

## Who signs
Free code signing provided by [SignPath.io](https://signpath.io), certificate by the [SignPath Foundation](https://signpath.org).

## How the executable is built
The Windows executable is built automatically by GitHub Actions (`.github/workflows/build.yml`) from the source code in this repository, on GitHub-hosted runners, with no manual steps. Only executables built this way are submitted for signing.

## Roles
- Author, reviewer and approver: [Domenico](https://github.com/dom0736)

## Privacy policy
This program will not transfer any information to other networked systems unless specifically requested by the user. All recorded data stays on the user's PC, in the project folder. The "Report a problem" button only prepares an email that the user reads and sends on their own.
